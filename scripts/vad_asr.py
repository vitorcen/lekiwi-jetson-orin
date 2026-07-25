#!/usr/bin/env python3
"""Transcribe what a candidate VAD config actually cut, and score it against the
corpus text. Runs on the board, after scripts/vad_sweep.py has narrowed the field.

The sweep answers "did the VAD cut in the right places". This answers the only
question the robot cares about: "does the ASR read it back correctly". They come
apart in one specific way — a cut that starts 40 ms late scores as a hit in the
sweep and loses the initial consonant here, which is how 「停」 becomes nothing.

Character error rate is computed against the known script, so a config that keeps
every utterance but shaves their onsets is visible as CER even at 12/12 recall.

Usage (on the board):
  ~/work/lekiwi-jetson-orin/voice/.venv/bin/python /tmp/vadtune/vad_asr.py \
      --engine fsmn --min-speech 0.25 --pre-roll 0.3 --gain 6
  ... --config '{"engine":"fsmn","min_speech_s":0.25,"pre_roll_s":0.3,"gain_db":6}'
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, "/tmp/vadtune")
import vad_sweep as S                                           # noqa: E402

sys.path.insert(0, S.VOICE)
import voice_asr_obs as vobs                                    # noqa: E402
import voice_engines as ve                                      # noqa: E402

PUNCT = "。，、,.!?！？；;:：\"'“”‘’()（）… "


def norm(s: str) -> str:
    return "".join(c for c in (s or "") if c not in PUNCT)


def cer(ref: str, hyp: str) -> float:
    """Levenshtein / len(ref). Insertions count: an ASR that hallucinates a
    sentence out of room noise is not doing better than one that stays quiet."""
    r, h = norm(ref), norm(hyp)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i]
        for j, hc in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(r)


def run_noise(x, cfg: dict, asr, log) -> dict:
    """What the robot would actually have done with 90 s of empty room.

    A VAD false trigger is not yet a spurious turn: daemon._asr_then_turn drops
    the segment unless classify_segment() calls the decode ACCEPTED. So the
    number that matters is not how often the VAD fires, it is how often the whole
    chain talks to itself — which is VAD misfires times ASR hallucinations."""
    params = {k: cfg.get(k, d) for k, d in
              (("threshold", 0.5), ("min_speech_s", 0.25),
               ("min_silence_s", 0.55), ("pre_roll_s", 0.0))}
    y, spans, _ = S.replay(x, cfg["engine"], params, cfg.get("gain_db", 0.0))
    counts, accepted = {}, []
    for a, b in spans:
        text = asr.transcribe(np.ascontiguousarray(y[a:b]))
        outcome = vobs.classify_segment(text)
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome == vobs.ACCEPTED:
            accepted.append({"at_s": round(a / S.SR, 1),
                             "dur_s": round((b - a) / S.SR, 2), "text": text})
            log(f"    起轮! {a/S.SR:6.1f}s {(b-a)/S.SR:.2f}s  {text}")
    mins = len(x) / S.SR / 60.0
    return {"segments": len(spans), "outcomes": counts,
            "spurious_turns": len(accepted),
            "per_min": round(len(accepted) / mins, 2),
            "vad_fp_per_min": round(len(spans) / mins, 2),
            "accepted": accepted}


def run_oracle(take, asr, log, pad_s: float = 0.2) -> dict:
    """Fun-ASR on the ground-truth windows — what a perfect VAD would hand it.

    Without this, every transcription error is ambiguous: bad cut, or bad decode?
    This is the ceiling. Anything a real config loses below it is the VAD's."""
    x, utts = take["x"], take["utts"]
    pad = int(pad_s * S.SR)
    rows = []
    for ref, ua, ub in utts:
        seg = np.ascontiguousarray(x[max(0, ua - pad):min(len(x), ub + pad)])
        hyp = asr.transcribe(seg)
        rows.append({"ref": ref, "hyp": hyp, "cer": round(cer(ref, hyp), 3)})
        log(f"    {ref:<16s} → {hyp}")
    exact = sum(1 for r in rows if norm(r["hyp"]) == norm(r["ref"]))
    return {"n": len(rows), "exact": exact,
            "cer": round(float(np.mean([r["cer"] for r in rows])), 3),
            "rows": rows}


def degrade_to(x, utts, lo: int, hi: int, target_db: float):
    """Add the recording's OWN gap noise until its consonant-band SNR hits target.

    Two mics at different distances cannot be compared directly — the far one
    always loses, and that says nothing about the capsule. Bringing the cleaner
    recording down to the other's SNR puts both on one line. Its own gap noise is
    used rather than white noise so the added interference keeps that mic's
    spectral character instead of inventing a new one."""
    speech, gap = S._masks(len(x), utts, lo, hi)
    src = x[gap]
    if src.size < S.SR:
        return None, None
    reps = int(np.ceil(len(x) / src.size))
    tile = np.tile(src, reps)[:len(x)].astype(np.float32)
    a, b = 0.0, 512.0
    for _ in range(40):                                     # bisect on alpha
        m = (a + b) / 2
        cur = S.band_snr(x + m * tile, utts, lo, hi)
        if cur > target_db:
            a = m
        else:
            b = m
    y = (x + ((a + b) / 2) * tile).astype(np.float32)
    return y, S.band_snr(y, utts, lo, hi)


def compare_mics(dirpath: str, tag: str, manifest, corpus, asr, log) -> dict:
    """Same playback, two microphones, one ASR, ground-truth cuts.

    Prints the consonant-band SNR next to every error rate on purpose: a mic that
    was further away decodes worse for a reason that has nothing to do with the
    mic. Only a worse CER at a comparable band SNR is evidence against it."""
    out, keep = {}, {}
    log("\n=== 麦克风对比(oracle 切分,同一个 ASR) ===")
    utts0 = [(u["text"], u["start"], u["end"]) for u in manifest["utterances"]]
    log("\n-- 数字语料(完全不过麦克风) — 天花板")
    out["digital"] = run_oracle({"x": corpus, "utts": utts0}, asr, log)
    log(f"    逐字全对 {out['digital']['exact']}/{out['digital']['n']}  "
        f"CER {out['digital']['cer']:.3f}")

    for who in ("boardmic", "macmic"):
        p = os.path.join(dirpath, f"{tag}-{who}.wav")
        if not os.path.exists(p):
            log(f"\n-- {who}: 缺文件 {p}")
            continue
        x = S.read_wav(p)
        try:
            off, _ = S.align(x, corpus)
        except SystemExit as exc:
            log(f"\n-- {who}: 对齐失败 — {exc}")
            continue
        utts, lo, hi = S.truth(manifest, off)
        band = S.band_snr(x, utts, max(0, lo), min(len(x), hi))
        wide = S._split_db(x, *S._masks(len(x), utts, max(0, lo), min(len(x), hi)))
        log(f"\n-- {who}  偏移 {off/S.SR:+.2f}s  宽带 {wide:+.1f} dB  "
            f"1-3.4k {band:+.1f} dB")
        r = run_oracle({"x": x, "utts": utts}, asr, log)
        r.update({"band_snr": round(band, 1), "wide_snr": round(wide, 1)})
        log(f"    逐字全对 {r['exact']}/{r['n']}  CER {r['cer']:.3f}")
        out[who] = r
        keep[who] = (x, utts, max(0, lo), min(len(x), hi))

    # The only comparison that says anything about the microphones themselves.
    if len(keep) == 2:
        hi_k, lo_k = sorted(keep, key=lambda k: -out[k]["band_snr"])
        target = out[lo_k]["band_snr"]
        y, got = degrade_to(*keep[hi_k], target)
        if y is not None:
            log(f"\n-- {hi_k} 掺入自身底噪降到 {got:+.1f} dB (对齐 {lo_k} 的 "
                f"{target:+.1f} dB)")
            x0, utts, a, b = keep[hi_k]
            r = run_oracle({"x": y, "utts": utts}, asr, log)
            r["band_snr"] = round(got, 1)
            log(f"    逐字全对 {r['exact']}/{r['n']}  CER {r['cer']:.3f}")
            out[f"{hi_k}@{lo_k}SNR"] = r

    log("\n--- 汇总 ---")
    log(f"  {'来源':<10s} {'1-3.4k SNR':>11s} {'逐字全对':>9s} {'CER':>7s}")
    for k, r in out.items():
        snr = f"{r['band_snr']:+.1f} dB" if "band_snr" in r else "—"
        log(f"  {k:<10s} {snr:>11s} {r['exact']:>6d}/{r['n']:<2d} {r['cer']:>7.3f}")
    return out


def load_asr(name: str):
    cls = ve.REGISTRY["asr"][name]
    eng = cls()
    eng.load()
    return eng


def run(take, cfg: dict, asr, log) -> dict:
    params = {k: cfg.get(k, d) for k, d in
              (("threshold", 0.5), ("min_speech_s", 0.25),
               ("min_silence_s", 0.55), ("pre_roll_s", 0.0))}
    x, spans, lost = S.replay(take["x"], cfg["engine"], params, cfg.get("gain_db", 0.0))
    if lost:
        log(f"  !! {lost} 段无法定位")
    utts, lo, hi = take["utts"], take["lo"], take["hi"]
    spans = [(a, b) for a, b in spans if b > lo and a < hi]

    claimed, rows = {}, []
    for si, (a, b) in enumerate(spans):
        hit = None
        for ui, (_, ua, ub) in enumerate(utts):
            if S.hits(a, b, ua, ub) and ui not in claimed:
                hit, claimed[ui] = ui, si
                break
        text = asr.transcribe(np.ascontiguousarray(x[a:b]))
        ref = utts[hit][0] if hit is not None else ""
        rows.append({"start_s": round(a / S.SR, 2), "dur_s": round((b - a) / S.SR, 2),
                     "ref": ref, "hyp": text,
                     "cer": round(cer(ref, text), 3) if ref else None})
        mark = "  " if hit is not None else "FP"
        log(f"   {mark} {a/S.SR:6.2f}s {(b-a)/S.SR:5.2f}s  "
            f"{(ref or '—'):<16s} → {text}")

    got = [r for r in rows if r["ref"]]
    miss = [u[0] for i, u in enumerate(utts) if i not in claimed]
    mean_cer = float(np.mean([r["cer"] for r in got])) if got else 1.0
    exact = sum(1 for r in got if norm(r["hyp"]) == norm(r["ref"]))
    return {"recall": len(got), "n_utt": len(utts), "miss": miss,
            "fp": len(rows) - len(got), "exact": exact,
            "cer": round(mean_cer, 3), "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=S.DIR)
    ap.add_argument("--asr", default="funasr")
    ap.add_argument("--config", action="append", default=[],
                    help="JSON config; repeatable. Overrides the flag form.")
    ap.add_argument("--engine", default="fsmn")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--min-speech", type=float, default=0.25)
    ap.add_argument("--min-silence", type=float, default=0.55)
    ap.add_argument("--pre-roll", type=float, default=0.0)
    ap.add_argument("--gain", type=float, default=0.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--oracle", default="",
                    help="NAME of a wav+json pair in --dir: decode the "
                         "ground-truth windows with no microphone in the path "
                         "and group the error rate by TTS voice")
    ap.add_argument("--compare", default="",
                    help="tag written by scripts/mic_compare.py: decode that "
                         "tag's board-mic and mac-mic takes and stop")
    args = ap.parse_args()

    def log(m=""):
        print(m, flush=True)

    configs = [json.loads(c) for c in args.config] or [{
        "engine": args.engine, "threshold": args.threshold,
        "min_speech_s": args.min_speech, "min_silence_s": args.min_silence,
        "pre_roll_s": args.pre_roll, "gain_db": args.gain}]

    if args.oracle:
        with open(os.path.join(args.dir, f"{args.oracle}.json")) as fh:
            man = json.load(fh)
        x = S.read_wav(os.path.join(args.dir, f"{args.oracle}.wav"))
        utts = [(u["text"], u["start"], u["end"]) for u in man["utterances"]]
        log(f"加载 ASR: {args.asr}   {args.oracle}: {len(utts)} 句")
        asr = load_asr(args.asr)
        try:
            r = run_oracle({"x": x, "utts": utts}, asr, lambda *_: None)
        finally:
            asr.unload()
        by = {}
        for u, row in zip(man["utterances"], r["rows"]):
            by.setdefault(u.get("voice", "?"), []).append(row)
        log(f"\n{'音色':<26s} {'句数':>4s} {'逐字全对':>8s} {'CER':>7s}   常错的句子")
        for v, rows in by.items():
            ex = sum(1 for q in rows if norm(q["hyp"]) == norm(q["ref"]))
            bad = [q["ref"] for q in sorted(rows, key=lambda q: -q["cer"])
                   if q["cer"] > 0][:3]
            log(f"{v:<26s} {len(rows):>4d} {ex:>5d}/{len(rows):<2d} "
                f"{float(np.mean([q['cer'] for q in rows])):>7.3f}   "
                + " ".join(bad))
        log(f"\n合计 逐字全对 {r['exact']}/{r['n']}  CER {r['cer']:.3f}")
        if args.out:
            with open(args.out, "w") as fh:
                json.dump({"by_voice": by, "total": r}, fh,
                          ensure_ascii=False, indent=1)
        return 0

    with open(os.path.join(args.dir, "corpus.json")) as fh:
        manifest = json.load(fh)
    corpus = S.read_wav(os.path.join(args.dir, "corpus.wav"))

    if args.compare:
        log(f"加载 ASR: {args.asr}")
        asr = load_asr(args.asr)
        try:
            res = compare_mics(args.dir, args.compare, manifest, corpus, asr, log)
        finally:
            asr.unload()
        if args.out:
            with open(args.out, "w") as fh:
                json.dump(res, fh, ensure_ascii=False, indent=1)
        return 0

    takes = {}
    for name in ("near", "far"):
        p = os.path.join(args.dir, f"{name}.wav")
        if not os.path.exists(p):
            continue
        x = S.read_wav(p)
        off, _ = S.align(x, corpus)
        utts, lo, hi = S.truth(manifest, off)
        S.check_alignment(x, utts, lo, hi)
        takes[name] = {"x": x, "utts": utts, "lo": max(0, lo), "hi": min(len(x), hi)}
    noise = S.read_wav(os.path.join(args.dir, "noise.wav"))

    log(f"加载 ASR: {args.asr}")
    asr = load_asr(args.asr)
    out = []
    try:
        oracle = {}
        for name, t in takes.items():
            log(f"\n=== oracle 切分({name}) — VAD 完美时 {args.asr} 的上限 ===")
            oracle[name] = run_oracle(t, asr, log)
            o = oracle[name]
            log(f"    逐字全对 {o['exact']}/{o['n']}  CER {o['cer']:.3f}")
        out.append({"config": "oracle", "asr": args.asr, **oracle})
        for cfg in configs:
            log(f"\n=== {json.dumps(cfg, ensure_ascii=False)} ===")
            row = {"config": cfg, "asr": args.asr}
            for name, t in takes.items():
                log(f" -- {name}")
                row[name] = run(t, cfg, asr, log)
                r = row[name]
                log(f"    召回 {r['recall']}/{r['n_utt']}  全对 {r['exact']}  "
                    f"误触发 {r['fp']}  CER {r['cer']:.3f}"
                    + (f"  漏: {' '.join(r['miss'])}" if r["miss"] else ""))
            log(" -- noise (空房间)")
            row["noise"] = run_noise(noise, cfg, asr, log)
            n = row["noise"]
            log(f"    VAD 切出 {n['segments']} 段 ({n['vad_fp_per_min']}/分),"
                f" 其中 {n['spurious_turns']} 段会真的起轮 "
                f"({n['per_min']}/分)  {n['outcomes']}")
            out.append(row)
    finally:
        asr.unload()

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
        log(f"\n写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

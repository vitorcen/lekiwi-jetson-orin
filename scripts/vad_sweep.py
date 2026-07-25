#!/usr/bin/env python3
"""Sweep VAD parameters offline, on the board, over fixed recordings.

Runs on the Jetson (fsmn needs the aarch64 llama-funasr-vad binary, and silero /
ten need the board's sherpa build), driven by scripts/vad_record.py from the Mac.

The recordings are replayed through the REAL voice_vad engines in the same 320 ms
chunks daemon._capture_loop feeds, with the same apply_gain() in front. Nothing
here reimplements segmentation — a sweep of a reimplementation tunes the
reimplementation.

Scoring, per config:
  noise.wav        every segment is a false trigger. This is the number the field
                   bug is about (the VAD calling 47% of idle audio speech).
  near/far.wav     each of the 12 utterances must be caught exactly once, with
                   its onset inside the cut. A segment overlapping no utterance
                   is a false trigger; two utterances in one segment is a merge.

Segment positions are recovered by locating each returned segment inside the
gained source array — every engine returns a contiguous slice of what it was
fed, so this is exact rather than inferred from timing.

Usage (from the Mac):
  scp scripts/vad_sweep.py jetson@<board>:/tmp/vadtune/
  ssh jetson@<board> '~/work/lekiwi-jetson-orin/voice/.venv/bin/python \
      /tmp/vadtune/vad_sweep.py --engines fsmn,silero,ten'
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
import wave

import numpy as np

SR = 16000
CHUNK = int(0.32 * SR)                 # daemon._capture_loop feeds 320 ms
DIR = "/tmp/vadtune"
VOICE = os.path.expanduser("~/work/lekiwi-jetson-orin/voice")
MIN_OVERLAP = int(0.10 * SR)           # real overlap needed to call a segment a hit

sys.path.insert(0, VOICE)
import voice_vad as vv                                          # noqa: E402


# --------------------------------------------------------------------------- #
# Recordings
# --------------------------------------------------------------------------- #
def read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        if w.getframerate() != SR or w.getnchannels() != 1:
            raise SystemExit(f"{path}: expected mono {SR} Hz, "
                             f"got {w.getnchannels()}ch {w.getframerate()} Hz")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def frame_db(x: np.ndarray, hop_ms: int = 10) -> np.ndarray:
    f = SR * hop_ms // 1000
    n = (len(x) // f) * f
    r = np.sqrt((x[:n].reshape(-1, f) ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(np.maximum(r, 1e-9))


def align(x: np.ndarray, corpus: np.ndarray, hop_ms: int = 10):
    """Where the corpus starts inside the recording, by cross-correlating the
    frame-energy envelopes.

    An earlier version keyed on the leading marker tone and picked, on the 12 dB
    attenuated take, a random room transient instead — a plausible-looking offset
    that scored every segment against the wrong window. The envelope of a whole
    68 s script is not something a bang can imitate, and mean-removed dB makes it
    level-invariant, so this works at any playback volume. Returns (offset, z),
    where z is how far the winning lag stands above the rest."""
    f = SR * hop_ms // 1000

    def env(a):
        n = (len(a) // f) * f
        e = np.sqrt((a[:n].reshape(-1, f) ** 2).mean(axis=1) + 1e-12)
        e = 20 * np.log10(np.maximum(e, 1e-9))
        return e - e.mean()

    ex, ec = env(x), env(corpus)
    if len(ex) < len(ec) + 10:
        raise SystemExit("录音比语料还短,没法对齐")
    r = np.correlate(ex, ec, mode="valid")
    k = int(r.argmax())
    rest = np.delete(r, slice(max(0, k - 50), k + 50))
    z = float((r[k] - rest.mean()) / (rest.std() + 1e-9)) if rest.size else 99.0
    # Loose gate only. The correlation of a long envelope is broad and skewed, so
    # z sits around 5 even for a textbook-sharp peak — the real proof that the
    # offset is right is check_alignment/band_snr below, which test whether the
    # speech windows actually contain the speech.
    if z < 3.0:
        raise SystemExit(f"对齐失败 (z={z:.1f}) — 播放是不是没出声?")
    return k * f, z


def truth(manifest: dict, offset: int):
    """Utterance windows and the scoring region, in recording sample indices."""
    utts = [(u["text"], u["start"] + offset, u["end"] + offset)
            for u in manifest["utterances"]]
    return utts, manifest["score_from"] + offset, manifest["total"] + offset


def _masks(n: int, utts, lo: int, hi: int):
    speech = np.zeros(n, dtype=bool)
    for _, a, b in utts:
        speech[max(0, a):min(n, b)] = True
    gap = np.zeros(n, dtype=bool)
    gap[max(0, lo):min(n, hi)] = True
    gap &= ~speech
    if not speech.any() or not gap.any():
        raise SystemExit("对齐后语音段或静音段为空")
    return speech, gap


def _split_db(x: np.ndarray, speech, gap) -> float:
    return float(20 * np.log10(max(float(np.sqrt((x[speech] ** 2).mean())), 1e-9))
                 - 20 * np.log10(max(float(np.sqrt((x[gap] ** 2).mean())), 1e-9)))


def check_alignment(x: np.ndarray, utts, lo: int, hi: int) -> float:
    """Speech windows must actually be louder than the gaps. This is the assertion
    the whole table hangs on: an offset found in the wrong place produces a
    plausible-looking sweep scored against nonsense."""
    d = _split_db(x, *_masks(len(x), utts, lo, hi))
    if d < 6.0:
        raise SystemExit(f"对齐后语音段只比静音段高 {d:.1f} dB — 对齐或播放有问题")
    return d


def band_snr(x: np.ndarray, utts, lo: int, hi: int,
             f_lo: float = 1000.0, f_hi: float = 3400.0) -> float:
    """Speech-minus-gap level restricted to the consonant band.

    Broadband RMS hides the failure that actually killed the first take: overall
    the speech sat 15 dB above the gaps, while everything above 1 kHz was 2.6 dB
    BELOW the room noise. That band is what a neural VAD and an ASR decoder run
    on, so it is the number that decides whether a recording is usable."""
    spec = np.fft.rfft(x)
    freq = np.fft.rfftfreq(len(x), 1.0 / SR)
    spec[(freq < f_lo) | (freq > f_hi)] = 0
    y = np.fft.irfft(spec, n=len(x)).astype(np.float32)
    return _split_db(y, *_masks(len(x), utts, lo, hi))


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
def clip_frac(x_raw: np.ndarray, gain_db: float, speech) -> float:
    """Share of speech samples the make-up gain drives into the rails.

    apply_gain hard-clips at ±1, so a large gain_db buys level by destroying the
    loudest syllables — measured here because a config that wins the sweep while
    clipping 5% of its speech is winning on a broken signal."""
    if gain_db <= 0:
        return 0.0
    x = vv.apply_gain(x_raw, gain_db)[speech]
    return float(np.mean(np.abs(x) >= 0.999))


def locate(src: np.ndarray, seg: np.ndarray, hint: int) -> int:
    """Start index of `seg` inside `src`. Engines hand back a contiguous slice of
    what they were fed, so this is an exact match, not a correlation."""
    n = seg.size
    hi = len(src) - n + 1
    if n == 0 or hi <= 0:
        return -1
    lo = max(0, hint) if max(0, hint) < hi else 0
    cand = np.flatnonzero(src[lo:hi] == seg[0]) + lo
    # A quiet recording holds only a few hundred distinct sample values, so the
    # first sample alone leaves thousands of candidates. Three more probes cut
    # that to ~1 before the full comparison.
    for k in (1, n // 2, n - 1):
        if cand.size <= 1:
            break
        cand = cand[src[cand + k] == seg[k]]
    for c in cand:
        if np.array_equal(src[c:c + n], seg):
            return int(c)
    return -1


def replay(x_raw: np.ndarray, engine: str, params: dict, gain_db: float):
    """Feed the whole recording through a fresh engine. Returns (gained_source,
    [(start, end)]). Gain is applied exactly where the daemon applies it."""
    x = vv.apply_gain(x_raw, gain_db)
    vad = vv.make_vad(engine, params, os.path.join(VOICE, "models"))
    spans = []
    hint = 0
    lost = 0

    def take(seg):
        nonlocal hint, lost
        s = locate(x, seg, hint)
        if s < 0:
            # A segment that cannot be found in the source is a segment we cannot
            # score. Silently dropping it would deflate recall and make a working
            # config look broken, so it is counted and surfaced.
            lost += 1
            return
        spans.append((s, s + len(seg)))
        hint = s

    try:
        for i in range(0, len(x), CHUNK):
            for seg in vad.feed(x[i:i + CHUNK]):
                take(seg)
        for seg in vad.flush():
            take(seg)
    finally:
        if hasattr(vad, "close"):
            vad.close()
    return x, spans, lost


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def frame_metrics(spans, utts, lo: int, hi: int, n: int, hop_ms: int = 10) -> dict:
    """Frame-level speech/non-speech accuracy over the scored region.

    Segment counts say how many turns the robot gets; these say how good the
    detector itself is. `idle_duty` — the share of genuinely silent frames the
    VAD calls speech — is the one that lines up with the field observation of
    47% while nobody was talking."""
    f = SR * hop_ms // 1000
    nf = n // f
    if nf <= 0:
        return {}
    pred = np.zeros(nf, dtype=bool)
    for a, b in spans:
        pred[min(nf, a // f):min(nf, -(-b // f))] = True
    ref = np.zeros(nf, dtype=bool)
    for _, ua, ub in utts:
        ref[min(nf, max(0, ua) // f):min(nf, -(-ub // f))] = True
    region = np.zeros(nf, dtype=bool)
    region[min(nf, max(0, lo) // f):min(nf, max(0, hi) // f)] = True
    p, t = pred[region], ref[region]
    tp = int((p & t).sum())
    fp = int((p & ~t).sum())
    fn = int((~p & t).sum())
    tn = int((~p & ~t).sum())
    out = {"tp_s": round(tp * hop_ms / 1000, 2), "fp_s": round(fp * hop_ms / 1000, 2),
           "fn_s": round(fn * hop_ms / 1000, 2), "tn_s": round(tn * hop_ms / 1000, 2),
           "idle_duty": round(fp / max(1, fp + tn), 4)}
    if tp + fn:
        out["recall"] = round(tp / (tp + fn), 4)
    if tp + fp:
        out["precision"] = round(tp / (tp + fp), 4)
    if out.get("recall") and out.get("precision"):
        out["f1"] = round(2 * out["recall"] * out["precision"]
                          / (out["recall"] + out["precision"]), 4)
    return out


def score_noise(spans, lo: int, hi: int) -> dict:
    segs = [(a, b) for a, b in spans if b > lo and a < hi]
    secs = sum(b - a for a, b in segs) / SR
    span_s = max(1e-6, (hi - lo) / SR)
    return {"fp": len(segs), "fp_s": round(secs, 2),
            "fp_per_min": round(len(segs) * 60.0 / span_s, 2),
            "duty": round(secs / span_s, 4)}


def hits(a: int, b: int, ua: int, ub: int) -> bool:
    """Does segment [a,b) actually cover utterance [ua,ub)?

    Genuine overlap, not a padded tolerance. The first version accepted a segment
    ending within 250 ms of an utterance's start, which on the 0.8 s gaps scored
    a correctly split pair of commands as a merge — the tolerance was larger than
    the thing it was supposed to resolve. Short utterances need only half their
    own length so 「停」 (0.3 s) is not held to the same 100 ms as a full sentence."""
    need = min(MIN_OVERLAP, max(1, (ub - ua) // 2))
    return min(b, ub) - max(a, ua) >= need


def score_speech(spans, utts, lo: int, hi: int) -> dict:
    """One segment delivers one turn, so one segment can claim only one utterance.

    The first version credited a hit to every utterance a segment overlapped,
    which scored a single 20 s cut swallowing nine commands as nine successes —
    the ASR pass then read that config back as 3/12 with a CER of 47. Whatever
    else a merged segment is, it is not the robot receiving both commands."""
    segs = [(a, b) for a, b in spans if b > lo and a < hi]
    overlaps = [[ui for ui, (_, ua, ub) in enumerate(utts)
                 if hits(a, b, ua, ub)]
                for a, b in segs]
    claimed = {}                                   # utterance index -> segment
    for si, ov in enumerate(overlaps):
        for ui in ov:
            if ui not in claimed:
                claimed[ui] = si
                break
    leads, tails, clipped = [], [], 0
    for ui, si in claimed.items():
        a, b = segs[si]
        _, ua, ub = utts[ui]
        leads.append((ua - a) * 1000.0 / SR)
        tails.append((b - ub) * 1000.0 / SR)
        # >30 ms of the onset missing. A few ms is reverb-tail bookkeeping; a
        # whole initial consonant is what turns 「停」 into nothing.
        if a > ua + int(0.03 * SR):
            clipped += 1
    return {
        "recall": len(claimed),
        "n_utt": len(utts),
        "miss": [t for i, (t, _, _) in enumerate(utts) if i not in claimed],
        # Segments that cut nothing useful: pure noise, or a second cut of an
        # utterance an earlier segment already delivered. Both are extra turns.
        "fp": len(segs) - len(set(claimed.values())),
        "merged": sum(1 for ov in overlaps if len(ov) > 1),
        "clipped": clipped,
        "lead_ms": round(float(np.median(leads)), 1) if leads else None,
        "lead_min_ms": round(float(np.min(leads)), 1) if leads else None,
        "tail_ms": round(float(np.median(tails)), 1) if tails else None,
    }


# --------------------------------------------------------------------------- #
# Grids
# --------------------------------------------------------------------------- #
def grid(engine: str) -> list:
    """fsmn takes only min_speech_s / pre_roll_s (make_vad drops the rest), so its
    grid is smaller by construction rather than by a table of identical rows."""
    gains = [0.0, 6.0, 15.0]
    if engine == "fsmn":
        axes = {"threshold": [0.5], "min_speech_s": [0.10, 0.25, 0.40, 0.60],
                "min_silence_s": [0.55], "pre_roll_s": [0.0, 0.3, 0.6, 0.9],
                "gain_db": gains}
    else:
        # 0.30 is in the list because at the far take's 5.7 dB consonant-band SNR
        # the stock 0.50 drops a third of the corpus, and pre_roll goes to 0.6
        # because 0.3 still clipped onsets on both engines.
        axes = {"threshold": [0.30, 0.50, 0.70, 0.85],
                "min_speech_s": [0.10, 0.25, 0.40],
                "min_silence_s": [0.40, 0.70],
                "pre_roll_s": [0.0, 0.30, 0.60],
                "gain_db": gains}
    keys = list(axes)
    return [dict(zip(keys, vals)) for vals in itertools.product(*axes.values())]


def prove_fsmn_ignores(rec, log) -> dict:
    """FsmnVad's docstring says threshold and min_silence_s are ignored. Show it
    instead of trusting the comment: same audio, wildly different values,
    byte-identical segments.

    Runs on a take with speech in it. An earlier version used the head of
    noise.wav, which after a quieter re-record yielded 0 segments both ways and
    reported "identical" — 0 == 0 proves nothing at all."""
    base = {"threshold": 0.1, "min_speech_s": 0.25, "min_silence_s": 0.10,
            "pre_roll_s": 0.0}
    alt = dict(base, threshold=0.95, min_silence_s=2.0)
    _, a, _ = replay(rec, "fsmn", base, 0.0)
    _, b, _ = replay(rec, "fsmn", alt, 0.0)
    same = a == b
    if not a and not b:
        log("  fsmn 忽略 threshold/min_silence 验证: 无效(两边都是 0 段)")
        return {"identical": None, "n_a": 0, "n_b": 0, "vacuous": True}
    log(f"  fsmn 忽略 threshold/min_silence 验证: {'一致' if same else '不一致!'} "
        f"({len(a)} vs {len(b)} 段)")
    return {"identical": same, "n_a": len(a), "n_b": len(b)}


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DIR)
    ap.add_argument("--engines", default="fsmn,silero,ten")
    ap.add_argument("--out", default=os.path.join(DIR, "sweep.json"))
    ap.add_argument("--limit", type=int, default=0, help="stop after N configs (smoke)")
    ap.add_argument("--check", action="store_true",
                    help="only align and report levels — is this recording usable?")
    args = ap.parse_args()

    def log(msg=""):
        print(msg, flush=True)

    with open(os.path.join(args.dir, "corpus.json")) as fh:
        manifest = json.load(fh)

    noise = read_wav(os.path.join(args.dir, "noise.wav"))
    corpus = read_wav(os.path.join(args.dir, "corpus.wav"))
    takes = {}
    for name in ("near", "far"):
        p = os.path.join(args.dir, f"{name}.wav")
        if not os.path.exists(p):
            continue
        x = read_wav(p)
        off, z = align(x, corpus)
        utts, lo, hi = truth(manifest, off)
        snr = check_alignment(x, utts, lo, hi)
        band = band_snr(x, utts, lo, hi)
        takes[name] = {"x": x, "utts": utts, "lo": max(0, lo), "hi": min(len(x), hi),
                       "snr": snr, "band_snr": band, "offset": off,
                       "speech": _masks(len(x), utts, lo, hi)[0]}
        log(f"{name}.wav {len(x)/SR:.1f}s  对齐偏移 {off/SR:+.2f}s (z={z:.0f})  "
            f"语音/静音 {snr:.1f} dB  1-3.4k 频段 {band:+.1f} dB  "
            f"中位电平 {np.median(frame_db(x)):+.1f} dBFS")
        if band < 4.0:
            log(f"  !! {name}: 辅音频段信噪比只有 {band:.1f} dB,录音本身不合格 "
                f"— 调参结果无意义,请提高播放音量或缩短距离")
    if not takes:
        raise SystemExit("没有 near/far 录音")
    nd = frame_db(noise)
    log(f"noise.wav {len(noise)/SR:.1f}s  中位 {np.median(nd):+.1f} dBFS  "
        f"p95 {np.percentile(nd, 95):+.1f}  峰 {nd.max():+.1f}")
    log()

    results = {"manifest": os.path.join(args.dir, "corpus.json"), "rows": [],
               "levels": {"noise_p50": float(np.median(nd)),
                          "noise_p95": float(np.percentile(nd, 95))},
               "checks": {}}
    for name, t in takes.items():
        results["levels"][f"{name}_snr_db"] = round(t["snr"], 1)
        results["levels"][f"{name}_band_snr_db"] = round(t["band_snr"], 1)

    if args.check:
        return 0 if all(t["band_snr"] >= 4.0 for t in takes.values()) else 2

    engines = [e for e in args.engines.split(",") if e]
    avail = vv.availability(os.path.join(VOICE, "models"))
    for e in engines:
        if not avail.get(e):
            raise SystemExit(f"引擎不可用: {e} — {avail}")

    if "fsmn" in engines:
        probe = takes.get("near") or next(iter(takes.values()))
        results["checks"]["fsmn_ignores"] = prove_fsmn_ignores(probe["x"], log)
        log()

    t_start = time.time()
    for engine in engines:
        configs = grid(engine)
        if args.limit:
            configs = configs[:args.limit]
        log(f"=== {engine}: {len(configs)} 组 × {1 + len(takes)} 段录音 ===")
        for i, cfg in enumerate(configs, 1):
            gain = cfg["gain_db"]
            params = {k: v for k, v in cfg.items() if k != "gain_db"}
            t0 = time.time()
            _, sp, lost = replay(noise, engine, params, gain)
            # Spans ride along in the result file. Any metric anyone thinks of
            # later is then a re-read of this JSON, not another 50-minute sweep.
            row = {"engine": engine, **cfg, "lost": lost,
                   "noise": score_noise(sp, 0, len(noise)),
                   "spans": {"noise": sp}}
            row["noise"]["frames"] = frame_metrics(sp, [], 0, len(noise), len(noise))
            for name, t in takes.items():
                _, sp, l = replay(t["x"], engine, params, gain)
                row["lost"] += l
                row["spans"][name] = sp
                row[name] = score_speech(sp, t["utts"], t["lo"], t["hi"])
                row[name]["clip"] = round(clip_frac(t["x"], gain, t["speech"]), 4)
                row[name]["frames"] = frame_metrics(sp, t["utts"], t["lo"], t["hi"],
                                                    len(t["x"]))
            row["secs"] = round(time.time() - t0, 1)
            if row["lost"]:
                log(f"  !! {row['lost']} 段无法在源音频中定位 — 打分不可信")
            results["rows"].append(row)
            near = row.get("near", {})
            nf = row["noise"].get("frames", {})
            log(f"  [{i:3d}/{len(configs)}] thr={cfg['threshold']:.2f} "
                f"msp={cfg['min_speech_s']:.2f} msl={cfg['min_silence_s']:.2f} "
                f"pre={cfg['pre_roll_s']:.2f} gain={gain:+.0f}  "
                f"noise_fp={row['noise']['fp']:<3d} "
                f"空闲误判={nf.get('idle_duty', 0)*100:4.1f}%  "
                f"near={near.get('recall')}/{near.get('n_utt')} "
                f"fp={near.get('fp')} F1={near.get('frames', {}).get('f1')}  "
                f"[{row['secs']}s]")
        log()

    with open(args.out, "w") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)
    log(f"{len(results['rows'])} 组写入 {args.out}  用时 {(time.time()-t_start)/60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

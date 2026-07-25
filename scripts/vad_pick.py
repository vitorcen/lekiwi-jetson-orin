#!/usr/bin/env python3
"""Read a sweep result and decide: best config per engine, and which parameters
can share one value across engines.

Ranking is lexicographic, not a weighted sum. Weights would let two extra false
triggers buy back a dropped 「停」, and they never should — a robot that misses a
stop command is broken in a way that a robot which occasionally mishears is not.

  1. missed utterances          (near + far)
  2. merged segments            (two commands cut as one)
  3. clipped onsets             (>30 ms of the first syllable gone)
  4. false triggers             (noise.wav + gaps in near/far)
  5. min_silence_s              (pure end-of-speech latency)
  6. min_speech_s               (start-of-speech latency)

"Can this axis share one value" is answered by forcing it: for each candidate
value, take each engine's best row constrained to that value, and report what the
worst-off engine loses against its own optimum. Zero loss means unify it.

Usage:
  scripts/vad_pick.py sweep.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

AXES = ["threshold", "min_speech_s", "min_silence_s", "pre_roll_s", "gain_db"]
TAKES = ["near", "far"]
# make_vad() hands fsmn only min_speech_s and pre_roll_s; the sweep proves the
# other two change nothing. An engine that ignores an axis has no opinion about
# its shared value, so it is left out of that axis's comparison rather than
# silently voting for whatever single value its grid happened to carry.
IGNORED = {"fsmn": {"threshold", "min_silence_s"}}


def key(row: dict):
    miss = merged = clipped = fp = 0
    for t in TAKES:
        s = row.get(t)
        if not s:
            continue
        miss += s["n_utt"] - s["recall"]
        merged += s["merged"]
        clipped += s["clipped"]
        fp += s["fp"]
    fp += row["noise"]["fp"]
    return (miss, merged, clipped, fp, row["min_silence_s"], row["min_speech_s"])


def describe(row: dict) -> str:
    k = key(row)
    parts = [f"thr={row['threshold']:.2f}", f"msp={row['min_speech_s']:.2f}",
             f"msl={row['min_silence_s']:.2f}", f"pre={row['pre_roll_s']:.2f}",
             f"gain={row['gain_db']:+.0f}"]
    clip = max((row[t].get("clip", 0.0) for t in TAKES if row.get(t)), default=0.0)
    idle = row["noise"].get("frames", {}).get("idle_duty")
    f1s = [row[t].get("frames", {}).get("f1") for t in TAKES if row.get(t)]
    f1s = [v for v in f1s if v is not None]
    return (f"{' '.join(parts):<52s} 漏{k[0]} 并{k[1]} 切{k[2]} 误{k[3]:<3d} "
            + " ".join(
                f"{t} {row[t]['recall']}/{row[t]['n_utt']}fp{row[t]['fp']}"
                for t in TAKES if row.get(t))
            + (f"  F1 {min(f1s):.2f}" if f1s else "")
            + (f"  空闲误判 {idle*100:.1f}%" if idle is not None else "")
            + (f"  削顶{clip*100:.1f}%" if clip > 0.001 else ""))


def best_of(rows):
    return min(rows, key=key) if rows else None


def shareable(by_engine: dict, axis: str):
    """For each value of `axis`: the per-engine best rows constrained to it, and
    the worst regression against that engine's unconstrained best."""
    by_engine = {e: rows for e, rows in by_engine.items()
                 if axis not in IGNORED.get(e, ())}
    if not by_engine:
        return []
    solo = {e: key(best_of(rows)) for e, rows in by_engine.items()}
    values = sorted({r[axis] for rows in by_engine.values() for r in rows})
    out = []
    for v in values:
        per = {}
        ok = True
        for e, rows in by_engine.items():
            sub = [r for r in rows if r[axis] == v]
            if not sub:
                ok = False
                break
            per[e] = key(best_of(sub))
        if not ok:
            continue
        # Regression is measured on the hard terms only (miss/merge/clip/fp);
        # the latency tiebreakers are not a quality loss.
        loss = {e: tuple(a - b for a, b in zip(per[e][:4], solo[e][:4]))
                for e in per}
        worst = max(sum(l) for l in loss.values())
        out.append((v, worst, loss))
    return sorted(out, key=lambda t: t[1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sweep", nargs="?", default="sweep.json")
    ap.add_argument("--top", type=int, default=8)
    args = ap.parse_args()

    with open(args.sweep) as fh:
        data = json.load(fh)
    rows = data["rows"]
    if not rows:
        print("空结果", file=sys.stderr)
        return 1

    lv = data.get("levels", {})
    print(f"录音电平: noise 中位 {lv.get('noise_p50', float('nan')):+.1f} dBFS  "
          + "  ".join(f"{t} 语音/静音 {lv.get(f'{t}_snr_db')} dB" for t in TAKES))
    chk = data.get("checks", {}).get("fsmn_ignores")
    if chk:
        print(f"fsmn 忽略 threshold/min_silence: "
              f"{'已验证一致' if chk['identical'] else '不一致(与文档矛盾!)'}")
    print()

    by_engine = defaultdict(list)
    for r in rows:
        by_engine[r["engine"]].append(r)

    for e, rs in by_engine.items():
        rs.sort(key=key)
        print(f"=== {e} ({len(rs)} 组) 前 {args.top} ===")
        for r in rs[:args.top]:
            print("  " + describe(r))
        clean = [r for r in rs if key(r)[:3] == (0, 0, 0)]
        print(f"  零漏零并零切: {len(clean)}/{len(rs)} 组;"
              f" 其中最少误触发 {min((key(r)[3] for r in clean), default='-')}")
        print()

    print("=== 各引擎最优 ===")
    best = {e: best_of(rs) for e, rs in by_engine.items()}
    for e, r in best.items():
        print(f"  {e:<7s} " + describe(r))
    print()

    print("=== 各引擎最优的帧级准确率 ===")
    print(f"  {'引擎':<8s} {'录音':<6s} {'精确率':>7s} {'召回率':>7s} {'F1':>6s} "
          f"{'漏掉语音':>9s} {'空闲误判':>9s}")
    for e, r in best.items():
        for t in TAKES + ["noise"]:
            fr = (r.get(t) or {}).get("frames") or {}
            if not fr:
                continue
            print(f"  {e:<8s} {t:<6s} "
                  f"{fr.get('precision', float('nan')):>7.3f} "
                  f"{fr.get('recall', float('nan')):>7.3f} "
                  f"{fr.get('f1', float('nan')):>6.3f} "
                  f"{fr.get('fn_s', 0):>8.1f}s {fr['idle_duty']*100:>8.1f}%")
    print()

    print("=== 参数能否统一 ===")
    print("  (强制所有引擎用同一个值,看最差的那个引擎比它自己的最优退化多少)")
    for axis in AXES:
        ranked = shareable(by_engine, axis)
        skip = [e for e in by_engine if axis in IGNORED.get(e, ())]
        if not ranked:
            continue
        v, worst, loss = ranked[0]
        tag = "统一" if worst == 0 else f"退化 {worst}"
        detail = ", ".join(f"{e}{'/'.join(str(x) for x in l)}"
                           for e, l in loss.items() if any(l))
        own = "  各自最优: " + " ".join(f"{e}={best[e][axis]:g}"
                                        for e in best if e not in skip)
        print(f"  {axis:<14s} 最佳共用值 {v:<6g} [{tag}]"
              + (f"  ({detail})" if detail else "") + own
              + (f"  (不适用: {'/'.join(skip)})" if skip else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

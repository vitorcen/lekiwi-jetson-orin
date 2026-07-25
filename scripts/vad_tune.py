#!/usr/bin/env python3
"""Tune the board's VAD/ASR from this Mac, over the real acoustic path.

Method, and why it is shaped this way:

  * The Mac PLAYS a fixed script through its speakers; the board's mic hears it.
    Earlier attempts compared settings while the room noise drifted 30 dB between
    runs, which made a higher threshold score *worse* than a lower one — that
    experiment compared environments, not parameters. Same audio every pass is
    the only way the numbers mean anything.
  * The board sits in the DEBUG transcription bench (`POST /asr_debug {on:1}`),
    which segments and transcribes but never reaches the brain or the speaker. A
    robot that answers during a VAD test contaminates the next measurement with
    its own echo.
  * Every utterance is followed by a silent gap. Segments cut during a gap are
    false triggers; the utterances themselves measure what tightening costs.

Only knobs the engine actually reads are worth sweeping. On `fsmn`, `threshold`
and `min_silence_s` are ignored outright (see FsmnVad's docstring) — sweeping
them produces a table of identical rows and a false sense of having tuned.

STATUS: scaffolding, NOT yet validated end to end. The next iteration replaces
macOS `say` with Matcha running on the Mac (same TTS the robot uses), so treat
the numbers it prints as unproven until that pass is done.

Usage:
  scripts/vad_tune.py --ip 192.168.13.189
  scripts/vad_tune.py --ip ... --engine silero      # threshold becomes live
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# (spoken text, seconds of silence to leave after it)
SCRIPT = [
    ("停",                       3.0),   # shortest real command
    ("往前走一点点",              3.0),
    ("你看看前面有什么东西",       3.0),
    ("现在电池还有多少电",         4.0),   # longer trailing gap: pure noise window
]

# fsmn reads only these two; gain sits in front of the VAD either way.
SWEEP = [
    {"min_speech_s": 0.10, "gain_db": 15.0},      # current setting
    {"min_speech_s": 0.30, "gain_db": 15.0},
    {"min_speech_s": 0.50, "gain_db": 15.0},
    {"min_speech_s": 0.30, "gain_db": 6.0},
    {"min_speech_s": 0.50, "gain_db": 6.0},
    {"min_speech_s": 0.30, "gain_db": 0.0},
]


class Board:
    def __init__(self, ip: str, token: str):
        self.base = f"http://{ip}:8092"
        self.token = token

    def _req(self, method: str, path: str, body=None, timeout=15):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return {"error": e.code, "detail": e.read().decode()[:200]}
        except Exception as exc:
            return {"error": repr(exc)}

    get = lambda self, p, **kw: self._req("GET", p, **kw)          # noqa: E731
    post = lambda self, p, b, **kw: self._req("POST", p, b, **kw)  # noqa: E731

    def debug(self, on: bool):
        return self.post("/asr_debug", {"on": 1 if on else 0})

    def set_vad(self, engine: str, params: dict) -> bool:
        self.post("/config", {"axis": "vad",
                              "value": {"engine": engine, **params},
                              "ephemeral": True})
        for _ in range(40):
            time.sleep(1)
            if self.get("/health").get("vad_engine") == engine:
                time.sleep(1)
                return True
        return False

    def set_gain(self, db: float):
        self.post("/config", {"axis": "audio", "value": {"gain_db": db},
                              "ephemeral": True})
        time.sleep(0.5)

    def tail(self, since: int):
        return self.get(f"/asr_debug/tail?since={since}")


def say_to_file(text: str, path: Path) -> float:
    """macOS `say` -> wav. Returns duration. Tingting, not the newer neural zh
    voices: those accept -o and then silently write a ~0.02 s stub."""
    subprocess.run(["say", "-v", "Tingting", "-r", "180",
                    "--file-format=WAVE", "--data-format=LEI16@16000",
                    "-o", str(path), text], check=True, capture_output=True)
    import wave
    with wave.open(str(path), "rb") as w:
        secs = w.getnframes() / w.getframerate()
    if secs < 0.3:
        raise RuntimeError(f"`say` produced only {secs:.2f}s for {text!r}")
    return secs


def run_pass(board: Board, clips, label: str) -> dict:
    """Play the whole script once; return what the board cut and decoded."""
    t0 = board.tail(0)
    since = t0.get("last_seq", 0) if isinstance(t0, dict) else 0

    marks = []            # (utterance, play_start, play_end)
    t_start = time.time()
    for (text, gap), (path, secs) in zip(SCRIPT, clips):
        s = time.time() - t_start
        subprocess.run(["afplay", str(path)], check=True)
        marks.append((text, s, time.time() - t_start))
        time.sleep(gap)
    time.sleep(1.5)       # let the last segment close and decode

    tail = board.tail(since)
    events = tail.get("events", []) if isinstance(tail, dict) else []
    segs = [e for e in events if e.get("kind") == "seg" or "outcome" in e]

    # A segment is "expected" if it overlaps a played utterance; the board and the
    # Mac share no clock, so this is by ordering, not timestamps: the first N
    # segments in play order are matched greedily to the N utterances.
    return {"label": label, "n_segs": len(segs),
            "texts": [e.get("text", "") for e in segs],
            "durs": [e.get("dur_s") for e in segs],
            "outcomes": [e.get("outcome") for e in segs],
            "n_utterances": len(SCRIPT)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="192.168.13.189")
    ap.add_argument("--engine", default="fsmn")
    ap.add_argument("--token-file",
                    default="/home/jetson/work/lekiwi-jetson-orin/voice/token")
    args = ap.parse_args()

    tok = subprocess.run(["ssh", f"jetson@{args.ip}", f"cat {args.token_file}"],
                         capture_output=True, text=True, check=True).stdout.strip()
    board = Board(args.ip, tok)

    h = board.get("/health")
    if "error" in h:
        print(f"板子不可达: {h}", file=sys.stderr)
        return 1
    print(f"板子 OK  capture={h.get('capture_card')} vad={h.get('vad_engine')}")
    if args.engine == "fsmn":
        print("注意: fsmn 忽略 threshold 与 min_silence_s，只扫 min_speech_s / gain\n")

    tmp = Path(tempfile.mkdtemp())
    clips = []
    for text, _ in SCRIPT:
        p = tmp / f"{abs(hash(text))}.wav"
        clips.append((p, say_to_file(text, p)))
    print("测试脚本:")
    for (text, gap), (_, secs) in zip(SCRIPT, clips):
        print(f"  「{text}」 {secs:.1f}s  + 静音 {gap:.0f}s")
    total = sum(s for _, s in clips) + sum(g for _, g in SCRIPT)
    print(f"每轮约 {total:.0f}s × {len(SWEEP)} 组 ≈ {total*len(SWEEP)/60:.0f} 分钟\n")

    if not board.debug(True).get("debug"):
        print("无法进入 DEBUG 转写台", file=sys.stderr)
        return 1
    print("已进入 DEBUG 转写台(不进大脑、不播报)\n")

    results = []
    try:
        for cfg in SWEEP:
            params = {k: v for k, v in cfg.items() if k != "gain_db"}
            params.setdefault("min_silence_s", 0.5)
            params.setdefault("pre_roll_s", 0.45)
            params.setdefault("threshold", 0.5)
            if not board.set_vad(args.engine, params):
                print(f"切换超时,跳过 {cfg}")
                continue
            board.set_gain(cfg["gain_db"])
            label = f"min_speech={cfg['min_speech_s']:.2f} gain={cfg['gain_db']:+.0f}dB"
            print(f"--- {label} ---", flush=True)
            r = run_pass(board, clips, label)
            results.append(r)
            extra = r["n_segs"] - r["n_utterances"]
            print(f"    截出 {r['n_segs']} 段 (念了 {r['n_utterances']} 句, "
                  f"多出 {extra:+d})")
            for t, d, o in zip(r["texts"], r["durs"], r["outcomes"]):
                print(f"      {str(d):>5}s {o:<10s} {t[:30]}")
            print(flush=True)
    finally:
        board.debug(False)
        print("已退出 DEBUG 转写台(VAD/增益为 ephemeral,重启 daemon 即还原)")

    print("\n=== 汇总 ===")
    print(f"{'配置':32s} {'段数':>5s} {'多出':>5s} {'出字段数':>8s}")
    for r in results:
        got = sum(1 for t in r["texts"] if (t or "").strip())
        print(f"{r['label']:32s} {r['n_segs']:5d} "
              f"{r['n_segs']-r['n_utterances']:+5d} {got:8d}")
    print("\n目标: 「多出」尽量接近 0(没有噪音段), 同时「出字段数」保持 "
          f"{len(SCRIPT)}(真话没被吞)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

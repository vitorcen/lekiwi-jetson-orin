#!/usr/bin/env python3
"""Capture the fixed inputs the VAD sweep runs on: room noise, and the Matcha
corpus played from this Mac and heard by the robot's own microphone.

Run from the Mac. It stops voice-daemon (arecord wants the USB card to itself),
records with byte-identical arecord flags to the daemon's capture loop, and
restarts the daemon on the way out — including on Ctrl-C or a failure.

Three takes, all landing in /tmp/vadtune/ on the board:

  noise.wav  — the room with nobody speaking. This is where false triggers are
               counted; the field symptom was the VAD calling 47% of idle
               samples speech, and this is the recording that measures it.
  near.wav   — corpus at full level: the user speaking up close.
  far.wav    — the same corpus attenuated 12 dB before playback: the user across
               the room. A setting that only works at one SNR is not a setting.

Playback level is attenuated digitally rather than by the system volume knob so
a re-run reproduces the same file, not the same slider position.

Usage:
  ~/.cache/lekiwi-mac-tts/venv/bin/python scripts/vad_record.py --ip 192.168.13.189
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import wave

import numpy as np

SR = 16000
CORPUS = os.path.expanduser("~/.cache/lekiwi-mac-tts/corpus")
BOARD_DIR = "/tmp/vadtune"
NOISE_S = 90.0
# Attenuations applied to corpus.wav before playback, and the name each take gets.
# -6 dB, not -12: at -12 the consonant band fell to 1.2 dB above the room floor
# while the broadband level still read a comfortable 11.7 dB. High frequencies
# drop into the noise long before the meter says anything is wrong.
TAKES = [("near", 0.0), ("far", -6.0)]


def sh(*args, **kw):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kw)


def osa(script: str) -> str:
    return sh("osascript", "-e", script).stdout.strip()


class Board:
    def __init__(self, host: str):
        self.host = host

    def run(self, cmd: str, check=True, timeout=120):
        return subprocess.run(["ssh", self.host, cmd], check=check,
                              capture_output=True, text=True, timeout=timeout)

    def daemon(self, action: str):
        self.run(f"systemctl --user {action} voice-daemon", check=False)

    def arecord_bg(self, seconds: float, dest: str) -> subprocess.Popen:
        """Same flags as daemon._capture_loop. -d bounds it so a lost ssh never
        leaves a recorder holding the card."""
        cmd = (f"arecord -D plughw:CARD=MCP01 -f S16_LE -r 16000 -c 1 "
               f"-d {int(seconds)} -q {shlex.quote(dest)}")
        return subprocess.Popen(["ssh", self.host, cmd],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def attenuate(src: str, dst: str, db: float) -> float:
    with wave.open(src, "rb") as w:
        n, sr = w.getnframes(), w.getframerate()
        x = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float32)
    if db:
        x = x * (10.0 ** (db / 20.0))
    with wave.open(dst, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x.astype("<i2").tobytes())
    return n / sr


def dbfs(x: np.ndarray) -> float:
    if x.size == 0:
        return -99.0
    return float(20 * np.log10(max(float(np.sqrt(np.mean(x * x))), 1e-9)))


def probe(board: Board, path: str) -> dict:
    """Level stats, computed on the board so nothing large crosses the wire."""
    code = (
        "import wave,numpy as np,json,sys\n"
        f"w=wave.open({path!r},'rb');n=w.getnframes()\n"
        "x=np.frombuffer(w.readframes(n),dtype='<i2').astype(np.float32)/32768.\n"
        "f=160;m=(len(x)//f)*f\n"
        "r=np.sqrt((x[:m].reshape(-1,f)**2).mean(axis=1)+1e-12)\n"
        "d=20*np.log10(np.maximum(r,1e-9))\n"
        "print(json.dumps({'secs':n/16000.,'p50':float(np.percentile(d,50)),"
        "'p95':float(np.percentile(d,95)),'p99':float(np.percentile(d,99)),"
        "'peak':float(d.max())}))\n")
    out = board.run(f"~/work/lekiwi-jetson-orin/voice/.venv/bin/python - <<'PYEOF'\n"
                    f"{code}PYEOF")
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="192.168.13.189")
    ap.add_argument("--user", default="jetson")
    ap.add_argument("--corpus", default=CORPUS)
    ap.add_argument("--takes", default="noise,near,far",
                    help="which of noise/near/far to (re-)record")
    ap.add_argument("--noise-secs", type=float, default=NOISE_S,
                    help="90 s sampled the room once and got a bang; the next 90 s "
                         "got nothing and every config scored a perfect zero. "
                         "Minutes, not seconds, or this axis measures the weather")
    ap.add_argument("--volume", type=int, default=100,
                    help="macOS output volume during playback; restored on exit. "
                         "The first take was made at 56 and the speech landed "
                         "BELOW the room noise above 1 kHz — this is not a knob "
                         "to be shy with")
    args = ap.parse_args()
    want = {t for t in args.takes.split(",") if t}

    wav_src = os.path.join(args.corpus, "corpus.wav")
    man_src = os.path.join(args.corpus, "corpus.json")
    for p in (wav_src, man_src):
        if not os.path.exists(p):
            print(f"缺少 {p},先跑 scripts/vad_corpus.py", file=sys.stderr)
            return 1

    board = Board(f"{args.user}@{args.ip}")
    board.run(f"mkdir -p {BOARD_DIR}")
    sh("scp", "-q", man_src, f"{board.host}:{BOARD_DIR}/corpus.json")
    sh("scp", "-q", wav_src, f"{board.host}:{BOARD_DIR}/corpus.wav")

    takes = []
    for name, db in TAKES:
        if name not in want:
            continue
        dst = os.path.join(args.corpus, f"play-{name}.wav")
        secs = attenuate(wav_src, dst, db)
        takes.append((name, db, dst, secs))

    sweep = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vad_sweep.py")
    sh("scp", "-q", sweep, f"{board.host}:{BOARD_DIR}/vad_sweep.py")

    vol0 = osa("output volume of (get volume settings)")
    print("停 voice-daemon(录完自动恢复)")
    board.daemon("stop")
    time.sleep(2.0)
    try:
        if "noise" in want:
            print(f"\n[房间噪声] {args.noise_secs:.0f}s —— 正常活动即可,只是别对着机器人说话")
            for i in range(5, 0, -1):
                print(f"  {i} ...", end="\r", flush=True)
                time.sleep(1)
            p = board.arecord_bg(args.noise_secs, f"{BOARD_DIR}/noise.wav")
            rc = p.wait(timeout=args.noise_secs + 60)
            if rc != 0:
                print(f"arecord 失败: {p.stderr.read().decode()[:200]}", file=sys.stderr)
                return 1
            st = probe(board, f"{BOARD_DIR}/noise.wav")
            print(f"  noise.wav {st['secs']:.1f}s  中位 {st['p50']:+.1f} dBFS  "
                  f"p95 {st['p95']:+.1f}  峰 {st['peak']:+.1f}")

        if takes:
            osa(f"set volume output volume {args.volume}")
            print(f"\nMac 输出音量 {vol0} → {args.volume}")
        for name, db, path, secs in takes:
            print(f"\n[语料 {name}] {db:+.0f} dB, {secs:.0f}s")
            p = board.arecord_bg(secs + 6, f"{BOARD_DIR}/{name}.wav")
            time.sleep(2.5)                      # let ALSA actually open the card
            sh("afplay", path)
            rc = p.wait(timeout=secs + 40)
            if rc != 0:
                print(f"arecord 失败: {p.stderr.read().decode()[:200]}", file=sys.stderr)
                return 1
            st = probe(board, f"{BOARD_DIR}/{name}.wav")
            print(f"  {name}.wav {st['secs']:.1f}s  中位 {st['p50']:+.1f} dBFS  "
                  f"p95 {st['p95']:+.1f}  p99 {st['p99']:+.1f}  峰 {st['peak']:+.1f}")
    finally:
        osa(f"set volume output volume {vol0}")
        print("\n恢复 voice-daemon")
        board.daemon("start")

    # Verdict before anything is swept: a take whose consonant band is buried in
    # room noise cannot be tuned on, and finding that out after a 30-minute sweep
    # is how you end up with a table of confident nonsense.
    if takes:
        print("\n=== 录音可用性检查 ===")
        r = board.run(f"~/work/lekiwi-jetson-orin/voice/.venv/bin/python "
                      f"{BOARD_DIR}/vad_sweep.py --check", check=False, timeout=300)
        print("\n".join(l for l in (r.stdout + r.stderr).splitlines()
                        if "pkg_resources" not in l))
        if r.returncode != 0:
            print("录音不合格 —— 别急着扫参", file=sys.stderr)
            return 2

    print(f"\n录音在板上 {BOARD_DIR}/ —— 下一步 scripts/vad_sweep.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

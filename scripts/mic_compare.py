#!/usr/bin/env python3
"""Is the robot's microphone the problem? Record the same playback on the board's
mic and this Mac's mic at the same instant, then decode both with the same ASR.

Why simultaneous: playing the corpus twice and comparing would compare two
moments of the room, not two microphones. One playback, two recorders, identical
source waveform — the only variable left is the mic (and its distance, which is
why the level report matters as much as the transcript).

Two directions, because the speaker sits next to one of the mics either way:

  --source mac    Mac plays. Board mic is far, Mac mic is right next to the
                  speaker. This is the direction the tuning corpus was recorded in.
  --source board  Board plays the same file through MCP01. Now the roles swap.
                  Caveat worth knowing before reading the numbers: MCP01 is a
                  speakerphone — mic and speaker in one USB device — so in this
                  direction its own echo cancellation may be acting on the
                  recording, and what it measures is the device, not the capsule.

The verdict is not "which recording has the lower CER". It is whether the board
mic decodes worse THAN THE MAC MIC AT THE SAME BAND SNR. A worse number at a
worse SNR just means it was further away.

Usage:
  scripts/mic_compare.py --source mac
  scripts/mic_compare.py --source board
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
import wave

SR = 16000
CORPUS = os.path.expanduser("~/.cache/lekiwi-mac-tts/corpus")
BOARD_DIR = "/tmp/vadtune"
VENV = "~/work/lekiwi-jetson-orin/voice/.venv/bin/python"


def sh(*args, **kw):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kw)


def wav_secs(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


def mac_record(seconds: float, dest: str, device):
    """PortAudio -> 16 kHz mono wav, the same format the board captures in.

    Not ffmpeg/avfoundation: it drops about 23% of samples on this machine, at
    the native rate with no resampling in the chain (20.5 s of wall clock came
    back as 15.9 s of audio, reproducibly). A recording missing a quarter of its
    samples is not merely short, its timeline is spliced — envelope alignment has
    nothing to lock onto. sounddevice measured 0.992 of wall clock.

    macOS may still be applying its own voice processing to the built-in mic and
    there is no flag here that turns it off, so this side is 'a laptop mic as
    macOS hands it over' — which is what a comparison against it is worth.

    Returns a started recording; call mac_finish() to block and write it out."""
    import sounddevice as sd
    if device is not None:
        sd.default.device = (device, None)
    buf = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="int16")
    return buf, dest


def mac_finish(rec) -> float:
    import sounddevice as sd
    buf, dest = rec
    sd.wait()
    with wave.open(dest, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(buf.tobytes())
    return len(buf) / SR


def board_record(host: str, seconds: float, dest: str) -> subprocess.Popen:
    cmd = (f"arecord -D plughw:CARD=MCP01 -f S16_LE -r 16000 -c 1 "
           f"-d {int(seconds)} -q {shlex.quote(dest)}")
    return subprocess.Popen(["ssh", host, cmd],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def board_play(host: str, path: str) -> subprocess.Popen:
    return subprocess.Popen(["ssh", host, f"aplay -D plughw:CARD=MCP01 -q {shlex.quote(path)}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="192.168.13.189")
    ap.add_argument("--user", default="jetson")
    ap.add_argument("--source", choices=["mac", "board"], default="mac")
    ap.add_argument("--mac-device", type=int, default=None,
                    help="PortAudio input index; default is the system input")
    ap.add_argument("--volume", type=int, default=100)
    args = ap.parse_args()

    host = f"{args.user}@{args.ip}"
    wav_src = os.path.join(CORPUS, "corpus.wav")
    if not os.path.exists(wav_src):
        print(f"缺少 {wav_src}", file=sys.stderr)
        return 1
    secs = wav_secs(wav_src)
    tag = f"mic-{args.source}"
    mac_out = os.path.join(CORPUS, f"{tag}-macmic.wav")

    vol0 = sh("osascript", "-e", "output volume of (get volume settings)").stdout.strip()
    sh("ssh", host, "systemctl --user stop voice-daemon")
    time.sleep(2.0)
    try:
        if args.source == "mac":
            sh("osascript", "-e", f"set volume output volume {args.volume}")
        rec_m = mac_record(secs + 14, mac_out, args.mac_device)
        rec_b = board_record(host, secs + 12, f"{BOARD_DIR}/{tag}-boardmic.wav")
        time.sleep(4.0)                      # both recorders actually open
        print(f"播放 ({args.source}) {secs:.0f}s ...", flush=True)
        if args.source == "mac":
            sh("afplay", os.path.join(CORPUS, "play-near.wav")
               if os.path.exists(os.path.join(CORPUS, "play-near.wav")) else wav_src)
        else:
            p = board_play(host, f"{BOARD_DIR}/corpus.wav")
            if p.wait(timeout=secs + 60) != 0:
                print(f"aplay 失败: {p.stderr.read().decode()[:200]}", file=sys.stderr)
        got = mac_finish(rec_m)
        print(f"  macmic {got:.1f}s  (语料 {secs:.1f}s)")
        if got < secs + 4:
            print("mac 录音短于语料+前置,无法对齐", file=sys.stderr)
            return 2
        if rec_b.wait(timeout=secs + 60) != 0:
            print(f"board 录音失败: {rec_b.stderr.read().decode()[:300]}", file=sys.stderr)
            return 1
    finally:
        sh("osascript", "-e", f"set volume output volume {vol0}")
        sh("ssh", host, "systemctl --user start voice-daemon")

    sh("scp", "-q", mac_out, f"{host}:{BOARD_DIR}/{tag}-macmic.wav")
    print(f"\n两份录音都在板上: {BOARD_DIR}/{tag}-boardmic.wav  {BOARD_DIR}/{tag}-macmic.wav")
    print(f"下一步: ssh {host} '{VENV} {BOARD_DIR}/vad_asr.py --compare {tag}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

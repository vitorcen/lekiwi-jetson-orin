#!/usr/bin/env python3
"""Render the Voice page's 电脑播报 script to audio files, on this Mac.

One process renders a WHOLE script, because both non-trivial engines have a
per-process cost that dwarfs per-line cost: f5 loads a 1.4GB model, edge opens a
TLS session. Rendering line-by-line from Rust would pay it 14 times.

Contract — a JSON job on stdin, a JSON result on stdout:

    {"engine": "edge"|"f5", "voice": "zh-CN-XiaoxiaoNeural",
     "seed": 1234, "cache_dir": "/…", "items": [{"text": "停", "out": "/….wav"}]}
    -> {"ok": true, "rendered": 3, "cached": 11, "ref": "/….wav"}

`out` paths are chosen (and cache-hit-checked) by the caller; this script only
fills the ones that do not exist yet.

## Dependencies are passed in, not declared

No PEP 723 block on purpose: the two engines need different sets, and declaring
the union would drag f5's 1.4GB model resolver into every network-only edge run.
The caller (gui/src-tauri/src/main.rs `mac_tts_render`) picks:

    edge:  uv run --with edge-tts --with numpy            scripts/mac_tts_render.py
    f5:    uv run --with f5-tts-mlx --with edge-tts --with numpy  scripts/mac_tts_render.py

f5 needs edge-tts too — its "voice" IS an edge clip (see below). Run either line
by hand with a JSON job on stdin to reproduce anything the GUI does.

## Why the two engines share one voice name

`f5` is zero-shot cloning: its "voice" is a reference clip, not a model. The
reference we clone is an edge voice — so `zh-CN-XiaoxiaoNeural` names the same
sound under both engines, and the engine choice only decides "call the network
every time" vs "call it once, then stay offline". One name space, no mapping
table for the operator to hold in their head.

## Why f5 needs an explicit duration (measured, not guessed)

f5-tts-mlx wants the TOTAL length (reference + generated) up front. Both of its
convenient defaults are wrong for command-length Chinese:

  * no duration at all -> 0.08s of −83dBFS silence for a 5-char line
  * estimate_duration=True -> collapses as text gets short: 1 char came out
    −92dBFS (silence), 2 chars −42dBFS (inaudible)

and a hand-picked 0.32s/char over-allocated, so the model stretched speech to
fill the slot and the result audibly broke up between syllables. What works is
deriving the rate from the reference clip's OWN speaking rate; across 1..13
characters that held 0.18-0.23s/char with no stretching. It is not a magic
number — change the reference and it follows.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import wave

import numpy as np

# Head/tail silence is not free here. The script declares the gap between lines,
# and a VAD run reads that gap as a measured variable (0.8-10s, deliberately
# unequal). edge pads "停" out to 1.78s for ~0.4s of speech — 1.4s of padding
# that would silently land on top of every gap. Trim to the speech extent and
# leave a fixed, known margin instead.
TRIM_FLOOR_DB = 40.0      # below (peak - this) counts as silence
TRIM_MARGIN_S = 0.05      # keep a hair so plosive onsets are not clipped

# Peak-normalise on the way out. Two reasons, both measured on 2026-07-26:
#
#   * Loudness is per-voice. Ten edge voices rendering the same line came out
#     between -6.1 and -3.5 dBFS, and `say` sits lower still. Without this the
#     acoustic level at the robot's microphone depends on which voice you picked,
#     so the volume knob means a different thing on every line.
#   * There is nothing to spend the headroom on. This audio exists to be played
#     across a room into an MCP01 array whose recorded peak came back at
#     -25 dBFS — the whole chain is starved for level, and 3-5 dB sitting unused
#     above the waveform is 3-5 dB the recogniser never sees.
#
# -1 dBFS, not 0: leaves room for the resampler's intersample overshoot.
NORM_PEAK_DB = -1.0


def trim_and_level(path: str) -> None:
    """Cut head/tail silence, then peak-normalise. One read, one write."""
    with wave.open(path, "rb") as w:
        n, sr, ch, width = w.getnframes(), w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    if width != 2 or n == 0:
        return
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    hop = max(1, int(0.01 * sr))
    frames = x[:len(x) // hop * hop].reshape(-1, hop)
    if not frames.size:
        return
    e = 20 * np.log10(np.maximum(np.sqrt((frames ** 2).mean(axis=1)), 1e-9))
    idx = np.where(e > e.max() - TRIM_FLOOR_DB)[0]
    if idx.size == 0:
        return                                    # all silence: leave it alone
    m = int(TRIM_MARGIN_S * sr)
    a = max(0, idx[0] * hop - m)
    b = min(len(x), (idx[-1] + 1) * hop + m)
    y = x[a:b]
    peak = np.abs(y).max()
    if peak > 0:
        y = y * (32768.0 * 10 ** (NORM_PEAK_DB / 20.0) / peak)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(y, -32768, 32767).astype("<i2").tobytes())

# The reference sentence f5 clones from. Long enough to carry prosody, short
# enough that minting it costs one edge call. Its length is also the denominator
# of the speaking-rate estimate, so it must be normal-paced prose.
REF_TEXT = ("这是一段用来克隆音色的参考录音，我会把每个字都念得清清楚楚，"
            "语速平稳，方便模型学到我的声音和语气。")
F5_STEPS = 8          # 8/16/32 measured within noise of each other; take the fast one
F5_MARGIN_S = 0.35    # slack so the tail is not clipped


def edge_render(text: str, voice: str, out: str) -> None:
    """edge-tts -> mp3 -> 24k mono wav (afconvert ships with macOS)."""
    import edge_tts
    mp3 = out + ".mp3"
    asyncio.run(edge_tts.Communicate(text, voice).save(mp3))
    subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@24000",
                    "-c", "1", mp3, out], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.unlink(mp3)
    trim_and_level(out)


def wav_seconds(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


def ensure_ref(voice: str, cache_dir: str) -> str:
    """The f5 reference clip for `voice`, minted once via edge and kept."""
    ref = os.path.join(cache_dir, f"ref_{voice}.wav")
    if not os.path.exists(ref):
        edge_render(REF_TEXT, voice, ref)
    return ref


def main() -> None:
    job = json.load(sys.stdin)
    engine = job["engine"]
    voice = job["voice"]
    items = [it for it in job["items"] if not os.path.exists(it["out"])]
    cached = len(job["items"]) - len(items)
    cache_dir = job.get("cache_dir") or "."
    os.makedirs(cache_dir, exist_ok=True)
    result = {"ok": True, "rendered": 0, "cached": cached}

    if engine == "edge":
        for it in items:
            edge_render(it["text"], voice, it["out"])
            result["rendered"] += 1
    elif engine == "f5":
        from f5_tts_mlx.generate import generate
        ref = ensure_ref(voice, cache_dir)
        result["ref"] = ref
        ref_s = wav_seconds(ref)
        rate = ref_s / len(REF_TEXT)          # the reference's own s/char
        for it in items:
            generate(generation_text=it["text"], ref_audio_path=ref,
                     ref_audio_text=REF_TEXT, seed=int(job.get("seed", 1234)),
                     steps=F5_STEPS, output_path=it["out"],
                     duration=ref_s + rate * len(it["text"]) + F5_MARGIN_S)
            trim_and_level(it["out"])
            result["rendered"] += 1
    else:
        raise SystemExit(f"unknown engine: {engine}")

    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

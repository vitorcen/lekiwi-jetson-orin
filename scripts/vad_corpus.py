#!/usr/bin/env python3
"""Synthesize the fixed VAD/ASR tuning corpus with Matcha (the robot's own TTS).

One WAV plus one manifest. The WAV is what the Mac plays into the room; the
manifest says, in played-file sample indices, where every utterance sits and
where the silence gaps are. The board records the room; the sweep aligns the two
by the leading marker tone and then knows, for every segment the VAD cut,
whether it landed on speech or on silence.

Why a fixed file at all: comparing VAD parameters on a live mic compares rooms,
not parameters — the noise floor here drifts ~30 dB over a few minutes, which
once produced a "threshold 0.7 is worse than 0.5" result. One recording, swept
offline, is the only version of this experiment whose numbers mean anything.

Setup on the Mac (one time):
  python3.12 -m venv ~/.cache/lekiwi-mac-tts/venv
  ~/.cache/lekiwi-mac-tts/venv/bin/pip install sherpa-onnx numpy
  scp -r jetson@<board>:~/work/lekiwi-jetson-orin/voice/models/matcha-icefall-zh-en \
        jetson@<board>:~/work/lekiwi-jetson-orin/voice/models/vocos-16khz-univ.onnx \
        ~/.cache/lekiwi-mac-tts/models/

Usage:
  ~/.cache/lekiwi-mac-tts/venv/bin/python scripts/vad_corpus.py
"""

from __future__ import annotations

import argparse
import json
import os
import wave

import numpy as np

SR = 16000
HOME = os.path.expanduser("~")
MODELS = os.path.join(HOME, ".cache/lekiwi-mac-tts/models")
OUT_DIR = os.path.join(HOME, ".cache/lekiwi-mac-tts/corpus")

# (text, seconds of digital silence to leave AFTER it). A list of strings is one
# utterance spoken with INTERNAL_PAUSE_S of breath in the middle — it must come
# back as ONE segment.
#
# The gaps carry two different measurements and are deliberately spread from
# 0.8 s to 10 s:
#   * long gaps are the surface false triggers are counted on. A VAD that only
#     misfires after five seconds of quiet looks perfect on uniform 3 s gaps.
#   * short gaps (0.8 / 1.2 s) are two commands in a row that must be cut into
#     two turns. Together with the mid-utterance breaths they pin min_silence_s
#     from both sides: too large merges the pair, too small splits the breath and
#     sends half a question to the brain. The first corpus had no gap under 2.5 s
#     and no internal pause at all, so that axis measured nothing.
INTERNAL_PAUSE_S = 0.35

SCRIPT = [
    ("停",                                        1.0),
    ("后退",                                      0.8),
    ("左转九十度",                                3.0),
    ("往前走一点点",                              1.5),
    (["帮我看一下", "桌子上面放着什么"],           5.0),
    ("现在电池还有多少电",                        2.0),
    ("好的",                                      7.0),
    ("你看看前面有什么东西",                      3.0),
    (["回到原来的位置", "然后停下来"],             4.0),
    ("不对",                                      1.2),
    ("再来一次",                                 10.0),
    ("摄像头看到的画面描述一下",                  2.5),
    ("停下来别动了",                              6.0),
    ("谢谢",                                      8.0),
]

# No alignment tone. The first version led with a 1 kHz beep; every VAD cut it as
# speech and then merged it with 「停」 2 s later, so the first utterance scored as
# a garbage transcription in every config. vad_sweep aligns by cross-correlating
# the whole 68 s energy envelope, which needs no marker at all.
LEAD_S = 4.0          # silence before the first utterance


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(round(seconds * SR)), dtype=np.float32)


EDGE_VOICES = [
    "zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-XiaoyiNeural",
    "zh-CN-YunjianNeural", "zh-CN-YunyangNeural", "zh-CN-YunxiaNeural",
]


def edge_synth(text: str, voice: str) -> np.ndarray:
    """edge-tts -> 16 kHz mono float32.

    Matcha is the robot's own TTS but a poor measuring instrument: it says
    「往前走一点点」 in a way Fun-ASR reads as 「往前存做点点」 with no microphone
    in the path at all, which charges the acoustic chain for a synthesis defect.
    Several voices also stop one badly-articulated speaker from deciding the
    whole experiment. Network required — these are Microsoft's online voices."""
    import asyncio
    import subprocess
    import tempfile
    import edge_tts

    fd, mp3 = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    wav = mp3[:-4] + ".wav"
    try:
        asyncio.run(edge_tts.Communicate(text, voice).save(mp3))
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", mp3, "-ac", "1", "-ar", str(SR), wav], check=True)
        with wave.open(wav, "rb") as w:
            raw = w.readframes(w.getnframes())
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    finally:
        for p in (mp3, wav):
            try:
                os.unlink(p)
            except OSError:
                pass


def load_matcha():
    import sherpa_onnx as so
    base = os.path.join(MODELS, "matcha-icefall-zh-en")
    tc = so.OfflineTtsConfig()
    tc.model.matcha.acoustic_model = os.path.join(base, "model-steps-3.onnx")
    tc.model.matcha.vocoder = os.path.join(MODELS, "vocos-16khz-univ.onnx")
    tc.model.matcha.lexicon = os.path.join(base, "lexicon.txt")
    tc.model.matcha.tokens = os.path.join(base, "tokens.txt")
    tc.model.matcha.data_dir = os.path.join(base, "espeak-ng-data")
    tc.model.num_threads = 2
    tc.rule_fsts = ",".join(
        os.path.join(base, f)
        for f in ("date-zh.fst", "number-zh.fst", "phone-zh.fst"))
    return so.OfflineTts(tc)


def _trim(x: np.ndarray, what: str) -> np.ndarray:
    """Trim an utterance to its own speech extent.

    Both engines pad a variable amount of near-silence around the phrase. Left
    in, that padding sits inside the manifest's "this is speech" window and
    quietly excuses a VAD that fires early."""
    if x.size < int(0.10 * SR):
        raise RuntimeError(f"{what} produced only {x.size / SR:.2f}s")
    frame = SR // 100                                   # 10 ms
    n = (len(x) // frame) * frame
    rms = np.sqrt((x[:n].reshape(-1, frame) ** 2).mean(axis=1) + 1e-12)
    loud = np.flatnonzero(rms > rms.max() * 0.02)       # -34 dB of the peak
    if loud.size == 0:
        raise RuntimeError(f"{what} output is silent")
    lo = max(0, (loud[0] - 2) * frame)
    hi = min(len(x), (loud[-1] + 3) * frame)
    return np.ascontiguousarray(x[lo:hi])


class Voicer:
    """One call site for both backends. `speak(text, voice)` -> trimmed 16 kHz."""

    def __init__(self, backend: str):
        self.backend = backend
        self.matcha = load_matcha() if backend == "matcha" else None

    def speak(self, text: str, voice: str | None) -> np.ndarray:
        if self.backend == "matcha":
            audio = self.matcha.generate(text, sid=0, speed=1.0)
            if audio.sample_rate != SR:
                raise RuntimeError(f"matcha returned {audio.sample_rate} Hz")
            return _trim(np.asarray(audio.samples, dtype=np.float32), "matcha")
        return _trim(edge_synth(text, voice), voice or "edge")


def build(voicer: Voicer, peak: float, voices):
    """Voices rotate across utterances so one recording carries several speakers —
    a corpus in a single synthetic voice measures that voice as much as the VAD."""
    parts = [_silence(LEAD_S)]
    cursor = len(parts[0])
    utts = []
    for i, (entry, gap) in enumerate(SCRIPT):
        voice = voices[i % len(voices)] if voices else None
        chunks = [entry] if isinstance(entry, str) else list(entry)
        pieces = []
        for j, chunk in enumerate(chunks):
            if j:
                pieces.append(_silence(INTERNAL_PAUSE_S))
            x = voicer.speak(chunk, voice)
            pieces.append((x * (peak / max(float(np.abs(x).max()), 1e-6)))
                          .astype(np.float32))
        body = np.concatenate(pieces)
        parts.append(body)
        utts.append({"text": "".join(chunks), "start": cursor,
                     "end": cursor + len(body), "dur_s": round(len(body) / SR, 3),
                     "chunks": len(chunks), "gap_after_s": gap,
                     "voice": voice or voicer.backend})
        cursor += len(body)
        parts.append(_silence(gap))
        cursor += len(parts[-1])

    wav = np.concatenate(parts)
    return wav, {
        "sample_rate": SR,
        "tts": voicer.backend,
        # Scoring starts one second before the first utterance; what the room did
        # while the recorder was already open but nothing was playing is measured
        # by noise.wav, not here.
        "score_from": utts[0]["start"] - SR,
        "utterances": utts,
        "total": len(wav),
        "peak": peak,
    }


def build_voicecheck(voicer: Voicer, peak: float, voices):
    """Every line in every voice, back to back — no room, no VAD, just the ASR.

    This is the instrument-calibration pass: a voice whose lines Fun-ASR cannot
    read here will never be readable through a microphone, and including it would
    charge the acoustic chain for a synthesis defect."""
    parts, utts, cursor = [_silence(0.5)], [], int(0.5 * SR)
    for voice in voices:
        for entry, _ in SCRIPT:
            text = entry if isinstance(entry, str) else "".join(entry)
            x = voicer.speak(text, voice)
            x = (x * (peak / max(float(np.abs(x).max()), 1e-6))).astype(np.float32)
            parts.append(x)
            utts.append({"text": text, "start": cursor, "end": cursor + len(x),
                         "dur_s": round(len(x) / SR, 3), "chunks": 1,
                         "gap_after_s": 0.6, "voice": voice})
            cursor += len(x)
            parts.append(_silence(0.6))
            cursor += len(parts[-1])
    wav = np.concatenate(parts)
    return wav, {"sample_rate": SR, "tts": voicer.backend, "score_from": 0,
                 "utterances": utts, "total": len(wav), "peak": peak}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--peak", type=float, default=0.85,
                    help="per-utterance peak amplitude (playback volume is set "
                         "on the Mac; this only normalises across utterances)")
    ap.add_argument("--tts", choices=["edge", "matcha"], default="edge")
    ap.add_argument("--voices", default=",".join(EDGE_VOICES))
    ap.add_argument("--name", default="corpus")
    ap.add_argument("--voice-check", action="store_true",
                    help="every line in every voice, no gaps worth scoring — "
                         "decode it to see which voices the ASR can read at all")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    voices = [v for v in args.voices.split(",") if v] if args.tts == "edge" else []
    print(f"tts={args.tts}"
          + (f"  voices={len(voices)}" if voices else "") + " ...", flush=True)
    voicer = Voicer(args.tts)
    if args.voice_check:
        wav, manifest = build_voicecheck(voicer, args.peak, voices or ["matcha"])
    else:
        wav, manifest = build(voicer, args.peak, voices)

    wav_path = os.path.join(args.out, f"{args.name}.wav")
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(wav, -1, 1) * 32767).astype("<i2").tobytes())
    with open(os.path.join(args.out, f"{args.name}.json"), "w") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)

    utts = manifest["utterances"]
    speech = sum(u["end"] - u["start"] for u in utts)
    print(f"{wav_path}  {manifest['total'] / SR:.1f}s "
          f"({len(utts)} 句 / 语音 {speech / SR:.1f}s / "
          f"静音 {(manifest['total'] - speech) / SR:.1f}s)")
    if not args.voice_check:
        for u in utts:
            print(f"  {u['start'] / SR:7.2f}s  {u['dur_s']:.2f}s  "
                  f"后隔 {u['gap_after_s']:4.1f}s  "
                  f"{u['voice'].replace('zh-CN-', '').replace('Neural', ''):<10s} "
                  f"{u['text']}" + ("  (含换气)" if u["chunks"] > 1 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

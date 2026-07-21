#!/usr/bin/env python3
"""In-process engine hosts for voice-daemon (host mode settled in P0a: gc.collect()
alone does NOT return RSS, but ctypes malloc_trim(0) does — so unload() MUST end with
it, see docs §5.0).

Engines own only the model lifecycle (load/unload/trim) and the sync compute primitive.
Audio I/O, VAD, generation numbers, subprocess management, the edge->melo breaker and
the aplay pipeline stay owned by the daemon (§5.1) — those behaviours are kept intact.

sherpa_onnx / numpy are imported lazily inside load()/compute so this module is
import-clean on a dev machine (tests import the pure helpers, not this).
"""

from __future__ import annotations

import ctypes
import gc
import os
import re

MODELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

# SenseVoice emits tags like <|zh|><|HAPPY|><|Speech|>; strip them from the transcript.
_TAG_RE = re.compile(r"<\|[^|]*\|>")


def malloc_trim():
    """Return freed heap arena to the OS. THE key step that makes engine unload actually
    drop RSS on the board (glibc). No-op / harmless if libc has no malloc_trim."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.malloc_trim(0)
    except (OSError, AttributeError):
        pass


class OfflineAsr:
    """SenseVoice offline recognizer (sensevoice). transcribe() runs the sync decode;
    the daemon calls it inside its ASR thread pool."""

    name = "sensevoice"

    def __init__(self):
        self.rec = None

    @property
    def loaded(self):
        return self.rec is not None

    def load(self):
        import sherpa_onnx as so
        self.rec = so.OfflineRecognizer.from_sense_voice(
            model=os.path.join(MODELS, "sense-voice/model.int8.onnx"),
            tokens=os.path.join(MODELS, "sense-voice/tokens.txt"),
            num_threads=4, use_itn=True, language="zh",
        )

    def unload(self):
        self.rec = None
        gc.collect()
        malloc_trim()

    def transcribe(self, samples):
        stream = self.rec.create_stream()
        stream.accept_waveform(16000, samples)
        self.rec.decode_stream(stream)
        text = stream.result.text or ""
        return _TAG_RE.sub("", text).strip()


class MeloTts:
    """Local Melo VITS (zh_en). Owns the sherpa OfflineTts model; synth() returns int16
    PCM @ 44100. Shared by the standalone 'melo' tts engine and as the edge->melo
    fallback (the breaker chain itself lives in the daemon, unchanged)."""

    name = "melo"
    sample_rate = 44100

    def __init__(self):
        self.tts = None

    @property
    def loaded(self):
        return self.tts is not None

    def load(self):
        import sherpa_onnx as so
        tc = so.OfflineTtsConfig()
        base = os.path.join(MODELS, "vits-melo-tts-zh_en")
        tc.model.vits.model = os.path.join(base, "model.onnx")
        tc.model.vits.lexicon = os.path.join(base, "lexicon.txt")
        tc.model.vits.tokens = os.path.join(base, "tokens.txt")
        tc.model.vits.dict_dir = os.path.join(base, "dict")
        tc.model.num_threads = 4
        tc.rule_fsts = ",".join(
            os.path.join(base, f) for f in ("date.fst", "number.fst", "phone.fst")
        )
        self.tts = so.OfflineTts(tc)
        self.tts.generate("好", sid=0, speed=1.0)      # warm up

    def unload(self):
        self.tts = None
        gc.collect()
        malloc_trim()

    def synth(self, text):
        import numpy as np
        audio = self.tts.generate(text, sid=0, speed=1.0)
        samp = np.asarray(audio.samples, dtype=np.float32)
        pcm = np.clip(samp, -1.0, 1.0)
        return (pcm * 32767.0).astype(np.int16)


# Metadata registry: which engines exist per axis. edge is a playback mode over the
# shared Melo model (its fallback), so only the model-owning engines appear as hosts.
REGISTRY = {
    "asr": {"sensevoice": OfflineAsr},
    "tts": {"melo": MeloTts},          # 'edge' reuses MeloTts as its breaker fallback
}

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


class OfflineParaformer:
    """Paraformer-large (zh) offline recognizer (paraformer). Same runtime + transcribe
    shape as SenseVoice (non-autoregressive, single forward pass, ~same footprint), so it
    drops into the ASR axis as a pure A/B alternative — often stronger on Mandarin
    homophones / named entities. Model dir: voice/models/paraformer-zh/."""

    name = "paraformer"

    def __init__(self):
        self.rec = None

    @property
    def loaded(self):
        return self.rec is not None

    def load(self):
        import sherpa_onnx as so
        # NB: from_paraformer has NO use_itn kwarg (that's sense_voice-only); the
        # paraformer-zh model bakes its own punctuation/ITN behaviour.
        self.rec = so.OfflineRecognizer.from_paraformer(
            paraformer=os.path.join(MODELS, "paraformer-zh/model.int8.onnx"),
            tokens=os.path.join(MODELS, "paraformer-zh/tokens.txt"),
            num_threads=4,
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


class OfflineWhisper:
    """Whisper large-v3-turbo (multilingual). Autoregressive encoder-decoder → heavier
    RAM (~1.5GB) and higher CPU latency than the NAR engines; kept here for A/B only,
    run it with the vision service stopped. Model dir: voice/models/whisper-turbo/."""

    name = "whisper"

    def __init__(self):
        self.rec = None

    @property
    def loaded(self):
        return self.rec is not None

    def load(self):
        import sherpa_onnx as so
        base = os.path.join(MODELS, "whisper-turbo")
        self.rec = so.OfflineRecognizer.from_whisper(
            encoder=os.path.join(base, "turbo-encoder.int8.onnx"),
            decoder=os.path.join(base, "turbo-decoder.int8.onnx"),
            tokens=os.path.join(base, "turbo-tokens.txt"),
            language="zh", task="transcribe", num_threads=4,
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


class OfflineQwen3Asr:
    """Qwen3-ASR-0.6B int8 (LLM-ASR: AuT audio encoder + Qwen3 decoder), sherpa-onnx
    native (from_qwen3_asr, board sherpa 1.13.4+). Much stronger noise/far-field
    robustness than the NAR small models — that's the whole point of adding it. RSS scales
    with clip length (LLM KV cache): ~0.9-1.2GB on 1-3s VAD segments (our real case), only
    balloons to ~3.5GB on long 20-50s clips. Its decoder has a max_total_len=512 KV cap →
    >~10s audio gets truncated, so keep segments short. Model dir: voice/models/qwen3-asr/."""

    name = "qwen3"

    def __init__(self):
        self.rec = None

    @property
    def loaded(self):
        return self.rec is not None

    def load(self):
        import sherpa_onnx as so
        base = os.path.join(MODELS, "qwen3-asr")
        self.rec = so.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=os.path.join(base, "conv_frontend.onnx"),
            encoder=os.path.join(base, "encoder.int8.onnx"),
            decoder=os.path.join(base, "decoder.int8.onnx"),
            tokenizer=os.path.join(base, "tokenizer"),      # a directory, not a file
            num_threads=4,
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


# Selectable streaming models (all zipformer transducers → same from_transducer path,
# only the model dir + filenames differ). Chinese-trained ones are much cleaner than the
# old bilingual (which repeats tokens). disk_mb measured on the board.
STREAM_SPECS = {
    "zh-2025": {"label": "流式 zipformer zh 2025", "dir": "streaming-zh-2025",
                "encoder": "encoder.int8.onnx", "decoder": "decoder.onnx",
                "joiner": "joiner.int8.onnx", "disk_mb": 300},
    "zh-xlarge": {"label": "流式 zipformer zh xlarge (700M,更强更慢)",
                  "dir": "streaming-zh-xlarge", "encoder": "encoder.int8.onnx",
                  "decoder": "decoder.onnx", "joiner": "joiner.int8.onnx", "disk_mb": 737},
    "multi-zh": {"label": "流式 multi-zh-hans (14k小时)", "dir": "streaming-multi-zh",
                 "encoder": "encoder-epoch-20-avg-1-chunk-16-left-128.int8.onnx",
                 "decoder": "decoder-epoch-20-avg-1-chunk-16-left-128.onnx",
                 "joiner": "joiner-epoch-20-avg-1-chunk-16-left-128.int8.onnx",
                 "disk_mb": 250},
    "zh-en": {"label": "流式 双语 zh-en (弱/基线)", "dir": "streaming-zipformer-zh-en",
              "encoder": "encoder-epoch-99-avg-1.int8.onnx",
              "decoder": "decoder-epoch-99-avg-1.int8.onnx",
              "joiner": "joiner-epoch-99-avg-1.int8.onnx", "disk_mb": 190},
}
STREAM_DEFAULT = "zh-2025"


class StreamingAsr:
    """Streaming zipformer for the DEBUG streaming mode — consumes audio continuously and
    self-endpoints, NO VAD. Owns one persistent OnlineStream, reset on each endpoint. NOT
    in the offline ASR REGISTRY: its interface is feed(chunk) -> (partial, final|None), not
    transcribe(whole_segment). Model-parameterized (STREAM_SPECS): the daemon picks which
    streaming model to load, independent of the offline engine dropdown."""

    name = "stream"

    def __init__(self):
        self.rec = None
        self.stream = None
        self.model = None

    @property
    def loaded(self):
        return self.rec is not None

    def load(self, model_id=STREAM_DEFAULT, endpoint_silence_s=1.2):
        import sherpa_onnx as so
        spec = STREAM_SPECS.get(model_id) or STREAM_SPECS[STREAM_DEFAULT]
        base = os.path.join(MODELS, spec["dir"])
        self.rec = so.OnlineRecognizer.from_transducer(
            tokens=os.path.join(base, "tokens.txt"),
            encoder=os.path.join(base, spec["encoder"]),
            decoder=os.path.join(base, spec["decoder"]),
            joiner=os.path.join(base, spec["joiner"]),
            num_threads=4, decoding_method="greedy_search",
            enable_endpoint_detection=True,
            rule2_min_trailing_silence=float(endpoint_silence_s),
        )
        self.stream = self.rec.create_stream()
        self.model = model_id if model_id in STREAM_SPECS else STREAM_DEFAULT

    def unload(self):
        self.rec = None
        self.stream = None
        self.model = None
        gc.collect()
        malloc_trim()

    def reset(self):
        """Drop any in-flight partial (e.g. on entering/leaving the mode)."""
        if self.rec is not None:
            self.stream = self.rec.create_stream()

    def feed(self, samples):
        """Feed a float32 16k chunk. Returns (partial, final|None): partial is the current
        growing hypothesis; final is set exactly on the chunk an endpoint fires, after
        which the stream is reset so the next utterance starts clean."""
        self.stream.accept_waveform(16000, samples)
        while self.rec.is_ready(self.stream):
            self.rec.decode_stream(self.stream)
        partial = _TAG_RE.sub("", self.rec.get_result(self.stream) or "").strip()
        final = None
        if self.rec.is_endpoint(self.stream):
            final = partial
            self.rec.reset(self.stream)
        return partial, final


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
    "asr": {"sensevoice": OfflineAsr, "paraformer": OfflineParaformer,
            "whisper": OfflineWhisper, "qwen3": OfflineQwen3Asr},
    "tts": {"melo": MeloTts},          # 'edge' reuses MeloTts as its breaker fallback
}

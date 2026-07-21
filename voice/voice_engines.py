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


class OfflineFunAsr:
    """Fun-ASR-Nano 0.8B (LLM-ASR: SAN-M encoder + Qwen3-0.6B decoder) via the patched
    llama-funasr-cli serve mode (-a -): a resident subprocess, one wav path line in ->
    one transcription line out. GPU decode (~0.4-0.7s/segment on Orin vs qwen3 CPU
    ~1.5s), emits punctuation. No --vad passed: the daemon's VAD already segmented, so
    the whole handed-over file is one window. Binary + GGUFs live outside the repo tree
    (built from the Fun-ASR fork, see .memory/voice-asr-engines)."""

    name = "funasr"
    unload_first = True    # ~1.6GB peak: can't coexist with the old engine on 8GB
    BIN = os.path.expanduser("~/work/Fun-ASR/runtime/llama.cpp/build/bin/llama-funasr-cli")
    GGUF = os.path.expanduser("~/work/funasr-gguf")
    SELFTEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest.wav")

    def __init__(self):
        self.proc = None

    @property
    def loaded(self):
        return self.proc is not None and self.proc.poll() is None

    def load(self):
        import subprocess
        if not os.path.exists(self.BIN):
            raise RuntimeError(f"llama-funasr-cli missing: {self.BIN}")
        self.proc = subprocess.Popen(
            [self.BIN, "--enc", os.path.join(self.GGUF, "funasr-encoder-f16.gguf"),
             "-m", os.path.join(self.GGUF, "qwen3-0.6b-q4km.gguf"), "-a", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        # Readiness barrier + honest load probe: the first request only answers once
        # the models are resident; a broken binary/GGUF fails HERE, not mid-conversation.
        if os.path.exists(self.SELFTEST) and not self._ask(self.SELFTEST, timeout=60.0):
            self.unload()
            raise RuntimeError("funasr serve probe returned empty")

    def unload(self):
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.stdin.close()                       # EOF -> serve loop exits
                p.wait(timeout=5.0)
            except Exception:                         # noqa: BLE001
                p.kill()
        gc.collect()
        malloc_trim()

    def _ask(self, wav_path, timeout=60.0):
        """One request/response on the resident process. Timeout or death -> kill +
        raise (the daemon's ASR pool must never hang on a wedged child)."""
        import select
        if not self.loaded:
            raise RuntimeError("funasr serve process not running")
        self.proc.stdin.write(wav_path + "\n")
        self.proc.stdin.flush()
        r, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not r:
            self.proc.kill()
            raise RuntimeError(f"funasr timed out after {timeout}s")
        return self.proc.stdout.readline().strip()

    def transcribe(self, samples):
        import tempfile
        import wave as _wave
        pcm = (samples * 32767.0).clip(-32768, 32767).astype("int16")
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="funasr-")
        try:
            with os.fdopen(fd, "wb") as fh, _wave.open(fh, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm.tobytes())
            return self._ask(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


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
    "x-asr-zh-en": {"label": "流式 X-ASR 中英混 (1M小时,fp32)",
                    "dir": "streaming-x-asr-zh-en", "encoder": "encoder-480ms.onnx",
                    "decoder": "decoder-480ms.onnx", "joiner": "joiner-480ms.onnx",
                    "disk_mb": 584},
    # api=paraformer: OnlineRecognizer.from_paraformer (no joiner file). Same feed()/
    # endpoint surface as the transducers — only the constructor differs.
    "para-zh-en": {"label": "流式 Paraformer-large 中英 (int8)", "api": "paraformer",
                   "dir": "streaming-paraformer-zh-en", "encoder": "encoder.int8.onnx",
                   "decoder": "decoder.int8.onnx", "disk_mb": 226},
}
STREAM_DEFAULT = "x-asr-zh-en"


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
        # Measured on Orin (xlarge, 16s audio): RTF 1t=0.62, 2t=0.56, 3t=0.80,
        # 4t=0.72, 6t=1.15 — streaming chunks are too small for wide parallelism,
        # more threads only add sync overhead. 2 is the sweet spot.
        common = dict(
            tokens=os.path.join(base, "tokens.txt"),
            encoder=os.path.join(base, spec["encoder"]),
            decoder=os.path.join(base, spec["decoder"]),
            num_threads=2, decoding_method="greedy_search",
            enable_endpoint_detection=True,
            rule2_min_trailing_silence=float(endpoint_silence_s),
        )
        if spec.get("api") == "paraformer":
            self.rec = so.OnlineRecognizer.from_paraformer(**common)
        else:
            self.rec = so.OnlineRecognizer.from_transducer(
                joiner=os.path.join(base, spec["joiner"]), **common)
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
        """Feed a float32 16k chunk (any size — the daemon's decode worker merges backlog
        into big batches to catch up). Returns (partial, finals): finals is a list because
        a merged batch can span several utterances. The endpoint check MUST be inside the
        decode loop: checking once per feed() call loses everything decoded after a
        mid-batch endpoint (reset wipes it) and misses endpoints entirely when the batch
        tail is speech again — that was the 'whole sentences vanish' bug."""
        self.stream.accept_waveform(16000, samples)
        finals = []
        while self.rec.is_ready(self.stream):
            self.rec.decode_stream(self.stream)
            if self.rec.is_endpoint(self.stream):
                txt = _TAG_RE.sub("", self.rec.get_result(self.stream) or "").strip()
                if txt:
                    finals.append(txt)
                self.rec.reset(self.stream)
        partial = _TAG_RE.sub("", self.rec.get_result(self.stream) or "").strip()
        return partial, finals


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


class MatchaTts:
    """Matcha-TTS zh-en (icefall) + vocos 16k vocoder. Non-autoregressive: board-measured
    RTF 0.18 @2 threads vs melo 1.60 — the only realtime local engine. Single voice.
    The zh-en acoustic model requires the 16khz vocos vocoder (22khz plays wrong)."""

    name = "matcha"
    sample_rate = 16000

    def __init__(self):
        self.tts = None

    @property
    def loaded(self):
        return self.tts is not None

    def load(self):
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
            for f in ("date-zh.fst", "number-zh.fst", "phone-zh.fst")
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
            "whisper": OfflineWhisper, "qwen3": OfflineQwen3Asr,
            "funasr": OfflineFunAsr},
    "tts": {"melo": MeloTts, "matcha": MatchaTts},
}

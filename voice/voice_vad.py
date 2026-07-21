#!/usr/bin/env python3
"""Switchable VAD front-end + digital make-up gain for voice-daemon.

Unified interface: feed(float32 16k chunk) -> list[np.ndarray segments]. Each engine
owns its own buffering/segmentation; the daemon just pushes 320ms chunks and consumes
whole segments, exactly as it did with the raw sherpa VoiceActivityDetector (P2.7).

Four engines:
  silero  — sherpa-onnx VoiceActivityDetector, a byte-for-byte wrapper: with the same
            params the emitted segments are identical to the pre-P2.7 daemon.
  ten     — sherpa-onnx TEN VAD (needs sherpa-onnx built with ten_vad + the model file).
  webrtc  — py-webrtcvad, 20ms frames aggregated by the pure open/close segmenter.
  energy  — pure-python dBFS gate, same segmenter. Zero deps: the debug baseline.

Engines that are not installable / not built are reported unavailable and MUST be
refused at switch time — never a silent fall back to another engine.

numpy / sherpa_onnx / webrtcvad are imported lazily so voice_config stays import-clean;
the unit tests exercise the pure segmenter + gain with numpy only.
"""
from __future__ import annotations

import math
import os
from collections import deque

# Aggregating engines (webrtc/energy) chop the stream into fixed frames. webrtcvad only
# accepts 10/20/30ms frames; 20ms @16k = 320 samples is the middle ground.
FRAME_MS = 20
SAMPLE_RATE = 16000
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000          # 320

VAD_ENGINES = ["silero", "fsmn", "ten", "webrtc", "energy"]
TEN_MODEL = "ten-vad.onnx"                              # under voice/models/
# FSMN-VAD (FunASR) runs via the patched llama-funasr-vad serve binary — built from the
# Fun-ASR fork outside the repo tree (see voice/patches/ + .memory/voice-asr-engines).
FSMN_BIN = os.path.expanduser(
    "~/work/Fun-ASR/runtime/llama.cpp/build/bin/llama-funasr-vad")
FSMN_MODEL = os.path.expanduser("~/work/funasr-gguf/fsmn-vad.gguf")


# --------------------------------------------------------------------------- #
# Digital make-up gain (audio front-end). Pure; default 0 dB is identity so the
# stock path is byte-for-byte unchanged.
# --------------------------------------------------------------------------- #
def apply_gain(samples, gain_db):
    """float32 [-1,1] * 10**(gain_db/20), hard-clipped back to [-1,1]. gain_db<=0 -> the
    array is returned unchanged (no allocation, exact stock behaviour at the 0 default)."""
    import numpy as np
    try:
        g = float(gain_db)
    except (TypeError, ValueError):
        g = 0.0
    if g <= 0.0:
        return samples
    factor = 10.0 ** (g / 20.0)
    return np.clip(samples * factor, -1.0, 1.0).astype(np.float32)


def _frames(seconds):
    """Duration in seconds -> whole 20ms frames (>=1)."""
    return max(1, int(math.ceil(float(seconds) / (FRAME_MS / 1000.0))))


# --------------------------------------------------------------------------- #
# Pure open/close segmenter — shared by webrtc + energy. Consecutive voiced frames
# >= min_speech open a segment; consecutive silence >= min_silence close it.
# --------------------------------------------------------------------------- #
class _Segmenter:
    """Deterministic, no I/O. The transition is unit-tested via segment_voiced().
    pre_roll_frames: on open, back-fill this many leading frames (look-back) so the
    segment carries the speech onset the trigger delay would otherwise clip."""

    def __init__(self, min_speech_frames, min_silence_frames, pre_roll_frames=0):
        self.min_speech = max(1, int(min_speech_frames))
        self.min_silence = max(1, int(min_silence_frames))
        self.pre_roll = max(0, int(pre_roll_frames))
        # look-back must hold the confirming speech run PLUS the pre-roll context.
        self._lookback = self.min_speech + self.pre_roll
        self.reset()

    def reset(self):
        self._triggered = False
        self._voiced_run = 0
        self._silence_run = 0
        self._buf = []
        self._ring = deque(maxlen=self._lookback)   # frames seen while not triggered

    @property
    def active(self):
        return self._triggered

    def push(self, voiced, frame):
        """Advance one frame. Returns [seg_samples] if a segment just closed, else []."""
        out = []
        if not self._triggered:
            self._ring.append(frame)               # keep rolling look-back over silence
            if voiced:
                self._voiced_run += 1
                if self._voiced_run >= self.min_speech:
                    self._triggered = True
                    self._silence_run = 0
                    self._buf = list(self._ring)   # pre-roll + confirming speech
                    self._ring.clear()
            else:
                self._voiced_run = 0
        else:
            self._buf.append(frame)
            if voiced:
                self._silence_run = 0
            else:
                self._silence_run += 1
                if self._silence_run >= self.min_silence:
                    out.append(self._emit())
        return out

    def _emit(self):
        import numpy as np
        seg = np.concatenate(self._buf) if self._buf else np.zeros(0, dtype=np.float32)
        self.reset()
        return seg

    def flush(self):
        """End-of-stream: emit an open segment if any, then reset."""
        if self._triggered and self._buf:
            return [self._emit()]
        self.reset()
        return []


def segment_voiced(flags, min_speech_frames, min_silence_frames, flush=True,
                   pre_roll_frames=0):
    """Pure reference over a boolean voiced sequence -> list of (start, end) frame-index
    ranges (end exclusive) of closed segments. Runs the SAME _Segmenter that webrtc/energy
    use, so testing this tests the streaming path (pre-roll included)."""
    import numpy as np
    seg = _Segmenter(min_speech_frames, min_silence_frames, pre_roll_frames)
    out = []
    for i, v in enumerate(flags):
        for s in seg.push(bool(v), np.array([i], dtype=np.float32)):
            out.append((int(s[0]), int(s[-1]) + 1))
    if flush:
        for s in seg.flush():
            out.append((int(s[0]), int(s[-1]) + 1))
    return out


# --------------------------------------------------------------------------- #
# Engines
# --------------------------------------------------------------------------- #
class VadEngine:
    name = "base"

    def feed(self, chunk):
        raise NotImplementedError

    def reset(self):
        pass

    def flush(self):
        return []

    @property
    def active(self):
        return False


class SileroVad(VadEngine):
    """sherpa-onnx VoiceActivityDetector wrapper. feed() = accept_waveform + drain, the
    exact call sequence the daemon used before P2.7 — same params => same segments."""

    name = "silero"
    MAX_SPEECH_S = 20
    BUFFER_S = 30
    RING_MARGIN_S = 3.0                              # ring beyond pre-roll for normal segs

    def __init__(self, model_path, threshold, min_speech_s, min_silence_s,
                 pre_roll_s=0.0):
        import sherpa_onnx as so
        cfg = so.VadModelConfig()
        self._configure(cfg, model_path, threshold, min_speech_s, min_silence_s)
        cfg.sample_rate = SAMPLE_RATE
        self._vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=self.BUFFER_S)
        # Pre-roll ring: sherpa clips the segment to speech onset (SpeechSegment.start is
        # the sample index since the last reset). We keep a rolling ring of fed audio and
        # back-fill [start-pre_roll, start). pre_roll==0 => no ring, exact stock output.
        self._pre = max(0, int(round(float(pre_roll_s) * SAMPLE_RATE)))
        self._cap = self._pre + int(self.RING_MARGIN_S * SAMPLE_RATE)
        self._reset_ring()

    def _configure(self, cfg, model_path, threshold, min_speech_s, min_silence_s):
        cfg.silero_vad.model = model_path
        cfg.silero_vad.threshold = float(threshold)
        cfg.silero_vad.min_silence_duration = float(min_silence_s)
        cfg.silero_vad.min_speech_duration = float(min_speech_s)
        cfg.silero_vad.max_speech_duration = self.MAX_SPEECH_S

    def _reset_ring(self):
        import numpy as np
        self._fed = 0                                # samples fed since last reset
        self._ring = np.zeros(0, dtype=np.float32)   # last <=_cap fed samples

    def _prepend(self, start, samples):
        """Back-fill up to pre_roll samples of context sitting before `start` in the ring.
        All indices clamped — a rolled-past / over-reported start just yields less pre-roll,
        never a crash (§coordinator: 取不到就有多少拼多少,不报错)."""
        import numpy as np
        ring_start = self._fed - len(self._ring)
        lo = max(start - self._pre, ring_start, 0)
        hi = min(start, self._fed)
        a, b = lo - ring_start, hi - ring_start
        if 0 <= a < b <= len(self._ring):
            return np.concatenate([self._ring[a:b], samples])
        return samples

    def _drain(self):
        import numpy as np
        out = []
        while not self._vad.empty():
            seg = self._vad.front
            samples = np.array(seg.samples, dtype=np.float32)
            if self._pre:
                samples = self._prepend(seg.start, samples)
            out.append(samples)
            self._vad.pop()
        return out

    def feed(self, chunk):
        self._vad.accept_waveform(chunk)
        if self._pre:
            import numpy as np
            self._fed += len(chunk)
            buf = np.concatenate([self._ring, chunk]) if self._ring.size \
                else np.asarray(chunk, dtype=np.float32)
            self._ring = buf[-self._cap:] if len(buf) > self._cap else buf
        return self._drain()

    def reset(self):
        self._vad.reset()
        if self._pre:
            self._reset_ring()

    def flush(self):
        try:
            self._vad.flush()
        except Exception:                                # noqa: BLE001
            pass
        return self._drain()

    @property
    def active(self):
        try:
            return bool(self._vad.is_speech_detected())
        except Exception:                                # noqa: BLE001
            return False


class TenVad(SileroVad):
    """TEN VAD: identical VoiceActivityDetector wrapper, only the model config differs."""

    name = "ten"

    def _configure(self, cfg, model_path, threshold, min_speech_s, min_silence_s):
        cfg.ten_vad.model = model_path
        cfg.ten_vad.threshold = float(threshold)
        cfg.ten_vad.min_silence_duration = float(min_silence_s)
        cfg.ten_vad.min_speech_duration = float(min_speech_s)
        cfg.ten_vad.max_speech_duration = self.MAX_SPEECH_S


class _FramedVad(VadEngine):
    """Common frame-buffering for webrtc/energy: keep a sub-frame residual across feeds,
    classify each whole 20ms frame, run the shared segmenter."""

    def __init__(self, min_speech_s, min_silence_s, pre_roll_s=0.0):
        self._seg = _Segmenter(_frames(min_speech_s), _frames(min_silence_s),
                               int(round(float(pre_roll_s) / (FRAME_MS / 1000.0))))
        self._residual = None

    def _voiced(self, frame):
        raise NotImplementedError

    def feed(self, chunk):
        import numpy as np
        buf = chunk if self._residual is None else np.concatenate([self._residual, chunk])
        n = (len(buf) // FRAME_SAMPLES) * FRAME_SAMPLES
        self._residual = buf[n:] if n < len(buf) else None
        out = []
        for i in range(0, n, FRAME_SAMPLES):
            frame = buf[i:i + FRAME_SAMPLES]
            out.extend(self._seg.push(self._voiced(frame), frame))
        return out

    def reset(self):
        self._seg.reset()
        self._residual = None

    def flush(self):
        return self._seg.flush()

    @property
    def active(self):
        return self._seg.active


class WebrtcVad(_FramedVad):
    """py-webrtcvad. threshold 0..1 -> aggressiveness mode 0..3."""

    name = "webrtc"

    def __init__(self, threshold, min_speech_s, min_silence_s, pre_roll_s=0.0):
        super().__init__(min_speech_s, min_silence_s, pre_roll_s)
        import webrtcvad
        self._vad = webrtcvad.Vad(self._mode(threshold))

    @staticmethod
    def _mode(threshold):
        return max(0, min(3, int(round(float(threshold) * 3))))

    def _voiced(self, frame):
        import numpy as np
        pcm = (np.clip(frame, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        return self._vad.is_speech(pcm, SAMPLE_RATE)


class EnergyVad(_FramedVad):
    """Pure dBFS gate. threshold is a dBFS floor (e.g. -45): frame RMS above it = voiced.
    Zero dependencies — the debug baseline that always exists."""

    name = "energy"

    def __init__(self, threshold, min_speech_s, min_silence_s, pre_roll_s=0.0):
        super().__init__(min_speech_s, min_silence_s, pre_roll_s)
        self._thr = float(threshold)

    def _voiced(self, frame):
        import numpy as np
        if frame.size == 0:
            return False
        rms = float(np.sqrt(np.mean(frame * frame)))
        dbfs = 20.0 * math.log10(max(rms, 1e-9))
        return dbfs >= self._thr


class FsmnVad(VadEngine):
    """FSMN-VAD (FunASR, Alibaba production VAD) via the patched llama-funasr-vad serve
    binary: a resident subprocess, one wav path line in -> 'beg end' ms lines + '.' out.

    The C++ side is batch (whole clip -> closed segments), so this engine keeps a rolling
    buffer and re-runs detection every DETECT_EVERY_S of new audio (~40-90ms per pass,
    model is 1.7MB). A reported segment is emitted only once its end sits CLOSE_MARGIN_S
    clear of the buffer tail — a segment touching the tail is still open (batch VAD ends
    it at clip end, not at true speech end). Emitted audio is trimmed off the buffer.
    threshold is ignored (FSMN has its own internal state machine); min_speech_s filters
    short segments; pre_roll_s extends the cut left (FSMN already leads ~150ms)."""

    name = "fsmn"
    DETECT_EVERY_S = 0.64            # re-run batch detection per this much new audio
    CLOSE_MARGIN_S = 0.24            # tail clearance before a segment counts as closed
    MAX_BUFFER_S = 60.0              # runaway guard: force-trim (open seg force-closed)

    def __init__(self, min_speech_s, pre_roll_s=0.0):
        import subprocess
        import numpy as np
        if not (os.path.exists(FSMN_BIN) and os.path.exists(FSMN_MODEL)):
            raise ValueError("fsmn vad binary/model missing on this board")
        self._min_speech = float(min_speech_s)
        self._pre = max(0, int(round(float(pre_roll_s) * SAMPLE_RATE)))
        self._np = np
        # Binary pipes + own line buffer: the response is MULTI-line ('beg end'* + '.'),
        # and select() on a TextIOWrapper is a trap — readline() slurps the whole
        # response into Python's buffer, after which select() on the fd never fires
        # again and every later line 'times out'. Byte-level reads keep select honest.
        self._proc = subprocess.Popen(
            [FSMN_BIN, "-m", FSMN_MODEL, "-a", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        self._rdbuf = b""
        self._buf = np.zeros(0, dtype=np.float32)
        self._base = 0                # absolute sample index of _buf[0]
        self._since = 0               # samples fed since last detection
        self._open = False            # an in-progress segment touched the tail last run

    def _detect(self):
        """Run batch detection over the current buffer via the serve process.
        Returns [(beg_samp, end_samp)] relative to _buf. Dead child -> ValueError
        (the daemon's vad-error path surfaces it; never silently no-speech)."""
        import select
        import tempfile
        import wave
        if self._proc.poll() is not None:
            raise ValueError("fsmn vad serve process died")
        pcm = (self._buf * 32767.0).clip(-32768, 32767).astype("int16")
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="fsmnvad-")
        try:
            with os.fdopen(fd, "wb") as fh, wave.open(fh, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(pcm.tobytes())
            self._proc.stdin.write((path + "\n").encode())
            self._proc.stdin.flush()
            segs = []
            deadline = 10.0
            fd = self._proc.stdout.fileno()
            import time as _time
            t_end = _time.monotonic() + deadline
            while True:
                nl = self._rdbuf.find(b"\n")
                if nl >= 0:
                    line = self._rdbuf[:nl].strip()
                    self._rdbuf = self._rdbuf[nl + 1:]
                    if line == b".":
                        return segs
                    if line:
                        b_ms, e_ms = line.split()
                        segs.append((int(b_ms) * SAMPLE_RATE // 1000,
                                     int(e_ms) * SAMPLE_RATE // 1000))
                    continue
                remain = t_end - _time.monotonic()
                if remain <= 0:
                    self._proc.kill()
                    raise ValueError("fsmn vad timed out")
                r, _, _ = select.select([fd], [], [], remain)
                if not r:
                    self._proc.kill()
                    raise ValueError("fsmn vad timed out")
                data = os.read(fd, 65536)
                if not data:
                    raise ValueError("fsmn vad closed pipe")
                self._rdbuf += data
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def feed(self, chunk):
        np = self._np
        chunk = np.asarray(chunk, dtype=np.float32)
        self._buf = np.concatenate([self._buf, chunk]) if self._buf.size else chunk
        self._since += len(chunk)
        if self._since < int(self.DETECT_EVERY_S * SAMPLE_RATE):
            return []
        self._since = 0
        return self._harvest(force=len(self._buf) > int(self.MAX_BUFFER_S * SAMPLE_RATE))

    def _harvest(self, force=False):
        np = self._np
        segs = self._detect()                             # chronological [(beg,end)]
        limit = len(self._buf) - int(self.CLOSE_MARGIN_S * SAMPLE_RATE)
        out = []
        trim_to = 0
        self._open = False
        for beg, end in segs:
            if end >= limit and not force:
                # Segment touches the buffer tail: still growing. Everything before it
                # is emitted or inter-segment silence — keep only the open segment plus
                # its pre-roll context.
                self._open = True
                trim_to = max(0, beg - self._pre)
                break
            if end - beg >= int(self._min_speech * SAMPLE_RATE):
                lo = max(0, beg - self._pre)
                out.append(np.array(self._buf[lo:end], dtype=np.float32))
            trim_to = end
        if not self._open and not segs:
            # pure silence: keep only a short tail so the buffer can't creep up
            trim_to = max(0, limit - self._pre)
        if trim_to > 0:
            self._buf = self._buf[trim_to:]
            self._base += trim_to
        return out

    def reset(self):
        self._buf = self._np.zeros(0, dtype=self._np.float32)
        self._base = 0
        self._since = 0
        self._open = False

    def flush(self):
        """End of capture: everything still buffered is final — emit open segments too."""
        if not self._buf.size:
            return []
        try:
            out = self._harvest(force=True)
        except ValueError:
            out = []
        self.reset()
        return out

    @property
    def active(self):
        return self._open

    def close(self):
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.stdin.close()
                p.wait(timeout=3.0)
            except Exception:                            # noqa: BLE001
                p.kill()


# --------------------------------------------------------------------------- #
# Availability + factory
# --------------------------------------------------------------------------- #
def _sherpa_has_ten():
    try:
        import sherpa_onnx as so
        return hasattr(so.VadModelConfig(), "ten_vad")
    except Exception:                                    # noqa: BLE001
        return False


def _webrtc_ok():
    try:
        import webrtcvad                                 # noqa: F401
        return True
    except Exception:                                    # noqa: BLE001
        return False


def availability(models_dir):
    """Which engines can actually be built on THIS board. energy is always true."""
    return {
        "silero": os.path.exists(os.path.join(models_dir, "silero_vad.onnx")),
        "fsmn": os.path.exists(FSMN_BIN) and os.path.exists(FSMN_MODEL),
        "ten": _sherpa_has_ten() and os.path.exists(os.path.join(models_dir, TEN_MODEL)),
        "webrtc": _webrtc_ok(),
        "energy": True,
    }


def make_vad(engine, params, models_dir):
    """Build a VadEngine, or raise ValueError (unknown / unavailable). NEVER silently
    substitutes a different engine — an unavailable engine is a hard refusal."""
    if engine not in VAD_ENGINES:
        raise ValueError(f"unknown vad engine: {engine}")
    if not availability(models_dir).get(engine):
        raise ValueError(f"vad engine unavailable: {engine}")
    params = params or {}
    thr = params.get("threshold", 0.5)
    msp = params.get("min_speech_s", 0.25)
    msl = params.get("min_silence_s", 0.55)
    pr = params.get("pre_roll_s", 0.0)
    if engine == "silero":
        return SileroVad(os.path.join(models_dir, "silero_vad.onnx"), thr, msp, msl, pr)
    if engine == "fsmn":
        return FsmnVad(msp, pr)      # threshold/min_silence: FSMN 自带内部状态机,不外调
    if engine == "ten":
        return TenVad(os.path.join(models_dir, TEN_MODEL), thr, msp, msl, pr)
    if engine == "webrtc":
        return WebrtcVad(thr, msp, msl, pr)
    return EnergyVad(thr, msp, msl, pr)

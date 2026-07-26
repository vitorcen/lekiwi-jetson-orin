#!/usr/bin/env python3
"""Unified board config (desired state) for voice-daemon.

Location: ~/.config/lekiwi/config.json (XDG, not in the source tree, hand-grep/editable).
voice-daemon is the ONLY writer (atomic write-then-rename); other board services read it.
Missing/invalid config -> fall back to built-in DEFAULT_CONFIG and start anyway (never
refuse to boot over config). Pure functions here are import-clean (stdlib only) so the
switch/merge logic is unit-testable off the board.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile

CONFIG_PATH = os.path.expanduser(
    os.environ.get("LEKIWI_CONFIG", "~/.config/lekiwi/config.json")
)

# The one persistent engine state is presets[*].pair + current brain. Running engines
# are always the projection of the current preset's pair (debug overrides are ephemeral).
DEFAULT_CONFIG = {
    # A preset carries its own `kind` (tagged union). The old shape had kind on
    # `brain` plus a top-level `omni.url`, which made "brain.kind says hermes but
    # brain.preset points at an omni entry" a representable state. It no longer is.
    # A preset without `kind` reads as "hermes" so existing configs keep working.
    "brain": {"preset": "deepseek"},
    "vision_speak": False,
    "vision_speak_limit": 300,          # spoken-caption cap, Python len() chars
    "auto_listen": True,                # boot straight into LISTENING (same
                                        # path as the GUI mic button)
    "vision": {"model": None},          # desired VLM model id; null = use board env as-is
    # Audio front-end (global, not part of a preset pair). vad = the switchable segmenter;
    # audio.gain_db = digital make-up gain (0 = identity, stock behaviour unchanged).
    # The 2026-07-25 sweep optimum. The old default (fsmn / 0.5 / 0.1 / 0.5 / 0.9
    # with audio gain +15) misfired on 20.6% of idle frames and clipped 14.8% of
    # speech; a hand-edited config that falls back to defaults should not land on
    # the worst engine in the box.
    "vad": {"engine": "silero", "threshold": 0.3,
            "min_speech_s": 0.1, "min_silence_s": 0.4, "pre_roll_s": 0.6},
    "audio": {"gain_db": 0},
    # Where the "remote" ASR engine sends segments. Empty url = not configured;
    # selecting the remote engine then fails loudly instead of silently doing
    # nothing. Kept out of the preset pair on purpose — see REMOTE_ASR_ENGINE.
    "remote_asr": {"url": "", "model": "mlx-community/whisper-large-v3-mlx"},
    # DEBUG-only streaming recognition mode (免VAD, 对比验证). enabled 时调试转写台走
    # 流式 OnlineRecognizer 连续解码+端点检测,不影响对话链路(对话恒走 VAD+离线)。
    "stream": {"enabled": False, "model": "x-asr-zh-en", "endpoint_silence_s": 1.2},
    "presets": {
        "deepseek": {
            "api": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "transport": "openai_chat",
            "key_env": "DEEPSEEK_API_KEY",
            "pair": {
                # funasr, not sensevoice: every deployed preset already carries
                # funasr and sensevoice measures 12/14 near-field. This seeds a
                # fresh install only. Whether to move to qwen3 (most accurate,
                # 1.20s/seg) or paraformer (same exact count, 0.09s/seg) is a
                # latency call per brain — see docs/vad-asr-tuning.html §5.
                "asr": "funasr",
                # matcha default (2026-07-22): realtime offline (RTF 0.18), no
                # per-sentence network stalls; edge stays selectable, melo is fallback.
                "tts": {"engine": "matcha"},
            },
        },
    },
}

# Axis enumerations surfaced by GET /config so the GUI dropdowns need no hardcoding.
# The bare id lists stay the membership source of truth (switch executors check
# `x in ASR_ENGINES`); enums() decorates them with label/size metadata.
# funasr listed first (field-tested best: GPU ~0.65s/seg, punctuation, LLM decoder);
# funasr/whisper/qwen3 are heavy — run them with the vision service stopped.
# Order here = GUI dropdown order.
# Ranked by the 2026-07-25 comparison (five engines, one fixed recording, oracle
# cuts so the VAD is not a variable: docs/vad-asr-tuning.html). Near-field they
# tie at 14/14; the ranking is decided at distance, where qwen3 takes 13/14 at
# CER 0.036 and is the only one that hears 「停」. paraformer matches its exact
# count 13x faster but loses that same 「停」. funasr, previously listed first, is
# slower than paraformer AND less accurate than qwen3. whisper is not a choice.
ASR_ENGINES = ["qwen3", "paraformer", "funasr", "sensevoice", "whisper", "remote"]
# "remote" is the odd one out: not a model on this board but an address on the LAN.
# It sits in the same axis because from the turn's point of view it IS the ASR —
# same transcribe(samples)->text contract, same switch path, same drift accounting.
# Its endpoint lives in the top-level `remote_asr` block, NOT in the pair: a URL is
# deployment (like the board IP), and moving it must not take the engine-switch lock.
REMOTE_ASR_ENGINE = "remote"
# matcha first/default (realtime offline); edge online quality; melo resident fallback
TTS_ENGINES = ["matcha", "edge", "melo"]
# Ranked by the 2026-07-25 sweep (480 configs, three engines, one fixed recording:
# see docs/vad-asr-tuning.html). silero 13/14 near + 14/14 far at 4.8% idle
# misfire; ten 13/14 + 13/14 at 7.5%; fsmn 10/14 + 10/14 at 9.8% and — structural,
# not a tuning miss — unable to split two commands 0.8s apart in any of its 48
# configs. webrtc/energy are the dependency-free debug baselines, never a choice.
VAD_ENGINES = ["silero", "ten", "fsmn", "webrtc", "energy"]
# Streaming models (二级下拉 when 一级=流式). x-asr first/default: field-tested slightly
# ahead of zh-xlarge (2026-07-21), bilingual, and 2x faster (RTF 0.25 vs 0.56).
STREAM_MODELS = ["x-asr-zh-en", "zh-xlarge", "para-zh-en", "zh-2025", "multi-zh", "zh-en"]

# vad axis range guards + digital gain guard.
VAD_THRESHOLD_RANGE = (-90.0, 3.0)                 # energy uses dBFS (negative), silero 0..1
VAD_TIME_RANGE = (0.02, 30.0)                      # min_speech_s / min_silence_s seconds
VAD_PREROLL_RANGE = (0.0, 1.0)                     # pre-roll look-back seconds
AUDIO_GAIN_RANGE = (0.0, 30.0)                     # digital make-up gain, dB
STREAM_SILENCE_RANGE = (0.2, 5.0)                  # streaming endpoint trailing silence, s

# Per-engine display metadata for GET /config enums (GUI shows size on offline
# models). params_b = published parameter count in billions (null when no reliable
# public figure — never guessed); disk_mb = measured on the board (du -sm of the
# model dir under voice/models/), hard data.
# label carries the two numbers that decide the pick: remote-field accuracy and
# seconds per segment (both board-measured 2026-07-25 on the same 14 utterances).
ASR_META = {
    "qwen3": {"id": "qwen3", "label": "Qwen3-ASR-0.6B (最准,远场13/14,1.20s/段)",
              "params_b": 0.6, "disk_mb": 954},
    "paraformer": {"id": "paraformer", "label": "Paraformer-large zh (最快,13/14,0.09s/段)",
                   "params_b": 0.22, "disk_mb": 232},
    "funasr": {"id": "funasr", "label": "Fun-ASR-Nano 0.8B (GPU带标点,远场11/14,0.56s/段)",
               "params_b": 0.8, "disk_mb": 932},
    "sensevoice": {"id": "sensevoice", "label": "SenseVoice-Small (近场12/14,0.11s/段)",
                   "params_b": 0.234, "disk_mb": 229},
    "whisper": {"id": "whisper", "label": "Whisper large-v3-turbo (6/14,淘汰)",
                "params_b": 0.809, "disk_mb": 989},
    # No params_b/disk_mb on purpose: the size is whatever the other machine loaded,
    # and this file has never guessed a number it did not measure.
    "remote": {"id": "remote", "label": "电脑 ASR (局域网大模型,端侧不占内存)",
               "params_b": None, "disk_mb": None},
}
# Order stays offline-first: matcha leads because it is the only local engine that
# runs realtime, not because it sounds best. It does not — fed back through
# Fun-ASR, matcha reads at 13/14 CER 0.143 while edge-tts reads at 83/84 CER 0.012
# across six voices, and matcha re-renders each call (the same 「停」 came out
# 0.29 / 0.39 / 0.41 s), which also disqualifies it as a test signal.
TTS_META = {
    # board-measured RTF 0.18 @2t (melo is 1.60 — not realtime); model 93MB + vocos 52MB
    "matcha": {"id": "matcha", "label": "Matcha zh-en 离线(唯一实时,吐字偏糊)",
               "params_b": None, "disk_mb": 145},
    "edge": {"id": "edge", "label": "edge-tts 在线(最清晰,需联网)",
             "params_b": None, "disk_mb": None},
    "melo": {"id": "melo", "label": "MeloTTS zh-en (RTF 1.60,不实时)",
             "params_b": None, "disk_mb": 183},
}
# VAD engine display table for GET /config enums (disk_mb measured on the board;
# webrtc/energy carry no model so 0). default_threshold lets the GUI reset the
# threshold box to a sane value per engine (energy is a dBFS floor, not 0..1).
# `available` is filled in by the daemon (needs the board's sherpa build / webrtcvad).
VAD_META = {
    "silero": {"id": "silero", "label": "Silero VAD (实测最优)", "disk_mb": 2,
               "default_threshold": 0.3},
    "ten": {"id": "ten", "label": "TEN VAD (次优,低阈值易黏段)", "disk_mb": 1,
            "default_threshold": 0.5},
    # FSMN 自带内部状态机:threshold/min_silence 不外调,min_speech/pre_roll 有效
    "fsmn": {"id": "fsmn", "label": "FSMN-VAD (FunASR,切不开连续短指令)",
             "disk_mb": 2, "default_threshold": 0.5},
    "webrtc": {"id": "webrtc", "label": "WebRTC VAD (调试基线)", "disk_mb": 0,
               "default_threshold": 0.6},
    "energy": {"id": "energy", "label": "能量门 dBFS (调试基线)", "disk_mb": 0,
               "default_threshold": -45},
}

# Streaming model display table (二级下拉 when 一级=流式). disk_mb measured on the board.
STREAM_META = {
    "zh-2025": {"id": "zh-2025", "label": "zipformer zh 2025", "disk_mb": 300},
    "zh-xlarge": {"id": "zh-xlarge", "label": "zipformer zh xlarge 700M", "disk_mb": 737},
    "multi-zh": {"id": "multi-zh", "label": "multi-zh-hans 14k小时", "disk_mb": 250},
    "zh-en": {"id": "zh-en", "label": "双语 zh-en (弱/基线)", "disk_mb": 190},
    "x-asr-zh-en": {"id": "x-asr-zh-en", "label": "X-ASR 中英混 1M小时",
                    "disk_mb": 584},
    "para-zh-en": {"id": "para-zh-en", "label": "Paraformer-large 中英",
                   "disk_mb": 226},
}

# A small curated edge-tts zh voice table (the full table is fetched at P3).
EDGE_VOICES = [
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunxiaNeural",
    "zh-CN-liaoning-XiaobeiNeural",
    "zh-CN-shaanxi-XiaoniNeural",
    "zh-HK-HiuMaanNeural",
    "zh-TW-HsiaoChenNeural",
]

DEFAULT_PAIR = DEFAULT_CONFIG["presets"]["deepseek"]["pair"]


def merge_defaults(loaded):
    """Fill missing top-level keys from DEFAULT_CONFIG (shallow) so a partial hand-edited
    config still yields a complete, usable state. Never mutates the input."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if isinstance(loaded, dict):
        for k, v in loaded.items():
            cfg[k] = copy.deepcopy(v)
    # presets must contain at least the selected preset; fall back if it vanished.
    if not isinstance(cfg.get("presets"), dict) or not cfg["presets"]:
        cfg["presets"] = copy.deepcopy(DEFAULT_CONFIG["presets"])
    return cfg


def load_config(path=CONFIG_PATH):
    """Read config.json; missing/invalid -> DEFAULT (start anyway). Returns (config, source)
    where source in {'file','default'}."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return merge_defaults(raw), "file"
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULT_CONFIG), "default"


def save_config(config, path=CONFIG_PATH):
    """Atomic write-then-rename. Creates parent dir. voice-daemon is the sole writer."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def current_preset_name(config):
    return (config.get("brain") or {}).get("preset") or "deepseek"


def current_preset(config):
    """The selected preset dict, or the first available, or the default."""
    presets = config.get("presets") or {}
    name = current_preset_name(config)
    if name in presets:
        return presets[name]
    if presets:
        return next(iter(presets.values()))
    return copy.deepcopy(DEFAULT_CONFIG["presets"]["deepseek"])


def current_brain_kind(config):
    """"hermes" | "omni" — read off the selected preset, never off `brain`.

    Absent means "hermes": every preset that predates the omni brain is a hermes
    one, so old configs need no migration.
    """
    return (current_preset(config) or {}).get("kind") or "hermes"


def current_pair(config):
    """The persistent (asr, tts) pair for the selected preset, with defaults filled."""
    pair = copy.deepcopy(DEFAULT_PAIR)
    p = current_preset(config).get("pair")
    if isinstance(p, dict):
        if p.get("asr"):
            pair["asr"] = p["asr"]
        if isinstance(p.get("tts"), dict):
            pair["tts"] = copy.deepcopy(p["tts"])
    return pair


def apply_axis(config, axis, value):
    """By-axis whole replacement (no deep-merge guessing) — the GUI GETs the full axis,
    edits it, and POSTs it back. Returns a NEW config; never mutates the input.
      axis 'asr'         -> every preset's pair.asr (str)
      axis 'tts'         -> every preset's pair.tts (dict)
      axis 'vision_speak'-> top-level bool
      axis 'auto_listen' -> top-level bool (boot behaviour only)
      axis 'brain'       -> top-level brain dict (P2)
    """
    cfg = copy.deepcopy(config)
    if axis in ("asr", "tts"):
        # The Voice page's save is the only pair editor and it means "use this
        # engine/voice for conversations, whichever brain is selected" — so the
        # value fans out to every preset. Per-preset divergence has no UI to
        # create it; honoring it would silently revert engines on a brain switch.
        presets = cfg.setdefault("presets", {})
        if not presets:
            presets[current_preset_name(cfg)] = copy.deepcopy(
                DEFAULT_CONFIG["presets"]["deepseek"])
        for preset in presets.values():
            if not isinstance(preset, dict):
                continue
            pair = preset.setdefault("pair", copy.deepcopy(DEFAULT_PAIR))
            pair[axis] = copy.deepcopy(value)
    elif axis == "vision_speak":
        cfg["vision_speak"] = bool(value)
    elif axis == "auto_listen":
        # Boot-time only: read once in main() to decide LISTENING vs IDLE. Toggling
        # it never touches the running session — that would be a second, hidden way
        # to open/close the mic, competing with the one button that owns it.
        cfg["auto_listen"] = bool(value)
    elif axis == "vision_speak_limit":
        try:
            cfg["vision_speak_limit"] = max(1, int(value))
        except (TypeError, ValueError):
            cfg["vision_speak_limit"] = DEFAULT_CONFIG["vision_speak_limit"]
    elif axis == "brain":
        cfg["brain"] = copy.deepcopy(value)
    elif axis == "vision":
        # whole-axis replace; keep only a string model id (null clears it).
        mid = value.get("model") if isinstance(value, dict) else value
        cfg["vision"] = {"model": str(mid) if mid else None}
    elif axis == "vad":
        cfg["vad"] = normalize_vad(value)
    elif axis == "audio":
        gain = value.get("gain_db") if isinstance(value, dict) else value
        cfg["audio"] = {"gain_db": clamp_gain(gain)}
    elif axis == "remote_asr":
        cfg["remote_asr"] = normalize_remote_asr(value)
    elif axis == "stream":
        cfg["stream"] = normalize_stream(value)
    else:
        raise ValueError(f"unknown axis: {axis}")
    return cfg


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def clamp_gain(v):
    """Digital make-up gain clamped to [0,30] dB; junk -> 0 (identity)."""
    return _clamp(v, AUDIO_GAIN_RANGE[0], AUDIO_GAIN_RANGE[1], 0.0)


def normalize_vad(value):
    """Whole-axis vad value with unknown engine / out-of-range numbers coerced back to
    defaults (never rejects — a bad hand-edit still yields a usable, safe front-end)."""
    d = value if isinstance(value, dict) else {}
    out = copy.deepcopy(DEFAULT_CONFIG["vad"])
    if d.get("engine") in VAD_ENGINES:
        out["engine"] = d["engine"]
    if "threshold" in d:
        out["threshold"] = _clamp(d["threshold"], VAD_THRESHOLD_RANGE[0],
                                  VAD_THRESHOLD_RANGE[1], out["threshold"])
    for k in ("min_speech_s", "min_silence_s"):
        if k in d:
            out[k] = _clamp(d[k], VAD_TIME_RANGE[0], VAD_TIME_RANGE[1], out[k])
    if "pre_roll_s" in d:
        out["pre_roll_s"] = _clamp(d["pre_roll_s"], VAD_PREROLL_RANGE[0],
                                   VAD_PREROLL_RANGE[1], out["pre_roll_s"])
    return out


def current_vad(config):
    """The persistent vad axis with defaults filled + guards applied."""
    return normalize_vad(config.get("vad"))


def normalize_stream(value):
    """Whole-axis stream value: enabled bool + model(whitelist) + endpoint_silence_s
    clamped. Bad hand-edit coerces back to defaults (never rejects)."""
    d = value if isinstance(value, dict) else {}
    out = copy.deepcopy(DEFAULT_CONFIG["stream"])
    out["enabled"] = bool(d.get("enabled", out["enabled"]))
    if d.get("model") in STREAM_MODELS:
        out["model"] = d["model"]
    if "endpoint_silence_s" in d:
        out["endpoint_silence_s"] = _clamp(d["endpoint_silence_s"], STREAM_SILENCE_RANGE[0],
                                           STREAM_SILENCE_RANGE[1], out["endpoint_silence_s"])
    return out


def normalize_remote_asr(value):
    """Whole-axis remote_asr value: url + model, both trimmed. An http(s) scheme is
    added when the operator typed a bare host:port (they will), a trailing slash is
    dropped, and anything unparseable becomes "" — i.e. "not configured", which the
    engine refuses loudly. Never raises: a bad hand-edit must not brick the daemon."""
    d = value if isinstance(value, dict) else {"url": value}
    out = copy.deepcopy(DEFAULT_CONFIG["remote_asr"])
    url = str(d.get("url") or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    if url and not url.startswith(("http://", "https://")):
        url = ""
    out["url"] = url
    model = str(d.get("model") or "").strip()
    if model:
        out["model"] = model
    return out


def current_remote_asr(config):
    """The persistent remote_asr block with defaults filled + guards applied."""
    return normalize_remote_asr(config.get("remote_asr"))


def current_stream(config):
    """The persistent stream axis with defaults filled + guards applied."""
    return normalize_stream(config.get("stream"))


def current_audio_gain(config):
    """Persistent digital make-up gain (dB), guarded. 0 = identity."""
    a = config.get("audio")
    return clamp_gain(a.get("gain_db") if isinstance(a, dict) else 0)


def effective_pair(config, override=None):
    """Running pair = persistent pair with an ephemeral override laid on top (debug page).
    override is {'asr'?: str, 'tts'?: dict} or None. Pure — no side effects."""
    pair = current_pair(config)
    if override:
        if override.get("asr"):
            pair["asr"] = override["asr"]
        if isinstance(override.get("tts"), dict):
            pair["tts"] = copy.deepcopy(override["tts"])
    return pair


def compute_drift(desired_pair, applied):
    """Diff desired (config) vs applied (actually running). Empty dict = no drift.
    applied is {'asr': str, 'tts_engine': str, 'edge_voice': str|None}."""
    drift = {}
    dtts = desired_pair.get("tts") or {}
    if desired_pair.get("asr") != applied.get("asr"):
        drift["asr"] = {"desired": desired_pair.get("asr"), "applied": applied.get("asr")}
    if dtts.get("engine") != applied.get("tts_engine"):
        drift["tts_engine"] = {"desired": dtts.get("engine"),
                               "applied": applied.get("tts_engine")}
    # voice only meaningful for edge
    if applied.get("tts_engine") == "edge" and dtts.get("engine") == "edge":
        if dtts.get("voice") != applied.get("edge_voice"):
            drift["edge_voice"] = {"desired": dtts.get("voice"),
                                   "applied": applied.get("edge_voice")}
    return drift


def enums():
    """Axis enums for GET /config. asr/tts entries are objects
    {id, label, params_b, disk_mb} (offline models carry size; online ones null);
    edge_voices stays a bare string list."""
    return {"asr": [copy.deepcopy(ASR_META[k]) for k in ASR_ENGINES],
            "tts": [copy.deepcopy(TTS_META[k]) for k in TTS_ENGINES],
            "vad": [copy.deepcopy(VAD_META[k]) for k in VAD_ENGINES],
            "stream": [copy.deepcopy(STREAM_META[k]) for k in STREAM_MODELS],
            "edge_voices": list(EDGE_VOICES)}

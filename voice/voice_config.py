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
    "brain": {"kind": "hermes", "preset": "deepseek"},
    "omni": {"url": "http://192.168.13.2:8093"},
    "vision_speak": False,
    "vision_speak_limit": 300,          # spoken-caption cap, Python len() chars
    "vision": {"model": None},          # desired VLM model id; null = use board env as-is
    # Audio front-end (global, not part of a preset pair). vad = the switchable segmenter;
    # audio.gain_db = digital make-up gain (0 = identity, stock behaviour unchanged).
    "vad": {"engine": "silero", "threshold": 0.5,
            "min_speech_s": 0.25, "min_silence_s": 0.55, "pre_roll_s": 0.45},
    "audio": {"gain_db": 0},
    "presets": {
        "deepseek": {
            "api": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "transport": "openai_chat",
            "key_env": "DEEPSEEK_API_KEY",
            "pair": {
                "asr": "sensevoice",
                "tts": {"engine": "edge", "voice": "zh-CN-XiaoxiaoNeural",
                        "fallback": "melo"},
            },
        },
    },
}

# Axis enumerations surfaced by GET /config so the GUI dropdowns need no hardcoding.
# The bare id lists stay the membership source of truth (switch executors check
# `x in ASR_ENGINES`); enums() decorates them with label/size metadata.
ASR_ENGINES = ["sensevoice"]                       # P3 adds zipformer
TTS_ENGINES = ["edge", "melo"]
VAD_ENGINES = ["silero", "ten", "webrtc", "energy"]

# vad axis range guards + digital gain guard.
VAD_THRESHOLD_RANGE = (-90.0, 3.0)                 # energy uses dBFS (negative), silero 0..1
VAD_TIME_RANGE = (0.02, 30.0)                      # min_speech_s / min_silence_s seconds
VAD_PREROLL_RANGE = (0.0, 1.0)                     # pre-roll look-back seconds
AUDIO_GAIN_RANGE = (0.0, 30.0)                     # digital make-up gain, dB

# Per-engine display metadata for GET /config enums (GUI shows size on offline
# models). params_b = published parameter count in billions (null when no reliable
# public figure — never guessed); disk_mb = measured on the board (du -sm of the
# model dir under voice/models/), hard data.
ASR_META = {
    "sensevoice": {"id": "sensevoice", "label": "SenseVoice-Small",
                   "params_b": 0.234, "disk_mb": 229},
}
TTS_META = {
    "edge": {"id": "edge", "label": "edge-tts 在线",
             "params_b": None, "disk_mb": None},
    "melo": {"id": "melo", "label": "MeloTTS zh-en",
             "params_b": None, "disk_mb": 183},
}
# VAD engine display table for GET /config enums (disk_mb measured on the board;
# webrtc/energy carry no model so 0). default_threshold lets the GUI reset the
# threshold box to a sane value per engine (energy is a dBFS floor, not 0..1).
# `available` is filled in by the daemon (needs the board's sherpa build / webrtcvad).
VAD_META = {
    "silero": {"id": "silero", "label": "Silero VAD", "disk_mb": 2,
               "default_threshold": 0.5},
    "ten": {"id": "ten", "label": "TEN VAD", "disk_mb": 1,
            "default_threshold": 0.5},
    "webrtc": {"id": "webrtc", "label": "WebRTC VAD", "disk_mb": 0,
               "default_threshold": 0.6},
    "energy": {"id": "energy", "label": "能量门 dBFS", "disk_mb": 0,
               "default_threshold": -45},
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
      axis 'asr'         -> current preset pair.asr (str)
      axis 'tts'         -> current preset pair.tts (dict)
      axis 'vision_speak'-> top-level bool
      axis 'brain'       -> top-level brain dict (P2)
    """
    cfg = copy.deepcopy(config)
    if axis in ("asr", "tts"):
        name = current_preset_name(cfg)
        presets = cfg.setdefault("presets", {})
        preset = presets.setdefault(name, copy.deepcopy(DEFAULT_CONFIG["presets"]["deepseek"]))
        pair = preset.setdefault("pair", copy.deepcopy(DEFAULT_PAIR))
        pair[axis] = copy.deepcopy(value)
    elif axis == "vision_speak":
        cfg["vision_speak"] = bool(value)
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
            "edge_voices": list(EDGE_VOICES)}

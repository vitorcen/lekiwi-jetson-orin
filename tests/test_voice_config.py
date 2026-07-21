"""Pure config logic: by-axis whole replacement, default merge, effective pair,
drift. No aiohttp/sherpa/hardware — only voice_config."""
import copy

import voice_config as vc


def test_merge_defaults_fills_missing_and_keeps_overrides():
    cfg = vc.merge_defaults({"vision_speak": True})
    assert cfg["vision_speak"] is True
    assert cfg["brain"]["kind"] == "hermes"          # filled from default
    assert "deepseek" in cfg["presets"]


def test_merge_defaults_empty_and_garbage_never_crash():
    for junk in ({}, None, [], "nope", 42):
        cfg = vc.merge_defaults(junk)
        assert cfg["presets"], "must always yield a usable preset"


def test_merge_defaults_does_not_mutate_input():
    src = {"vision_speak": True}
    vc.merge_defaults(src)
    assert src == {"vision_speak": True}


def test_apply_axis_tts_replaces_only_current_preset_pair():
    cfg = vc.merge_defaults({})
    new = vc.apply_axis(cfg, "tts", {"engine": "melo"})
    assert new["presets"]["deepseek"]["pair"]["tts"] == {"engine": "melo"}
    # asr untouched, other top-level untouched, input immutable
    assert new["presets"]["deepseek"]["pair"]["asr"] == "sensevoice"
    assert cfg["presets"]["deepseek"]["pair"]["tts"]["engine"] == "edge"


def test_apply_axis_asr():
    cfg = vc.merge_defaults({})
    new = vc.apply_axis(cfg, "asr", "sensevoice")
    assert new["presets"]["deepseek"]["pair"]["asr"] == "sensevoice"


def test_apply_axis_asr_paraformer_roundtrips():
    cfg = vc.merge_defaults({})
    new = vc.apply_axis(cfg, "asr", "paraformer")
    assert new["presets"]["deepseek"]["pair"]["asr"] == "paraformer"
    assert "paraformer" in vc.ASR_ENGINES


def test_default_has_stream_axis():
    cfg = vc.merge_defaults({})
    assert cfg["stream"] == {"enabled": False, "model": "zh-2025",
                             "endpoint_silence_s": 1.2}


def test_apply_axis_stream_boolifies_clamps_and_whitelists_model():
    cfg = vc.merge_defaults({})
    new = vc.apply_axis(cfg, "stream",
                        {"enabled": 1, "model": "multi-zh", "endpoint_silence_s": 99})
    assert new["stream"]["enabled"] is True
    assert new["stream"]["model"] == "multi-zh"
    assert new["stream"]["endpoint_silence_s"] == vc.STREAM_SILENCE_RANGE[1]  # clamped
    # unknown model → falls back to default, not rejected
    bad = vc.apply_axis(cfg, "stream", {"enabled": 0, "model": "nonsense"})
    assert bad["stream"]["model"] == vc.DEFAULT_CONFIG["stream"]["model"]


def test_enums_expose_stream_models():
    e = vc.enums()
    ids = [s["id"] for s in e["stream"]]
    assert ids == vc.STREAM_MODELS
    assert e["stream"][0]["id"] == "zh-2025"      # default listed first


def test_current_stream_garbage_falls_back():
    assert vc.current_stream({"stream": "nonsense"}) == vc.DEFAULT_CONFIG["stream"]
    assert vc.current_stream({}) == vc.DEFAULT_CONFIG["stream"]


def test_enums_expose_paraformer_with_size():
    e = vc.enums()
    asr = {a["id"]: a for a in e["asr"]}
    assert "paraformer" in asr
    assert asr["paraformer"]["label"] == "Paraformer-large zh"
    assert asr["paraformer"]["params_b"] and asr["paraformer"]["disk_mb"]


def test_apply_axis_vision_speak_is_boolified():
    cfg = vc.merge_defaults({})
    assert vc.apply_axis(cfg, "vision_speak", 1)["vision_speak"] is True
    assert vc.apply_axis(cfg, "vision_speak", 0)["vision_speak"] is False


def test_default_has_vision_speak_limit():
    cfg = vc.merge_defaults({})
    assert cfg["vision_speak_limit"] == 300


def test_default_has_vision_axis_null_model():
    cfg = vc.merge_defaults({})
    assert cfg["vision"] == {"model": None}


def test_apply_axis_vision_sets_and_clears_model():
    cfg = vc.merge_defaults({})
    new = vc.apply_axis(cfg, "vision", {"model": "Qwen3-VL-2B-Instruct-Q4_K_M"})
    assert new["vision"] == {"model": "Qwen3-VL-2B-Instruct-Q4_K_M"}
    # null / empty clears back to None; input never mutated
    assert vc.apply_axis(cfg, "vision", {"model": None})["vision"] == {"model": None}
    assert vc.apply_axis(cfg, "vision", {"model": ""})["vision"] == {"model": None}
    assert cfg["vision"] == {"model": None}


def test_apply_axis_vision_speak_limit_is_int_and_clamped():
    cfg = vc.merge_defaults({})
    assert vc.apply_axis(cfg, "vision_speak_limit", "500")["vision_speak_limit"] == 500
    assert vc.apply_axis(cfg, "vision_speak_limit", 0)["vision_speak_limit"] == 1
    # garbage falls back to the default rather than crashing
    assert vc.apply_axis(cfg, "vision_speak_limit", "x")["vision_speak_limit"] == 300


def test_apply_axis_unknown_raises():
    try:
        vc.apply_axis(vc.merge_defaults({}), "bogus", 1)
        assert False, "should raise"
    except ValueError:
        pass


def test_apply_axis_targets_selected_preset_not_default():
    cfg = vc.merge_defaults({})
    cfg["presets"]["mimo"] = copy.deepcopy(cfg["presets"]["deepseek"])
    cfg["brain"]["preset"] = "mimo"
    new = vc.apply_axis(cfg, "tts", {"engine": "melo"})
    assert new["presets"]["mimo"]["pair"]["tts"] == {"engine": "melo"}
    assert new["presets"]["deepseek"]["pair"]["tts"]["engine"] == "edge"  # deepseek untouched


def test_effective_pair_override_wins_but_config_persists():
    cfg = vc.merge_defaults({})
    eff = vc.effective_pair(cfg, {"tts": {"engine": "melo"}})
    assert eff["tts"]["engine"] == "melo"
    # config itself unchanged (ephemeral, not persisted)
    assert vc.current_pair(cfg)["tts"]["engine"] == "edge"


def test_effective_pair_no_override_is_config_pair():
    cfg = vc.merge_defaults({})
    assert vc.effective_pair(cfg, None) == vc.current_pair(cfg)


def test_compute_drift_detects_ephemeral_override():
    desired = vc.current_pair(vc.merge_defaults({}))       # edge/Xiaoxiao
    applied = {"asr": "sensevoice", "tts_engine": "melo", "edge_voice": None}
    drift = vc.compute_drift(desired, applied)
    assert "tts_engine" in drift and drift["tts_engine"]["applied"] == "melo"


def test_compute_drift_empty_when_aligned():
    desired = vc.current_pair(vc.merge_defaults({}))
    applied = {"asr": "sensevoice", "tts_engine": "edge",
               "edge_voice": desired["tts"]["voice"]}
    assert vc.compute_drift(desired, applied) == {}


def test_enums_are_metadata_objects():
    e = vc.enums()
    # asr/tts entries are {id,label,params_b,disk_mb} objects, edge_voices stays strings
    asr = {a["id"]: a for a in e["asr"]}
    assert asr["sensevoice"]["label"] == "SenseVoice-Small"
    assert asr["sensevoice"]["params_b"] == 0.234 and asr["sensevoice"]["disk_mb"] == 229
    assert e["asr"][0]["id"] == "qwen3"        # qwen3 listed first (headline engine)
    tts = {t["id"]: t for t in e["tts"]}
    assert tts["edge"]["params_b"] is None and tts["edge"]["disk_mb"] is None  # online
    assert tts["melo"]["disk_mb"] == 183                                       # offline
    assert all(isinstance(v, str) for v in e["edge_voices"])
    # id lists remain the membership source of truth for the switch executors
    assert [a["id"] for a in e["asr"]] == vc.ASR_ENGINES


# ---- vad axis (switchable segmenter, global audio front-end) ---------------
def test_default_has_vad_and_audio_axes():
    cfg = vc.merge_defaults({})
    assert cfg["vad"] == {"engine": "silero", "threshold": 0.5,
                          "min_speech_s": 0.25, "min_silence_s": 0.55,
                          "pre_roll_s": 0.45}
    assert cfg["audio"] == {"gain_db": 0}


def test_apply_axis_vad_preroll_clamped_to_unit():
    cfg = vc.merge_defaults({})
    assert vc.apply_axis(cfg, "vad", {"pre_roll_s": 0.3})["vad"]["pre_roll_s"] == 0.3
    assert vc.apply_axis(cfg, "vad", {"pre_roll_s": 9})["vad"]["pre_roll_s"] == 1.0
    assert vc.apply_axis(cfg, "vad", {"pre_roll_s": -1})["vad"]["pre_roll_s"] == 0.0


def test_enums_vad_table_and_energy_webrtc_zero_disk():
    e = vc.enums()
    vad = {v["id"]: v for v in e["vad"]}
    assert [v["id"] for v in e["vad"]] == vc.VAD_ENGINES
    assert vad["energy"]["disk_mb"] == 0 and vad["webrtc"]["disk_mb"] == 0
    assert vad["silero"]["disk_mb"] == 2
    assert vad["energy"]["default_threshold"] == -45      # dBFS floor, not 0..1


def test_apply_axis_vad_normalizes_engine_and_ranges():
    cfg = vc.merge_defaults({})
    new = vc.apply_axis(cfg, "vad", {"engine": "energy", "threshold": -45,
                                     "min_speech_s": 0.3, "min_silence_s": 0.6})
    assert new["vad"]["engine"] == "energy"
    assert new["vad"]["threshold"] == -45
    assert new["vad"]["min_speech_s"] == 0.3
    # unknown engine falls back to default silero (never rejects a hand-edit)
    assert vc.apply_axis(cfg, "vad", {"engine": "bogus"})["vad"]["engine"] == "silero"
    # out-of-range time clamped into [0.02, 30]
    assert vc.apply_axis(cfg, "vad", {"min_speech_s": 999})["vad"]["min_speech_s"] == 30.0
    assert cfg["vad"]["engine"] == "silero"               # input immutable


def test_apply_axis_audio_gain_clamped():
    cfg = vc.merge_defaults({})
    assert vc.apply_axis(cfg, "audio", {"gain_db": 20})["audio"]["gain_db"] == 20.0
    assert vc.apply_axis(cfg, "audio", {"gain_db": 99})["audio"]["gain_db"] == 30.0
    assert vc.apply_axis(cfg, "audio", {"gain_db": -5})["audio"]["gain_db"] == 0.0
    assert vc.apply_axis(cfg, "audio", "junk")["audio"]["gain_db"] == 0.0


def test_current_vad_and_gain_accessors():
    cfg = vc.merge_defaults({"vad": {"engine": "webrtc", "threshold": 0.6},
                             "audio": {"gain_db": 18}})
    assert vc.current_vad(cfg)["engine"] == "webrtc"
    assert vc.current_vad(cfg)["min_silence_s"] == 0.55    # default filled
    assert vc.current_audio_gain(cfg) == 18.0
    assert vc.current_audio_gain(vc.merge_defaults({})) == 0.0

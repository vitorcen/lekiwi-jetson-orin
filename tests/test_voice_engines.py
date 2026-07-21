"""Engine-registry wiring (import-clean: sherpa_onnx is imported lazily inside
load()/transcribe(), never at module top, so this runs off-board)."""
import voice_engines as ve
import voice_config as vc


def test_registry_asr_has_all_hosts():
    assert set(ve.REGISTRY["asr"]) == {"sensevoice", "paraformer", "whisper", "qwen3"}


def test_host_classes_carry_matching_names():
    for name, cls in ve.REGISTRY["asr"].items():
        assert cls.name == name              # class.name is the axis id


def test_registry_matches_config_enum_membership():
    # the switch executor checks `x in ASR_ENGINES`; every host must be enumerated
    assert set(ve.REGISTRY["asr"]) == set(vc.ASR_ENGINES)


def test_paraformer_host_unloaded_by_default():
    host = ve.OfflineParaformer()
    assert host.loaded is False          # no model touched until load()

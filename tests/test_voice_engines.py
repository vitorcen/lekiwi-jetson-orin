"""Engine-registry wiring (import-clean: sherpa_onnx is imported lazily inside
load()/transcribe(), never at module top, so this runs off-board)."""
import voice_engines as ve
import voice_config as vc


def test_registry_asr_has_all_hosts():
    assert set(ve.REGISTRY["asr"]) == {"sensevoice", "paraformer", "whisper", "qwen3",
                                       "funasr"}


def test_host_classes_carry_matching_names():
    for name, cls in ve.REGISTRY["asr"].items():
        assert cls.name == name              # class.name is the axis id


def test_registry_matches_config_enum_membership():
    # the switch executor checks `x in ASR_ENGINES`; every host must be enumerated
    assert set(ve.REGISTRY["asr"]) == set(vc.ASR_ENGINES)


def test_paraformer_host_unloaded_by_default():
    host = ve.OfflineParaformer()
    assert host.loaded is False          # no model touched until load()


def test_registry_tts_hosts_and_sample_rates():
    # 'edge' is a playback mode over melo, so only model-owning hosts appear here
    assert set(ve.REGISTRY["tts"]) == {"melo", "matcha"}
    assert set(vc.TTS_ENGINES) == {"edge"} | set(ve.REGISTRY["tts"])
    assert ve.MeloTts.sample_rate == 44100
    assert ve.MatchaTts.sample_rate == 16000     # aplay rate follows the host
    for name, cls in ve.REGISTRY["tts"].items():
        assert cls.name == name
        assert cls().loaded is False

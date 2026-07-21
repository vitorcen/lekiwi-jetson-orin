"""Pure VLM model scanning / mmproj pairing / env-file logic. No aiohttp/GPU —
only vlm_models + a tmp dir of empty .gguf files."""
import os

import vlm_models as vm


def _touch(d, name, size=1):
    p = os.path.join(d, name)
    with open(p, "wb") as fh:
        fh.write(b"\0" * size)
    return p


# ---- family / mmproj detection -------------------------------------------
def test_is_mmproj():
    assert vm.is_mmproj("mmproj-Qwen3-VL-2B-Instruct-Q8_0.gguf")
    assert vm.is_mmproj("MMPROJ-foo.gguf")            # case-insensitive
    assert not vm.is_mmproj("Qwen3-VL-2B-Instruct-Q4_K_M.gguf")


def test_family_strips_quant_token():
    assert vm._family("Qwen3-VL-2B-Instruct-Q4_K_M") == "qwen3-vl-2b-instruct"
    assert vm._family("Qwen3-VL-2B-Instruct-Q8_0") == "qwen3-vl-2b-instruct"
    assert vm._family("Foo-F16") == "foo"


# ---- pairing --------------------------------------------------------------
def test_pair_exact_family_ignoring_quant(tmp_path):
    d = str(tmp_path)
    _touch(d, "Qwen3-VL-2B-Instruct-Q4_K_M.gguf")
    _touch(d, "mmproj-Qwen3-VL-2B-Instruct-Q8_0.gguf")
    models, mmprojs = vm.scan_dir(d)
    assert vm.pair_mmproj(models[0], mmprojs) == "mmproj-Qwen3-VL-2B-Instruct-Q8_0.gguf"


def test_pair_sole_mmproj_fallback(tmp_path):
    # families differ, but a single mmproj in the dir is used for all models.
    d = str(tmp_path)
    _touch(d, "SomeOther-Model-Q4_0.gguf")
    _touch(d, "mmproj-Qwen3-VL-2B-Instruct-Q8_0.gguf")
    models, mmprojs = vm.scan_dir(d)
    assert vm.pair_mmproj(models[0], mmprojs) == "mmproj-Qwen3-VL-2B-Instruct-Q8_0.gguf"


def test_pair_none_when_no_mmproj(tmp_path):
    d = str(tmp_path)
    _touch(d, "Qwen3-VL-2B-Instruct-Q4_K_M.gguf")
    models, mmprojs = vm.scan_dir(d)
    assert vm.pair_mmproj(models[0], mmprojs) is None


def test_pair_ambiguous_multiple_mmproj_no_family(tmp_path):
    # two mmproj, neither matching this model's family -> unpairable (not a guess).
    d = str(tmp_path)
    _touch(d, "Lonely-Model-Q4_0.gguf")
    _touch(d, "mmproj-Alpha-Q8_0.gguf")
    _touch(d, "mmproj-Beta-Q8_0.gguf")
    models, mmprojs = vm.scan_dir(d)
    assert vm.pair_mmproj("Lonely-Model-Q4_0.gguf", mmprojs) is None


def test_pair_two_families_each_matches_own_mmproj(tmp_path):
    d = str(tmp_path)
    _touch(d, "Alpha-VL-2B-Q4_K_M.gguf")
    _touch(d, "Beta-VL-7B-Q4_K_M.gguf")
    _touch(d, "mmproj-Alpha-VL-2B-Q8_0.gguf")
    _touch(d, "mmproj-Beta-VL-7B-Q8_0.gguf")
    _, mmprojs = vm.scan_dir(d)
    assert vm.pair_mmproj("Alpha-VL-2B-Q4_K_M.gguf", mmprojs) == "mmproj-Alpha-VL-2B-Q8_0.gguf"
    assert vm.pair_mmproj("Beta-VL-7B-Q4_K_M.gguf", mmprojs) == "mmproj-Beta-VL-7B-Q8_0.gguf"


# ---- scan / listing -------------------------------------------------------
def test_scan_excludes_mmproj_and_nongguf(tmp_path):
    d = str(tmp_path)
    _touch(d, "Model-Q4_K_M.gguf")
    _touch(d, "mmproj-Model-Q8_0.gguf")
    _touch(d, "SHA256SUMS")
    _touch(d, "readme.txt")
    models, mmprojs = vm.scan_dir(d)
    assert models == ["Model-Q4_K_M.gguf"]
    assert mmprojs == ["mmproj-Model-Q8_0.gguf"]


def test_scan_missing_dir_is_empty():
    assert vm.scan_dir("/no/such/dir/here") == ([], [])


def test_list_models_marks_usable_and_active(tmp_path):
    d = str(tmp_path)
    model = _touch(d, "Model-Q4_K_M.gguf", size=1024 * 1024 * 3)
    _touch(d, "mmproj-Model-Q8_0.gguf")
    _touch(d, "NoProj-Q4_0.gguf")            # a second family with no mmproj match
    out = {m["id"]: m for m in vm.list_models(d, active_model_file=model)}
    assert out["Model-Q4_K_M"]["usable"] is True
    assert out["Model-Q4_K_M"]["active"] is True
    assert out["Model-Q4_K_M"]["disk_mb"] == 3
    # sole-mmproj fallback pairs NoProj too (only one mmproj present)
    assert out["NoProj-Q4_0"]["usable"] is True
    assert out["NoProj-Q4_0"]["active"] is False


def test_list_models_unusable_when_no_mmproj(tmp_path):
    d = str(tmp_path)
    _touch(d, "Model-Q4_K_M.gguf")           # no mmproj at all
    out = vm.list_models(d)
    assert out[0]["usable"] is False
    assert out[0]["mmproj"] is None


# ---- env file round-trip --------------------------------------------------
def test_build_and_parse_env_roundtrip():
    txt = vm.build_env("/m/model.gguf", "/m/mmproj.gguf")
    env = vm.parse_env(txt)
    assert env["VLM_MODEL"] == "/m/model.gguf"
    assert env["VLM_MMPROJ"] == "/m/mmproj.gguf"


def test_parse_env_ignores_blank_and_comments():
    env = vm.parse_env("# comment\n\nVLM_MODEL=/a.gguf\n  VLM_MMPROJ = /b.gguf \n")
    assert env == {"VLM_MODEL": "/a.gguf", "VLM_MMPROJ": "/b.gguf"}


def test_active_model_id():
    assert vm.active_model_id("/home/jetson/models/vlm/Qwen3-VL-2B-Instruct-Q4_K_M.gguf") \
        == "Qwen3-VL-2B-Instruct-Q4_K_M"
    assert vm.active_model_id(None) is None

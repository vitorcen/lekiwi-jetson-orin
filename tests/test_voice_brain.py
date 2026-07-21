"""Pure brain-switch logic: yaml patch planning + preset validation.

Needs ruamel.yaml (the round-trip patcher). Run:
  uv run --with pytest --with numpy --with ruamel.yaml pytest tests/ -q
"""
import copy

import pytest

import voice_brain as vb

# A faithful mirror of the board profile config.yaml: comments, the
# disabled_toolsets kill-list, and the mcp_servers safety chain must survive
# every patch untouched.
YAML = """\
# LeKiwi robot profile — Hermes v0.18.2, fields verified against source.
# Security: toolset trimming is layer 1 of 4.

model:
  provider: "custom:deepseek"
  default: "deepseek-v4-flash"

providers:
  deepseek:
    api: "https://api.deepseek.com"
    key_env: "DEEPSEEK_API_KEY"
    default_model: "deepseek-v4-flash"
    transport: "openai_chat"

agent:
  max_turns: 40
  # Kill-list applied last, overrides everything.
  disabled_toolsets: [terminal, file, browser, cronjob, skills_hub]

mcp_servers:
  vlm:
    command: /home/jetson/vlm/.venv/bin/python
    args: ["/home/jetson/vlm/mcp_server.py"]
    timeout: 60
  drive:
    command: /home/jetson/vlm/.venv/bin/python
    args: ["/home/jetson/drive/mcp_server.py"]
    timeout: 30
"""

DEEPSEEK = {"api": "https://api.deepseek.com", "model": "deepseek-v4-flash",
            "transport": "openai_chat", "key_env": "DEEPSEEK_API_KEY"}
MIMO = {"api": "https://api.mimo.example.com", "model": "mimo-v2.5-pro",
        "transport": "openai_chat", "key_env": "MIMO_API_KEY"}


def _load(text):
    return vb._yaml().load(text)


# --------------------------------------------------------------------------- #
# patch planning
# --------------------------------------------------------------------------- #
def test_patch_upserts_new_provider_and_sets_model():
    out = vb.plan_yaml_patch(YAML, "mimo", MIMO)
    d = _load(out)
    assert d["model"]["provider"] == "custom:mimo"
    assert d["model"]["default"] == "mimo-v2.5-pro"
    # new provider block inserted with all four fields
    assert d["providers"]["mimo"]["api"] == "https://api.mimo.example.com"
    assert d["providers"]["mimo"]["key_env"] == "MIMO_API_KEY"
    assert d["providers"]["mimo"]["default_model"] == "mimo-v2.5-pro"
    assert d["providers"]["mimo"]["transport"] == "openai_chat"
    # the original deepseek provider is left in place
    assert d["providers"]["deepseek"]["api"] == "https://api.deepseek.com"


def test_patch_existing_provider_is_idempotent_and_only_moves_model_default():
    # switch to a deepseek preset whose model differs only in the default pointer
    preset = dict(DEEPSEEK, model="deepseek-v4-pro")
    out = vb.plan_yaml_patch(YAML, "deepseek", preset)
    d = _load(out)
    assert d["model"]["provider"] == "custom:deepseek"
    assert d["model"]["default"] == "deepseek-v4-pro"
    # existing provider block identity fields untouched (no duplicate/overwrite)
    assert d["providers"]["deepseek"]["api"] == "https://api.deepseek.com"
    assert d["providers"]["deepseek"]["key_env"] == "DEEPSEEK_API_KEY"


def test_patch_refuses_existing_provider_field_mismatch():
    bad = dict(DEEPSEEK, api="https://evil.example.com")  # api disagrees with yaml
    with pytest.raises(vb.BrainError):
        vb.plan_yaml_patch(YAML, "deepseek", bad)


def test_patch_leaves_safety_chain_structurally_identical():
    before = _load(YAML)
    out = vb.plan_yaml_patch(YAML, "mimo", MIMO)
    after = _load(out)
    # the two keys the switch must never touch
    assert list(after["agent"]["disabled_toolsets"]) == \
        list(before["agent"]["disabled_toolsets"])
    assert vb._to_plain(after["mcp_servers"]) == vb._to_plain(before["mcp_servers"])
    assert after["agent"]["max_turns"] == before["agent"]["max_turns"]


def test_patch_preserves_comments():
    out = vb.plan_yaml_patch(YAML, "mimo", MIMO)
    assert "Kill-list applied last" in out
    assert "layer 1 of 4" in out


def test_structure_diff_guard_fires_on_tampered_yaml():
    # A model block that also carries an extra key is fine to strip; but if the
    # patch (hypothetically) changed a non-allowed key, the guard must catch it.
    # We exercise the guard directly via _strip_allowed round-trip equality.
    before = vb._strip_allowed(_load(YAML), "mimo")
    tampered = _load(YAML)
    tampered["agent"]["disabled_toolsets"] = ["terminal"]  # simulate leakage
    after = vb._strip_allowed(tampered, "mimo")
    assert before != after


def test_patch_missing_model_or_providers_raises():
    with pytest.raises(vb.BrainError):
        vb.plan_yaml_patch("agent:\n  max_turns: 1\n", "mimo", MIMO)


# --------------------------------------------------------------------------- #
# preset validation
# --------------------------------------------------------------------------- #
def test_validate_good_cloud_preset():
    vb.validate_preset("deepseek", DEEPSEEK)      # no raise
    vb.validate_preset("mimo", MIMO)


@pytest.mark.parametrize("mutate", [
    {"api": "http://api.deepseek.com"},           # not https
    {"api": "https://localhost"},                 # localhost
    {"api": "https://127.0.0.1"},                 # loopback
    {"api": "https://169.254.169.254"},           # cloud metadata / link-local
    {"api": "https://192.168.1.5"},               # bare LAN IP (cloud forbids)
    {"api": "https://8.8.8.8"},                   # bare public IP (needs domain)
    {"api": "https://nodots"},                    # no public domain
    {"model": "bad model name"},                  # space in model
    {"key_env": "lowercase"},                     # key_env must be UPPER_SNAKE
    {"transport": "grpc"},                        # not whitelisted
])
def test_validate_rejects(mutate):
    preset = dict(DEEPSEEK, **mutate)
    with pytest.raises(vb.BrainError):
        vb.validate_preset("deepseek", preset)


def test_validate_bad_provider_name():
    with pytest.raises(vb.BrainError):
        vb.validate_preset("Bad Name", DEEPSEEK)


def test_validate_allow_lan_permits_private_ip():
    lan = dict(DEEPSEEK, api="https://192.168.13.2")
    vb.validate_preset("omni", lan, allow_lan=True)   # no raise
    # but a link-local address is blocked even with allow_lan
    with pytest.raises(vb.BrainError):
        vb.validate_preset("omni", dict(lan, api="https://169.254.169.254"),
                           allow_lan=True)


# --------------------------------------------------------------------------- #
# wizard-style yaml (builtin provider + model.base_url global override)
# --------------------------------------------------------------------------- #
# `hermes model` writes this shape: no providers.<name> block, endpoint inlined
# as model.base_url. base_url routes EVERY model to that endpoint, so it must be
# removed on switch or a deepseek preset would silently talk to the mimo host.
WIZARD_YAML = YAML.replace(
    'model:\n  provider: "custom:deepseek"\n  default: "deepseek-v4-flash"\n',
    'model:\n  provider: xiaomi\n  default: mimo-v2.5\n'
    '  base_url: https://api.xiaomimimo.com/v1\n')

XIAOMI = {"api": "https://api.xiaomimimo.com/v1", "model": "mimo-v2.5",
          "transport": "openai_chat", "key_env": "XIAOMI_API_KEY"}


def test_patch_removes_wizard_base_url_on_switch_back():
    out = vb.plan_yaml_patch(WIZARD_YAML, "deepseek", DEEPSEEK)
    data = _load(out)
    assert "base_url" not in data["model"]
    assert data["model"]["provider"] == "custom:deepseek"
    assert data["model"]["default"] == "deepseek-v4-flash"


def test_patch_wizard_yaml_to_custom_xiaomi_provider():
    out = vb.plan_yaml_patch(WIZARD_YAML, "xiaomi", XIAOMI)
    data = _load(out)
    assert "base_url" not in data["model"]
    assert data["model"]["provider"] == "custom:xiaomi"
    assert data["model"]["default"] == "mimo-v2.5"
    blk = data["providers"]["xiaomi"]
    assert blk["api"] == "https://api.xiaomimimo.com/v1"
    assert blk["key_env"] == "XIAOMI_API_KEY"
    # safety chain untouched
    assert data["agent"]["disabled_toolsets"] == [
        "terminal", "file", "browser", "cronjob", "skills_hub"]
    assert set(data["mcp_servers"]) == {"vlm", "drive"}

#!/usr/bin/env python3
"""Pure logic for the brain (Hermes LLM preset) switch: yaml patch planning +
preset validation. No aiohttp, no systemctl, no network — the daemon wraps these
with the real lock/backup/restart/probe. Kept import-clean so it is unit-testable
off the board (preset validation is stdlib-only; the yaml patch needs ruamel,
imported lazily so the validators still import on a box without it).

The patch is deliberately minimal (plan §5.5): the ONLY writes allowed into
config.yaml are `model.provider` / `model.default` (two scalars) and an idempotent
upsert of one `providers.<name>` block. Every other key — above all
`disabled_toolsets` and `mcp_servers` (the safety chain) — must come out of the
patch structurally identical, or we refuse. Switching a model must never move a
single byte of the toolset kill-list.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

PROVIDER_PREFIX = "custom:"

# Format whitelists (§5.5). Names go into a yaml key and a URL-ish provider id,
# so keep them boring: no spaces, no path/query metacharacters.
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# One "/" is allowed because a local model IS a HuggingFace repo id — the LAN
# mlx_lm server loads whatever `model` names, e.g. mlx-community/Qwen3.5-9B-8bit.
# A cloud model name never contains one, so this only widens the local case.
# It stays a single interior slash: no leading/trailing one, no "..", nothing that
# could climb out of a repo id and into a path.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}"
                       r"(/[A-Za-z0-9][A-Za-z0-9._:-]{0,63})?$")
_KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_TRANSPORTS = {"openai_chat", "openai_responses", "anthropic"}


class BrainError(Exception):
    """Any refusal — bad preset, SSRF-y address, provider drift, structure diff."""


# --------------------------------------------------------------------------- #
# Preset validation (stdlib only)
# --------------------------------------------------------------------------- #
def _check_api(api):
    """Two shapes are legal and the ADDRESS decides which, not a caller flag:

      * a private IP literal  -> the LAN. http:// allowed (the Mac's mlx_lm server
        has no certificate and never will); nothing leaves the house.
      * a public https domain -> the cloud. https only, because a key rides along.

    There used to be an `allow_lan=` parameter for this, and it was the wrong
    shape: it asked the CALLER to declare something the URL already states, so the
    two could disagree. It also had no production caller — both call sites took
    the default — while `allow_lan=True` silently permitted any hostname at all.
    Classifying off the address is both simpler and strictly narrower: a hostname
    can never be "private", so it can never reach the http concession.
    """
    if not isinstance(api, str) or not api:
        raise BrainError("preset.api missing")
    if not api.startswith(("https://", "http://")):
        raise BrainError(f"preset.api must be http(s):// (got {api!r})")
    parts = urlsplit(api)
    host = parts.hostname
    if not host:
        raise BrainError(f"preset.api has no host: {api!r}")
    low = host.lower()
    if low == "localhost" or low.endswith(".localhost") or low.endswith(".local") \
            or low.endswith(".internal"):
        raise BrainError(f"preset.api host not allowed: {host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        # An IP literal. Block the SSRF-relevant ranges outright; what survives
        # is a LAN address, and only those may be bare IPs.
        if ip.is_loopback or ip.is_link_local or ip.is_multicast \
                or ip.is_unspecified or ip.is_reserved:
            raise BrainError(f"preset.api points at a blocked address: {host}")
        if not ip.is_private:
            raise BrainError(
                f"a bare IP must be a private LAN address, got public {host}")
        return
    # A hostname: cloud. Needs a real public domain, and https because the key
    # crosses the internet.
    if "." not in low:
        raise BrainError(f"cloud preset.api needs a public domain: {host}")
    if not api.startswith("https://"):
        raise BrainError(f"cloud preset.api must be https:// (got {api!r})")


def validate_preset(name, preset):
    """Raise BrainError on anything unsafe/malformed. Whether the endpoint is LAN
    or cloud is read off preset.api itself — see _check_api."""
    if not _PROVIDER_RE.match(str(name or "")):
        raise BrainError(f"bad provider/preset name: {name!r}")
    if not isinstance(preset, dict):
        raise BrainError("preset must be an object")
    model = preset.get("model")
    if not isinstance(model, str) or not _MODEL_RE.match(model):
        raise BrainError(f"bad model name: {model!r}")
    key_env = preset.get("key_env")
    if not isinstance(key_env, str) or not _KEY_ENV_RE.match(key_env):
        raise BrainError(f"bad key_env name: {key_env!r}")
    transport = preset.get("transport")
    if transport not in _TRANSPORTS:
        raise BrainError(f"unknown transport: {transport!r}")
    _check_api(preset.get("api"))


# --------------------------------------------------------------------------- #
# yaml patch (ruamel round-trip: preserves comments/formatting/other bytes)
# --------------------------------------------------------------------------- #
def _yaml():
    from ruamel.yaml import YAML  # lazy: validators must import without ruamel
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # never line-wrap our scalars
    return y


def _to_plain(obj):
    """ruamel CommentedMap/Seq -> plain dict/list/scalars, for structure diffing."""
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    return obj


def _strip_allowed(data, name):
    """Return a plain copy with the ONLY legally-mutable spots removed, so the
    remainder can be compared byte-for-structure against the original."""
    d = _to_plain(data)
    model = d.get("model")
    if isinstance(model, dict):
        model.pop("provider", None)
        model.pop("default", None)
        # base_url is a GLOBAL endpoint override the `hermes model` wizard writes
        # (builtin-provider style). It routes every model to that endpoint, so a
        # stale one after a provider switch silently misroutes — we always remove
        # it (the endpoint lives in providers.<name>.api), hence it is mutable.
        model.pop("base_url", None)
    providers = d.get("providers")
    if isinstance(providers, dict):
        providers.pop(name, None)
    return d


def plan_yaml_patch(yaml_text, name, preset):
    """Return the patched config.yaml text. name = the provider/preset key.
    Raises BrainError on preset validation failure, provider drift (existing block
    disagrees), or any structural change outside the two allowed spots."""
    validate_preset(name, preset)

    y = _yaml()
    import io
    data = y.load(yaml_text)
    if not isinstance(data, dict) or "model" not in data or "providers" not in data:
        raise BrainError("config.yaml missing model/providers — refusing to patch")

    before = _strip_allowed(data, name)

    api = preset["api"]
    model = preset["model"]
    key_env = preset["key_env"]
    transport = preset["transport"]

    providers = data["providers"]
    existing = providers.get(name)
    if existing is not None:
        # Idempotent: an existing block must AGREE on the identity fields, else it
        # is drift a human introduced — refuse rather than silently overwrite.
        for field, want in (("api", api), ("key_env", key_env),
                            ("transport", transport)):
            got = existing.get(field)
            if got != want:
                raise BrainError(
                    f"providers.{name}.{field} mismatch: yaml has {got!r}, "
                    f"preset wants {want!r} — refusing (fix the yaml or the preset)")
        # Agree -> touch nothing in the block (keeps its comments/bytes intact).
    else:
        block = {"api": api, "key_env": key_env,
                 "default_model": model, "transport": transport}
        providers[name] = block

    data["model"]["provider"] = PROVIDER_PREFIX + name
    data["model"]["default"] = model
    # Kill any wizard-written global endpoint override (see _strip_allowed).
    data["model"].pop("base_url", None)

    buf = io.StringIO()
    y.dump(data, buf)
    new_text = buf.getvalue()

    # Guard: reload the emitted text and prove nothing outside the allowed spots
    # moved. Compare parsed structures (comment/whitespace drift is fine; a changed
    # disabled_toolsets / mcp_servers is not).
    after = _strip_allowed(y.load(new_text), name)
    if before != after:
        raise BrainError(
            "yaml patch would alter keys outside model.*/providers."
            f"{name} — aborting (structure diff non-empty)")
    return new_text

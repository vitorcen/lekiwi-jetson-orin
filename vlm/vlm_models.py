#!/usr/bin/env python3
"""Pure model-scanning / mmproj-pairing / env-file logic for the VLM model switch.

Stdlib-only and side-effect free (no aiohttp, no systemd, no GPU) so the scan +
pairing + env generation can be unit-tested off the board. The daemon wraps these
with the real filesystem write, `systemctl --user restart llama-server`, and the
llama /health readiness + real-inference probe.

The llama-server unit reads model paths from an EnvironmentFile:
    VLM_MODEL=/abs/path/to/<model>.gguf
    VLM_MMPROJ=/abs/path/to/mmproj-<...>.gguf
so switching a model is: rewrite that file (atomic) + restart the unit.
"""

from __future__ import annotations

import os
import re

# A trailing quantization token so a model and its mmproj map to the same family
# key: "Qwen3-VL-2B-Instruct-Q4_K_M" -> "qwen3-vl-2b-instruct", and the mmproj
# "mmproj-Qwen3-VL-2B-Instruct-Q8_0" (prefix stripped) -> the same key.
_QUANT_RE = re.compile(
    r"[-.]?(?:Q\d+(?:_[0-9A-Za-z]+)*|IQ\d+[_0-9A-Za-z]*|F16|F32|BF16|FP16)$",
    re.IGNORECASE,
)


def _family(stem: str) -> str:
    """Strip a trailing quant token + separators, lowercased, for pairing."""
    return _QUANT_RE.sub("", stem).rstrip("-._").lower()


def is_mmproj(filename: str) -> bool:
    """mmproj / multimodal projector files are keyed by the 'mmproj' name prefix."""
    return os.path.basename(filename).lower().startswith("mmproj")


def _mmproj_family(mmproj_name: str) -> str:
    stem = mmproj_name[:-5] if mmproj_name.endswith(".gguf") else mmproj_name
    stem = re.sub(r"^mmproj[-._]*", "", stem, flags=re.IGNORECASE)
    return _family(stem)


def scan_dir(models_dir: str) -> tuple[list[str], list[str]]:
    """Return (models, mmprojs) basename lists of *.gguf, mmproj-prefixed split off.
    Unreadable dir -> ([], []) (never raises; the endpoint degrades to empty)."""
    try:
        names = sorted(os.listdir(models_dir))
    except OSError:
        return [], []
    ggufs = [n for n in names if n.endswith(".gguf")]
    models = [n for n in ggufs if not is_mmproj(n)]
    mmprojs = [n for n in ggufs if is_mmproj(n)]
    return models, mmprojs


def pair_mmproj(model_name: str, mmprojs: list[str]) -> str | None:
    """Pick the mmproj basename for `model_name`, or None when unpairable.
      1) exact family match (quant token ignored);
      2) sole mmproj in the dir (covers a single-family board);
      3) otherwise None -> the model is listed but usable=False.
    Deterministic: ties resolve to the lexicographically first candidate."""
    fam = _family(model_name[:-5] if model_name.endswith(".gguf") else model_name)
    exact = sorted(m for m in mmprojs if _mmproj_family(m) == fam)
    if exact:
        return exact[0]
    if len(mmprojs) == 1:
        return mmprojs[0]
    return None


def _mb(path: str) -> int | None:
    try:
        return int(round(os.path.getsize(path) / (1024 * 1024)))
    except OSError:
        return None


def _same_path(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.abspath(a) == os.path.abspath(b)
    except (OSError, ValueError):
        return a == b


def list_models(models_dir: str, active_model_file: str | None = None) -> list[dict]:
    """One dict per non-mmproj *.gguf under `models_dir`:
      {id, file, mmproj, disk_mb, usable, active}
    id = filename without .gguf; mmproj = paired projector path or None;
    disk_mb = measured model-file size; usable = has a paired mmproj;
    active = this file is the one named in the current llama env file."""
    models, mmprojs = scan_dir(models_dir)
    out = []
    for name in models:
        mm = pair_mmproj(name, mmprojs)
        path = os.path.join(models_dir, name)
        out.append({
            "id": name[:-5],
            "file": path,
            "mmproj": os.path.join(models_dir, mm) if mm else None,
            "disk_mb": _mb(path),
            "usable": mm is not None,
            "active": _same_path(active_model_file, path),
        })
    return out


def build_env(model_file: str, mmproj_file: str) -> str:
    """The llama-server EnvironmentFile body. Absolute paths, no shell expansion
    (systemd EnvironmentFile does not expand %h/~ — the daemon writes absolutes)."""
    return f"VLM_MODEL={model_file}\nVLM_MMPROJ={mmproj_file}\n"


def parse_env(text: str) -> dict:
    """Parse a KEY=value EnvironmentFile body -> dict. Blank/# lines ignored."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def active_model_id(active_model_file: str | None) -> str | None:
    """Model id (filename minus .gguf) named by the current env file, or None."""
    if not active_model_file:
        return None
    base = os.path.basename(active_model_file)
    return base[:-5] if base.endswith(".gguf") else base or None

"""Motion action files: schema, validation and the action directory.

One action = one JSON file named `<action_id>.json`; the file name IS the ID,
so there is no manifest and no second mapping to keep in sync. Deleting moves
the file into `.trash/` (recoverable); nothing is ever unlinked in place, so
the active directory never holds a half-deleted action.

Coordinates are semantic, never hardware raw values:
  arm  — `dq[6]`, counts relative to the CALIBRATED MIDDLE pose (ARM_MID).
  base — body twist `vx_mps / vy_mps / omega_dps`, never wheel raw speeds.
Both are re-clamped by base_host at playback time against the *current*
calibration and kinematics, so re-calibrating never invalidates a file.

This module is the ONLY schema validator. base_host, the CLI and the MCP
server all call `validate_action()`; none of them re-implement the rules.
"""
from __future__ import annotations

import json
import math
import os
import re
import time

SCHEMA_VERSION = 1

ARM_SPACE = "middle_delta_counts"
BASE_SPACE = "body_twist"
TRACK_SPACE = {"arm": ARM_SPACE, "base": BASE_SPACE}

ACTION_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ARM_JOINTS = 6

MAX_DURATION_MS = 15_000
MIN_DURATION_MS = 200
MIN_FRAMES = 3
MAX_FRAMES = 600
MAX_FILE_BYTES = 256 * 1024
MAX_LABEL_CHARS = 60
MAX_DESCRIPTION_CHARS = 200

# File-sanity envelope, deliberately WIDER than the runtime ceiling. base_host's
# clamp_body()/joint limits are the real authority on what reaches the motors;
# these bounds only reject files that are obviously not recordings.
DQ_ABS_MAX = 4095
VEL_ABS_MAX = 1.0        # m/s
OMEGA_ABS_MAX = 180.0    # deg/s

TRASH_DIRNAME = ".trash"
TRASH_KEEP_DAYS = 30


class ActionError(Exception):
    """A refusal with a stable machine-readable code (never a silent fallback)."""

    def __init__(self, code, detail=""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail

    def payload(self):
        return {"ok": False, "error": self.code, "detail": self.detail}


# ---- directories ----------------------------------------------------------

def actions_root():
    """Active action directory. Runtime data, not in Git."""
    env = os.environ.get("LEKIWI_ACTIONS_DIR")
    if env:
        return env
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "lekiwi", "motion-actions")


def trash_root(root):
    return os.path.join(root, TRASH_DIRNAME)


def _ensure_dirs(root):
    os.makedirs(root, exist_ok=True)
    os.makedirs(trash_root(root), exist_ok=True)


def action_path(root, action_id):
    return os.path.join(root, action_id + ".json")


# ---- field validation -----------------------------------------------------

def valid_action_id(value):
    return isinstance(value, str) and bool(ACTION_ID_RE.match(value))


def check_action_id(value):
    if not valid_action_id(value):
        raise ActionError(
            "invalid_action_id",
            "action_id must match [a-z][a-z0-9_-]{0,31}",
        )
    return value


def clean_text(value, limit, field):
    """Trim and length-check free text; reject control characters only.

    Not a character whitelist: labels and descriptions are human text in any
    language. What actually hurts is a control character smuggling newlines or
    terminal escapes into logs and LLM context, so that is what we reject.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ActionError("invalid_action", f"{field} must be a string")
    text = value.strip()
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise ActionError("invalid_action", f"{field} contains control characters")
    if len(text) > limit:
        raise ActionError("invalid_action", f"{field} exceeds {limit} characters")
    return text


def _finite(value, field):
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ActionError("invalid_action", f"{field} is not a number") from None
    if not math.isfinite(out):
        raise ActionError("invalid_action", f"{field} is not finite")
    return out


def _bounded(value, limit, field):
    out = _finite(value, field)
    if abs(out) > limit:
        raise ActionError("invalid_action", f"{field} exceeds |{limit}|")
    return out


# ---- track validation -----------------------------------------------------

def _check_times(frames, name):
    last = -1
    for i, frame in enumerate(frames):
        t = frame.get("t_ms")
        if not isinstance(t, int) or isinstance(t, bool):
            raise ActionError("invalid_action", f"{name} frame {i}: t_ms must be an int")
        if t <= last:
            raise ActionError("invalid_action", f"{name} frame {i}: t_ms must increase")
        last = t
    if frames[0]["t_ms"] != 0:
        raise ActionError("invalid_action", f"{name} must start at t_ms 0")
    return last


def _validate_arm_frames(frames):
    out = []
    for i, frame in enumerate(frames):
        dq = frame.get("dq")
        if not isinstance(dq, list) or len(dq) != ARM_JOINTS:
            raise ActionError("invalid_action",
                              f"arm frame {i}: dq must hold {ARM_JOINTS} values")
        out.append({
            "t_ms": frame["t_ms"],
            "dq": [round(_bounded(v, DQ_ABS_MAX, f"arm frame {i} dq")) for v in dq],
        })
    return out


def _validate_base_frames(frames):
    out = []
    for i, frame in enumerate(frames):
        out.append({
            "t_ms": frame["t_ms"],
            "vx_mps": _bounded(frame.get("vx_mps"), VEL_ABS_MAX, f"base frame {i} vx_mps"),
            "vy_mps": _bounded(frame.get("vy_mps"), VEL_ABS_MAX, f"base frame {i} vy_mps"),
            "omega_dps": _bounded(frame.get("omega_dps"), OMEGA_ABS_MAX,
                                  f"base frame {i} omega_dps"),
        })
    return out


_TRACK_VALIDATORS = {"arm": _validate_arm_frames, "base": _validate_base_frames}


def validate_tracks(tracks):
    """Normalize `tracks`; returns (tracks, duration_ms)."""
    if not isinstance(tracks, dict) or not tracks:
        raise ActionError("invalid_action", "tracks must hold at least one track")
    unknown = sorted(set(tracks) - set(_TRACK_VALIDATORS))
    if unknown:
        raise ActionError("invalid_action", "unknown track: " + ", ".join(unknown))
    out, duration = {}, 0
    for name in sorted(tracks):
        track = tracks[name]
        if not isinstance(track, dict):
            raise ActionError("invalid_action", f"{name} track must be an object")
        if track.get("space") != TRACK_SPACE[name]:
            raise ActionError("invalid_action",
                              f"{name} track space must be {TRACK_SPACE[name]!r}")
        frames = track.get("frames")
        if not isinstance(frames, list):
            raise ActionError("invalid_action", f"{name} track has no frames")
        if len(frames) < MIN_FRAMES:
            raise ActionError("invalid_action",
                              f"{name} track has fewer than {MIN_FRAMES} frames")
        if len(frames) > MAX_FRAMES:
            raise ActionError("invalid_action",
                              f"{name} track exceeds {MAX_FRAMES} frames")
        if not all(isinstance(f, dict) for f in frames):
            raise ActionError("invalid_action", f"{name} frames must be objects")
        duration = max(duration, _check_times(frames, name))
        out[name] = {"space": TRACK_SPACE[name],
                     "frames": _TRACK_VALIDATORS[name](frames)}
    if duration < MIN_DURATION_MS:
        raise ActionError("invalid_action",
                          f"duration {duration} ms is below {MIN_DURATION_MS} ms")
    if duration > MAX_DURATION_MS:
        raise ActionError("invalid_action",
                          f"duration {duration} ms exceeds {MAX_DURATION_MS} ms")
    return out, duration


def scope_of(tracks):
    """Scope is DERIVED from the track keys — never stored as a second truth."""
    has_arm, has_base = "arm" in tracks, "base" in tracks
    if has_arm and has_base:
        return "full"
    return "arm" if has_arm else "base"


def validate_action(obj):
    """Full action-object check. Returns a normalized copy or raises ActionError."""
    if not isinstance(obj, dict):
        raise ActionError("invalid_action", "action must be a JSON object")
    version = obj.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ActionError("unsupported_schema",
                          f"schema_version {version!r}, expected {SCHEMA_VERSION}")
    action_id = check_action_id(obj.get("action_id"))
    tracks, duration = validate_tracks(obj.get("tracks"))
    return {
        "schema_version": SCHEMA_VERSION,
        "action_id": action_id,
        "label": clean_text(obj.get("label"), MAX_LABEL_CHARS, "label"),
        "description": clean_text(obj.get("description"), MAX_DESCRIPTION_CHARS,
                                  "description"),
        "recorded_at": clean_text(obj.get("recorded_at"), 40, "recorded_at"),
        "duration_ms": duration,
        "tracks": tracks,
    }


def build_action(action_id, tracks, label="", description="", recorded_at=None):
    """Assemble + validate a new action from freshly recorded tracks."""
    return validate_action({
        "schema_version": SCHEMA_VERSION,
        "action_id": action_id,
        "label": label,
        "description": description,
        "recorded_at": recorded_at or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tracks": tracks,
    })


def summary(action):
    """What the GUI list and the MCP `motion_action_list` tool show."""
    return {
        "action_id": action["action_id"],
        "scope": scope_of(action["tracks"]),
        "label": action.get("label", ""),
        "description": action.get("description", ""),
        "duration_ms": action["duration_ms"],
        "frames": {name: len(t["frames"]) for name, t in action["tracks"].items()},
    }


# ---- filesystem -----------------------------------------------------------

def _read_json(path):
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ActionError("unknown_action", str(exc)) from None
    if size > MAX_FILE_BYTES:
        raise ActionError("invalid_action", f"file exceeds {MAX_FILE_BYTES} bytes")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise ActionError("invalid_action", str(exc)) from None


def load_action(root, action_id):
    check_action_id(action_id)
    path = action_path(root, action_id)
    if not os.path.exists(path):
        raise ActionError("unknown_action", action_id)
    action = validate_action(_read_json(path))
    if action["action_id"] != action_id:
        raise ActionError("invalid_action",
                          f"action_id {action['action_id']!r} != file name {action_id!r}")
    return action


def list_actions(root):
    """Scan the directory. A broken file is reported, it never poisons the list."""
    actions, invalid = [], []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return {"actions": [], "invalid": []}
    for name in names:
        if not name.endswith(".json"):
            continue
        action_id = name[:-len(".json")]
        try:
            actions.append(summary(load_action(root, action_id)))
        except ActionError as exc:
            invalid.append({"action_id": action_id, "error": exc.code,
                            "detail": exc.detail})
    return {"actions": actions, "invalid": invalid}


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_action(root, action, overwrite=False):
    """Validate, then temp-file + fsync + atomic rename. Never a partial file."""
    action = validate_action(action)
    _ensure_dirs(root)
    path = action_path(root, action["action_id"])
    exists = os.path.exists(path)
    if exists and not overwrite:
        raise ActionError("action_exists", action["action_id"])
    blob = json.dumps(action, ensure_ascii=False, indent=2) + "\n"
    if len(blob.encode("utf-8")) > MAX_FILE_BYTES:
        raise ActionError("invalid_action", f"file exceeds {MAX_FILE_BYTES} bytes")
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ActionError("write_failed", str(exc)) from None
    _fsync_dir(root)
    out = summary(action)
    out["replaced"] = exists
    return out


def trash_name(action_id, when=None):
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(when))
    return f"{action_id}.{stamp}.json"


def delete_action(root, action_id):
    """Recoverable delete: atomic rename into .trash/, never an unlink."""
    check_action_id(action_id)
    path = action_path(root, action_id)
    if not os.path.exists(path):
        raise ActionError("unknown_action", action_id)
    _ensure_dirs(root)
    name = trash_name(action_id)
    target = os.path.join(trash_root(root), name)
    try:
        os.replace(path, target)
    except OSError as exc:
        raise ActionError("write_failed", str(exc)) from None
    _fsync_dir(root)
    _fsync_dir(trash_root(root))
    return {"action_id": action_id, "trash_name": name}


def list_trash(root):
    """Most recently deleted FIRST — that order is what "undo delete" means.

    Sorting by name would order by action_id, so `zebra` deleted last year
    would outrank `apple` deleted a second ago and undo would restore the
    wrong recording.
    """
    directory = trash_root(root)
    try:
        names = [n for n in os.listdir(directory) if n.endswith(".json")]
    except OSError:
        return []

    def deleted_at(name):
        try:
            return os.path.getmtime(os.path.join(directory, name))
        except OSError:
            return 0.0

    return sorted(names, key=lambda n: (deleted_at(n), n), reverse=True)


def _trash_action_id(name):
    parts = name[:-len(".json")].split(".") if name.endswith(".json") else []
    if len(parts) != 2 or not valid_action_id(parts[0]):
        raise ActionError("invalid_action", f"bad trash name {name!r}")
    return parts[0]


def restore_action(root, name):
    """Undo a delete. Refuses to overwrite an action that took the ID back."""
    action_id = _trash_action_id(name)
    source = os.path.join(trash_root(root), name)
    if not os.path.exists(source):
        raise ActionError("unknown_action", name)
    path = action_path(root, action_id)
    if os.path.exists(path):
        raise ActionError("action_exists", action_id)
    try:
        os.replace(source, path)
    except OSError as exc:
        raise ActionError("write_failed", str(exc)) from None
    _fsync_dir(root)
    _fsync_dir(trash_root(root))
    return {"action_id": action_id, "trash_name": name}


def purge_trash(root, keep_days=TRASH_KEEP_DAYS, now=None):
    """Explicit maintenance only — nothing purges the trash automatically."""
    cutoff = (now if now is not None else time.time()) - keep_days * 86400
    removed = []
    for name in list_trash(root):
        path = os.path.join(trash_root(root), name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
                removed.append(name)
        except OSError:
            continue
    return removed

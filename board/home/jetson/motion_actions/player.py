"""Pure recording / playback maths. No serial, no sockets, no clock of its own.

`Recorder` turns a stream of canonical targets into multi-track frames sharing
one monotonic origin; `Playback` turns elapsed time back into the target that
should be in force *right now*. Both take `now` as an argument so the whole
module is testable without hardware or sleeps.
"""
from __future__ import annotations

import bisect

from . import store

# base_host's main loop polls at ~20 Hz. Sampling once per tick is the honest
# rate; this floor only stops a faster loop from filling a track with frames
# that carry no new information. `t_ms` is the single truth about timing —
# there is deliberately no `sample_hz` field anywhere.
MIN_SAMPLE_PERIOD_S = 0.030

SCOPE_TRACKS = {"arm": ("arm",), "base": ("base",), "full": ("arm", "base")}


def tracks_for_scope(scope):
    if scope not in SCOPE_TRACKS:
        raise store.ActionError("invalid_scope", f"scope must be one of {sorted(SCOPE_TRACKS)}")
    return SCOPE_TRACKS[scope]


def index_at(times, t_ms):
    """Index of the newest sample at or before `t_ms`; -1 before the first.

    Latest-only by construction: a late tick jumps straight to the current
    frame instead of replaying the frames it slept through.
    """
    return bisect.bisect_right(times, t_ms) - 1


class Recorder:
    """Passive observer of the canonical targets — takes no motion ownership."""

    def __init__(self, scope, min_period_s=MIN_SAMPLE_PERIOD_S,
                 max_frames=store.MAX_FRAMES, max_duration_ms=store.MAX_DURATION_MS):
        self.scope = scope
        self.names = tracks_for_scope(scope)
        self.frames = {name: [] for name in self.names}
        self.min_period_s = min_period_s
        self.max_frames = max_frames
        self.max_duration_ms = max_duration_ms
        self.t0 = None
        self.last_t_ms = -1
        self.capped = False

    def sample(self, now, arm_dq=None, base=None):
        """Append one multi-track sample. Returns True if it was recorded."""
        if self.capped:
            return False
        if "arm" in self.names and arm_dq is None:
            return False
        if "base" in self.names and base is None:
            return False
        if self.t0 is None:
            self.t0 = now
        t_ms = round((now - self.t0) * 1000.0)
        if self.last_t_ms >= 0 and t_ms - self.last_t_ms < self.min_period_s * 1000.0:
            return False        # also guarantees strictly increasing t_ms
        if t_ms > self.max_duration_ms:
            self.capped = True
            return False
        if "arm" in self.names:
            self.frames["arm"].append({"t_ms": t_ms, "dq": [int(v) for v in arm_dq]})
        if "base" in self.names:
            vx, vy, om = base
            self.frames["base"].append({"t_ms": t_ms, "vx_mps": float(vx),
                                        "vy_mps": float(vy), "omega_dps": float(om)})
        self.last_t_ms = t_ms
        if any(len(f) >= self.max_frames for f in self.frames.values()):
            self.capped = True
        return True

    @property
    def duration_ms(self):
        return max(self.last_t_ms, 0)

    @property
    def frame_count(self):
        return {name: len(f) for name, f in self.frames.items()}

    def tracks(self):
        """Track payload in store schema shape (unvalidated — store decides)."""
        return {name: {"space": store.TRACK_SPACE[name], "frames": frames}
                for name, frames in self.frames.items()}


class Playback:
    """Time -> canonical target. Holds no leases; base_host arbitrates those."""

    def __init__(self, action, t0):
        self.action = action
        self.t0 = t0
        self.duration_ms = action["duration_ms"]
        self.names = tuple(sorted(action["tracks"]))
        self._frames = {n: action["tracks"][n]["frames"] for n in self.names}
        self._times = {n: [f["t_ms"] for f in self._frames[n]] for n in self.names}
        self.cursor = {n: -1 for n in self.names}
        self.applied = {n: 0 for n in self.names}

    @property
    def scope(self):
        return store.scope_of(self.action["tracks"])

    def elapsed_ms(self, now):
        return round((now - self.t0) * 1000.0)

    def targets(self, now):
        """Current frame per track. Counts a frame as applied once, when reached."""
        t_ms = self.elapsed_ms(now)
        out = {}
        for name in self.names:
            i = index_at(self._times[name], t_ms)
            if i < 0:
                continue
            if i != self.cursor[name]:
                self.cursor[name] = i
                self.applied[name] = i + 1       # frames reached, not frames sent
            out[name] = self._frames[name][i]
        return out

    def done(self, now):
        return self.elapsed_ms(now) >= self.duration_ms

    def progress(self, now):
        if self.duration_ms <= 0:
            return 1.0
        return max(0.0, min(1.0, self.elapsed_ms(now) / float(self.duration_ms)))

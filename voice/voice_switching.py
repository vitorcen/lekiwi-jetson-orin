#!/usr/bin/env python3
"""Pure state/logic helpers for the engine switch executor, ephemeral overrides,
the asr-debug tail ring, and vision-caption dedup.

Everything here is stdlib-only and side-effect free (no aiohttp, no sherpa, no
hardware) so the daemon's decision logic is unit-testable off the board. The async
daemon wraps these with a real asyncio.Lock and the actual load/unload calls.
"""

from __future__ import annotations

import time
from collections import deque

# Daemon state names — shared vocabulary between the daemon core and the HTTP
# layer (voice_http), defined here so neither imports the other's module.
IDLE, LISTENING, THINKING, SPEAKING = "idle", "listening", "thinking", "speaking"
SWITCHING, DEBUG = "switching", "debug"


# --------------------------------------------------------------------------- #
# MCP01 off-hook HID report (pure byte builder — device I/O lives in the daemon)
# --------------------------------------------------------------------------- #
def offhook_report(on):
    """MCP01 HID output report: Report ID 3, bit0 = Off-Hook LED. off-hook=1 takes the
    USB speakerphone out of idle so the mic runs at normal gain (idle is ~30dB quieter,
    below Silero's floor). Exactly two bytes: [0x03, 0x01|0x00]."""
    return bytes([0x03, 0x01 if on else 0x00])


# --------------------------------------------------------------------------- #
# Switch executor: serialized, single-axis, honest-degraded (no fake rollback)
# --------------------------------------------------------------------------- #
class EngineSwitcher:
    """Guards that only one switch runs at a time. try_begin() returns False when a
    switch is already in flight -> the HTTP layer answers 409 immediately (it does
    NOT queue behind a lock, per §5.2). The async daemon still holds the real work
    inside this begin/end bracket."""

    def __init__(self):
        self.busy = False
        self.job_id = None

    def try_begin(self, job_id):
        if self.busy:
            return False
        self.busy = True
        self.job_id = job_id
        return True

    def end(self):
        self.busy = False
        self.job_id = None


def resolve_switch(prev, target, new_loaded, old_reloaded):
    """Decide the applied state after a switch attempt. Cross-process/engine switches
    are NOT atomically reversible (§5.2): on load failure we try to reload the old
    engine and report the REAL applied state, which may be degraded.

    Returns {'applied', 'status', 'persist'}:
      new_loaded            -> applied=target, status='ok',       persist=True
      not new, old reloaded -> applied=prev,   status='reverted', persist=False
      neither               -> applied=None,   status='degraded', persist=False
    """
    if new_loaded:
        return {"applied": target, "status": "ok", "persist": True}
    if old_reloaded:
        return {"applied": prev, "status": "reverted", "persist": False}
    return {"applied": None, "status": "degraded", "persist": False}


# --------------------------------------------------------------------------- #
# Ephemeral override: the ONE temporary layer over the persistent pair
# --------------------------------------------------------------------------- #
class EphemeralOverride:
    """Debug-page engine changes that must NOT be persisted. Holds {'asr'?, 'tts'?}.
    Leaving DEBUG (or a daemon restart) clears it, snapping engines back to the
    config pair. This is the whole 'one persistent + one ephemeral, no third state'
    model from §3.2."""

    def __init__(self):
        self._ov = {}

    def active(self):
        return bool(self._ov)

    def set(self, axis, value):
        if axis not in ("asr", "tts"):
            raise ValueError(f"override axis must be asr/tts, got {axis}")
        self._ov[axis] = value

    def get(self):
        return dict(self._ov)

    def clear(self):
        had = bool(self._ov)
        self._ov = {}
        return had


# --------------------------------------------------------------------------- #
# asr-debug tail ring: independent incremental channel (does NOT touch the 200
# event feed ring, so partials/finals never crowd out conversation events)
# --------------------------------------------------------------------------- #
class TailRing:
    """Monotonic-seq ring for /asr_debug/tail?since=. Each entry has a unique seq;
    since(seq) returns everything strictly after seq plus the current last_seq so a
    client can detect gaps. Partial entries are meant to be throttled/coalesced by
    the caller (P3 streaming); sensevoice only appends 'final'."""

    def __init__(self, maxlen=200):
        self.ring = deque(maxlen=maxlen)
        self.seq = 0

    def append(self, kind, text, **extra):
        self.seq += 1
        ev = {"seq": self.seq, "kind": kind, "text": text,
              "ts": round(time.time(), 3)}
        ev.update(extra)          # seg_id/outcome/dur_s/peak_dbfs for asr-seg rows
        self.ring.append(ev)
        return ev

    def since(self, seq):
        events = [e for e in self.ring if e["seq"] > seq]
        oldest = self.ring[0]["seq"] if self.ring else 0
        return {"events": events, "last_seq": self.seq, "oldest_seq": oldest}

    def clear(self):
        self.ring.clear()
        # seq keeps advancing on purpose: a client that missed the clear still sees
        # strictly increasing seqs and won't replay stale text.


# --------------------------------------------------------------------------- #
# Vision caption dedup + truncate for the board-side speak bridge
# --------------------------------------------------------------------------- #
def truncate_caption(text, limit=120):
    """>limit chars -> cut and append an ellipsis (keeps spoken lines short)."""
    if text is None:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


class CaptionDedup:
    """Speak a vlm caption only when it is genuinely new. A caption repeats its
    (seq, frame_ts) between commits (§vlm), so we key on that pair. accept() returns
    the truncated text to speak, or None to skip (duplicate / empty / error)."""

    def __init__(self, limit=120):
        self.limit = limit
        self._last_key = None

    def accept(self, caption):
        if not isinstance(caption, dict):
            return None
        if caption.get("error"):
            return None
        text = caption.get("text")
        if not text or not str(text).strip():
            return None
        key = (caption.get("seq"), caption.get("frame_ts"))
        if key == self._last_key:
            return None
        self._last_key = key
        return truncate_caption(str(text), self.limit)

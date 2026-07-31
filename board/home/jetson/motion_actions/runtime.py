"""The motion-action state machine that lives inside base_host.

Recorder, player, both domain leases and the final arbitration all run on
base_host's single main-loop thread. Nothing here talks to a socket or a
serial port on its own: base_host refreshes the per-tick inputs, calls
`tick()`, and applies whatever base velocity comes back through its normal
clamp -> solve -> drive path.

Consequences that fall out of that placement, for free:
  * "atomically acquire the base AND arm lease" is one `if` on one thread.
  * base_host dies -> playback dies with it; nothing keeps streaming frames.
  * the MCP client dies -> its socket closes -> `Job.cancelled` -> abort.
  * an in-memory draft is lost on restart, and `status()` says so out loud.

Priority: `motion_action` ranks BELOW pad/gui/ros/mcp. Any human or ROS input
in a leased domain pre-empts the WHOLE action — the other track never keeps
playing on its own, and the base is forced to zero.
"""
from __future__ import annotations

import time

from . import store
from .player import Playback, Recorder, tracks_for_scope

PREVIEW_ID = "preview"          # transient id for playing an unsaved draft


class MotionActions:
    def __init__(self, root, *, mid, order, hold_s, base_prio, prio_names,
                 log=None):
        self.root = root
        self.mid = dict(mid)
        self.order = tuple(order)
        self.hold_s = hold_s
        self.base_prio = base_prio
        self.prio_names = dict(prio_names)
        self.log = log or (lambda _msg: None)
        self.started_at = time.time()

        # ---- inputs refreshed by base_host every tick ----------------------
        self.arm = None                 # None = arm did not answer at boot
        self.motion_on = True
        self.prio_last = {}             # priority level -> last base frame time
        self.canon_base = (0.0, 0.0, 0.0)   # last APPLIED body twist (0 after watchdog)
        self.last_human_arm = -1e9
        self.last_human_arm_src = "gui"

        # ---- state ---------------------------------------------------------
        self.rec = None
        self.draft = None
        self.play = None
        self.play_job = None
        self.play_action_id = None      # None while previewing a draft
        self.play_clamped = set()
        self.needs_base_stop = False    # base_host must send a zero frame
        self.last_result = None

    # ---- small predicates -------------------------------------------------

    @property
    def recording(self):
        return self.rec is not None

    @property
    def playing(self):
        return self.play is not None

    def arm_dq(self):
        """Canonical arm target as counts relative to the calibrated middle."""
        if self.arm is None:
            return None
        return [int(self.arm.raw[sid] - self.mid[sid]) for sid in self.order]

    def arm_busy(self, now):
        """A human owns the arm for hold_s after their last arm frame."""
        return now - self.last_human_arm < self.hold_s

    def base_holder(self, now):
        """Highest-priority source outranking motion_action that holds the base."""
        for level in range(self.base_prio):
            if now - self.prio_last.get(level, -1e9) < self.hold_s:
                return level
        return None

    def arm_state(self):
        if self.arm is None:
            return "unavailable"
        if self.arm.cal_stage:
            return "calibrating"
        return "ready"

    def _needs(self):
        names = self.play.names if self.play else ()
        return "arm" in names, "base" in names

    # ---- recording --------------------------------------------------------

    def record_start(self, scope):
        names = tracks_for_scope(scope)                 # raises invalid_scope
        if self.recording:
            raise store.ActionError("already_recording", "stop the current recording first")
        if self.playing:
            raise store.ActionError("playing", "an action is playing; stop it first")
        if self.draft is not None:
            raise store.ActionError("draft_exists", "save or discard the draft first")
        if not self.motion_on:
            raise store.ActionError("safety_off", "motor output is cut")
        if "arm" in names:
            state = self.arm_state()
            if state == "unavailable":
                raise store.ActionError("arm_unavailable", "the follower arm did not answer")
            if state == "calibrating":
                raise store.ActionError("calibrating", "arm calibration is in progress")
        self.rec = Recorder(scope)
        self.log(f"[motion] recording {scope}")
        return {"ok": True, "scope": scope}

    def record_stop(self):
        if not self.recording:
            raise store.ActionError("not_recording", "no recording is running")
        rec, self.rec = self.rec, None
        try:
            tracks, duration = store.validate_tracks(rec.tracks())
        except store.ActionError:
            self.log("[motion] draft discarded: too short to be a valid action")
            raise
        self.draft = {
            "draft_id": f"draft-{int(time.time() * 1000)}",
            "scope": rec.scope,
            "tracks": tracks,
            "duration_ms": duration,
            "frames": rec.frame_count,
            "capped": rec.capped,
        }
        self.log(f"[motion] draft {self.draft['draft_id']} {duration} ms")
        return {"ok": True, **self._draft_view()}

    def record_discard(self):
        self.rec = None
        had = self.draft is not None
        self.draft = None
        return {"ok": True, "discarded": had}

    def _draft_view(self):
        if self.draft is None:
            return {"draft": None}
        return {"draft": {k: self.draft[k] for k in
                          ("draft_id", "scope", "duration_ms", "frames", "capped")}}

    def record_save(self, action_id, label="", description="", overwrite=False):
        if self.draft is None:
            raise store.ActionError("no_draft", "nothing recorded to save")
        store.check_action_id(action_id)
        self._refuse_if_in_use(action_id)
        action = store.build_action(
            action_id, self.draft["tracks"],
            label=store.clean_text(label, store.MAX_LABEL_CHARS, "label"),
            description=store.clean_text(description, store.MAX_DESCRIPTION_CHARS,
                                         "description"))
        result = store.save_action(self.root, action, overwrite=overwrite)
        self.draft = None
        self.log(f"[motion] saved {action_id} ({result['duration_ms']} ms)")
        return {"ok": True, **result}

    # ---- catalogue --------------------------------------------------------

    def _refuse_if_in_use(self, action_id):
        """One rule for save-overwrite AND delete: never touch what is playing."""
        if self.playing and self.play_action_id == action_id:
            raise store.ActionError("action_in_use", f"{action_id} is playing")

    def delete(self, action_id):
        store.check_action_id(action_id)
        self._refuse_if_in_use(action_id)
        result = store.delete_action(self.root, action_id)
        self.log(f"[motion] deleted {action_id} -> .trash/{result['trash_name']}")
        return {"ok": True, **result}

    def restore(self, name):
        result = store.restore_action(self.root, name)
        self.log(f"[motion] restored {result['action_id']}")
        return {"ok": True, **result}

    # ---- playback ---------------------------------------------------------

    def play_start(self, job, action_id=None, preview=False):
        """Acquire every lease the action needs, or start nothing at all."""
        now = time.time()
        if self.recording:
            raise store.ActionError("recording", "a recording is running")
        if self.playing:
            raise store.ActionError("busy", f"{self.play_name()} is playing")
        if not self.motion_on:
            raise store.ActionError("safety_off", "motor output is cut")
        if preview:
            if self.draft is None:
                raise store.ActionError("no_draft", "nothing recorded to preview")
            action = store.build_action(PREVIEW_ID, self.draft["tracks"])
        else:
            action = store.load_action(self.root, action_id)
        names = tuple(sorted(action["tracks"]))
        if "arm" in names:
            state = self.arm_state()
            if state == "unavailable":
                raise store.ActionError("arm_unavailable", "the follower arm did not answer")
            if state == "calibrating":
                raise store.ActionError("calibrating", "arm calibration is in progress")
            if self.arm_busy(now):
                raise store.ActionError("busy", f"arm held by {self.last_human_arm_src}")
        if "base" in names:
            holder = self.base_holder(now)
            if holder is not None:
                raise store.ActionError(
                    "busy", f"base held by {self.prio_names.get(holder, '?')}")
        self.play = Playback(action, now)
        self.play_job = job
        self.play_action_id = None if preview else action["action_id"]
        self.play_clamped = set()
        self.log(f"[motion] play {self.play_name()} ({store.scope_of(action['tracks'])})")

    def play_name(self):
        if self.play is None:
            return None
        return self.play_action_id or "<draft preview>"

    def play_stop(self, by="gui"):
        """Idempotent stop."""
        if not self.playing:
            return {"ok": True, "result": "idle"}
        result = self._finish("stopped", by=by)
        return {"ok": True, **result}

    def on_safety_off(self):
        """Master cut: abort playback, end any recording, forget nothing silently."""
        if self.playing:
            self._finish("safety_cut", by="safety")
        if self.recording:
            self.rec = None
            self.last_result = {"result": "recording_cut", "finished_at": time.time(),
                                "detail": "motor output was cut while recording"}
            self.log("[motion] recording aborted: motor output cut")

    def on_shutdown(self):
        """base_host is going away (stop, restart, serial death).

        Playback cannot outlive this process, so say so on the wire instead of
        letting the client discover an EOF and guess. The draft dies here too —
        it only ever lived in this object.
        """
        if self.playing:
            self._finish("host_stopped", by="base_host")
        self.rec = None
        self.draft = None

    def note_human_arm(self, src, now):
        """Any human/ROS arm frame: take the arm back and kill the whole action."""
        self.last_human_arm = now
        self.last_human_arm_src = src or "gui"
        if self.playing and self._needs()[0]:
            self._finish("preempted", by=self.last_human_arm_src)

    def _clamped_joints(self, dq):
        out = []
        for i, sid in enumerate(self.order):
            lo, hi = self.arm.limits[sid]
            if not lo <= self.mid[sid] + dq[i] <= hi:
                out.append(sid)
        return out

    def _finish(self, result, by=None):
        play, job = self.play, self.play_job
        now = time.time()
        payload = {
            "result": result,
            # Identifies the EVENT, not the outcome: two preemptions in a row
            # are two things that happened, and a poller must be able to tell.
            "finished_at": now,
            "action_id": self.play_action_id,
            "source": "draft" if self.play_action_id is None else "action",
            "scope": play.scope,
            "duration_ms": play.elapsed_ms(now),
            "frames_applied": dict(play.applied),
            "clamped_joints": sorted(self.play_clamped),
        }
        if by:
            payload["by"] = by
        needs_base = self._needs()[1]
        self.play = self.play_job = None
        self.play_action_id = None
        if needs_base:
            self.needs_base_stop = True         # base always ends at zero
        self.last_result = payload
        self.log(f"[motion] {result}" + (f" by {by}" if by else ""))
        if job is not None:
            job.finish({"ok": result == "succeeded", **payload})
        return payload

    def tick(self, now):
        """One main-loop step. Returns the body twist to drive, or None."""
        if self.rec is not None:
            self.rec.sample(now, self.arm_dq(), self.canon_base)
            if self.rec.capped:
                self.log("[motion] recording hit the length cap; stopping")
                try:
                    self.record_stop()
                except store.ActionError as exc:
                    self.last_result = {"result": "draft_invalid", "finished_at": now,
                                        "detail": exc.detail}
        if self.play is None:
            return None
        if not self.motion_on:
            self._finish("safety_cut", by="safety")
            return None
        job = self.play_job
        if job is not None and job.cancelled:
            self._finish("aborted", by="client")
            return None
        needs_arm, needs_base = self._needs()
        if needs_arm:
            state = self.arm_state()
            if state != "ready":
                self._finish("preempted", by=state)
                return None
            if self.arm_busy(now):
                self._finish("preempted", by=self.last_human_arm_src)
                return None
        if needs_base:
            holder = self.base_holder(now)
            if holder is not None:
                self._finish("preempted", by=self.prio_names.get(holder, "?"))
                return None
        targets = self.play.targets(now)
        base_out = None
        if "arm" in targets:
            dq = targets["arm"]["dq"]
            self.play_clamped.update(self._clamped_joints(dq))
            self.arm.follow(dq)
        if "base" in targets:
            frame = targets["base"]
            base_out = (frame["vx_mps"], frame["vy_mps"], frame["omega_dps"])
            self.prio_last[self.base_prio] = now
        if self.play.done(now):
            self._finish("succeeded")
            return None
        return base_out

    # ---- status -----------------------------------------------------------

    def status(self):
        now = time.time()
        holder = self.base_holder(now)
        if holder is not None:
            base_owner = self.prio_names.get(holder, "?")
        elif self.playing and self._needs()[1]:
            base_owner = self.prio_names.get(self.base_prio, "motion_action")
        else:
            base_owner = "none"
        if self.playing and self._needs()[0]:
            arm_owner = "motion_action"
        elif self.arm_busy(now):
            arm_owner = self.last_human_arm_src
        else:
            arm_owner = "none"
        playing = None
        if self.playing:
            playing = {
                "name": self.play_name(),
                "action_id": self.play_action_id,
                "scope": self.play.scope,
                "elapsed_ms": self.play.elapsed_ms(now),
                "duration_ms": self.play.duration_ms,
                "progress": round(self.play.progress(now), 3),
            }
        recording = None
        if self.recording:
            recording = {"scope": self.rec.scope, "duration_ms": self.rec.duration_ms,
                         "frames": self.rec.frame_count}
        return {
            "ok": True,
            # Restarts are observable: a GUI holding a draft_id from an older
            # host epoch can tell the draft evaporated instead of waiting on it.
            "host_started_at": self.started_at,
            "motion_on": self.motion_on,
            "arm": self.arm_state(),
            "actions_dir": self.root,
            "recording": recording,
            **self._draft_view(),
            "playing": playing,
            "owners": {"base": base_owner, "arm": arm_owner},
            "last_result": self.last_result,
        }

    # ---- control-socket dispatch ------------------------------------------

    def dispatch(self, job):
        """Run one control request. Returns True if the job is still pending."""
        request = job.request
        op = request.get("op")
        if job.cancelled:
            # The client hung up between sending and this tick. Running the
            # handler anyway would start motion (or mutate the catalogue) for
            # nobody, with no one left to stop it.
            job.finish({"ok": False, "error": "aborted", "detail": "client gone"})
            return False
        handler = _OPS.get(op)
        if handler is None:
            job.finish({"ok": False, "error": "unknown_op", "detail": str(op)})
            return False
        try:
            payload = handler(self, job, request)
        except store.ActionError as exc:
            job.finish(exc.payload())
            return False
        except (OSError, TypeError, ValueError) as exc:
            job.finish({"ok": False, "error": "internal_error", "detail": str(exc)})
            return False
        if payload is None:
            return True                     # `play`: finishes when playback ends
        job.finish(payload)
        return False


def _arg(request, name, default=None):
    return request.get(name, default)


_OPS = {
    "status": lambda ma, job, r: ma.status(),
    "list": lambda ma, job, r: {"ok": True, **store.list_actions(ma.root),
                                "trash": store.list_trash(ma.root)},
    "record_start": lambda ma, job, r: ma.record_start(_arg(r, "scope", "arm")),
    "record_stop": lambda ma, job, r: ma.record_stop(),
    "record_discard": lambda ma, job, r: ma.record_discard(),
    "record_save": lambda ma, job, r: ma.record_save(
        _arg(r, "action_id"), _arg(r, "label", ""), _arg(r, "description", ""),
        bool(_arg(r, "overwrite", False))),
    "play": lambda ma, job, r: ma.play_start(
        job, _arg(r, "action_id"), preview=bool(_arg(r, "preview", False))),
    "stop": lambda ma, job, r: ma.play_stop(_arg(r, "by", "gui")),
    "delete": lambda ma, job, r: ma.delete(_arg(r, "action_id")),
    "restore": lambda ma, job, r: ma.restore(_arg(r, "trash_name")),
    "purge_trash": lambda ma, job, r: {
        "ok": True, "removed": store.purge_trash(ma.root, int(_arg(r, "keep_days", 30)))},
}

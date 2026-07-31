"""Motion actions: schema, recorder/player maths and the lease state machine.

Pure logic only, per the board-test rule. No serial, no sockets, no sleeps:
`Recorder`/`Playback`/`MotionActions` all take `now` or a fake arm, so what is
under test here is "would this action move the wrong motor / start when a human
holds the bus / lose a file", not the plumbing around it.
"""
import json
import os
import time

import base_host as bh
import pytest
from motion_actions import player, store
from motion_actions.runtime import MotionActions

ORDER = bh.ARM_ORDER
MID = bh.ARM_MID
PRIO_NAMES = bh.BASE_PRIO_NAME


# ---- helpers --------------------------------------------------------------

def test_watchdog_tracks_even_one_count_wheel_commands():
    assert bh.base_command_active({7: 0, 8: 1, 9: 0})
    assert bh.base_command_active({7: -1, 8: 0, 9: 0})
    assert not bh.base_command_active({7: 0, 8: 0, 9: 0})


class FakeArm:
    """Just enough of base_host.Arm for the lease/clamp logic."""

    def __init__(self, limits=None):
        self.raw = {sid: MID[sid] for sid in ORDER}
        self.limits = limits or {sid: (900, 3200) for sid in ORDER}
        self.cal_stage = None
        self.followed = []

    def follow(self, dq):
        self.followed.append(list(dq))
        for i, sid in enumerate(ORDER):
            lo, hi = self.limits[sid]
            self.raw[sid] = max(lo, min(hi, MID[sid] + dq[i]))


class FakeJob:
    def __init__(self, request=None):
        self.request = request or {}
        self.result = None
        self.cancelled = False

    def finish(self, payload):
        self.result = payload
        return True


def arm_track(count=4, step=33):
    return {"space": store.ARM_SPACE,
            "frames": [{"t_ms": i * step, "dq": [0, i * 5, 0, 0, 0, 0]}
                       for i in range(count)]}


def base_track(count=4, step=33):
    return {"space": store.BASE_SPACE,
            "frames": [{"t_ms": i * step, "vx_mps": 0.1, "vy_mps": 0.0,
                        "omega_dps": 5.0} for i in range(count)]}


def action(action_id="yes", **tracks):
    return store.build_action(action_id, tracks or {"arm": arm_track(10)})


_DEFAULT_ARM = object()


def runtime(tmp_path, arm=_DEFAULT_ARM):
    ma = MotionActions(str(tmp_path), mid=MID, order=ORDER, hold_s=0.5,
                       base_prio=bh.MOTION_ACTION_PRIO, prio_names=PRIO_NAMES)
    ma.arm = FakeArm() if arm is _DEFAULT_ARM else arm
    return ma


# ---- schema: what must never reach nine motors ---------------------------

def test_scope_is_derived_from_tracks_not_stored():
    assert store.scope_of({"arm": {}}) == "arm"
    assert store.scope_of({"base": {}}) == "base"
    assert store.scope_of({"arm": {}, "base": {}}) == "full"


def test_action_id_rules():
    assert store.valid_action_id("yes")
    assert store.valid_action_id("wave_hello-2")
    assert not store.valid_action_id("Yes")        # no uppercase
    assert not store.valid_action_id("2fast")      # must start with a letter
    assert not store.valid_action_id("")
    assert not store.valid_action_id("a" * 33)


@pytest.mark.parametrize("bad", [
    {"schema_version": 2},                                  # unknown version
    {"action_id": "Bad Id"},
    {"tracks": {}},                                         # no track at all
    {"tracks": {"legs": arm_track()}},                      # unknown track
])
def test_validate_rejects_structural_damage(bad):
    obj = {"schema_version": 1, "action_id": "yes", "tracks": {"arm": arm_track(10)}}
    obj.update(bad)
    with pytest.raises(store.ActionError):
        store.validate_action(obj)


def test_seven_joints_is_rejected():
    track = arm_track(4)
    track["frames"][2]["dq"] = [0] * 7
    with pytest.raises(store.ActionError):
        store.validate_tracks({"arm": track})


def test_non_finite_is_rejected():
    track = base_track(8)
    track["frames"][3]["vx_mps"] = float("nan")
    with pytest.raises(store.ActionError):
        store.validate_tracks({"base": track})


def test_time_must_start_at_zero_and_increase():
    track = arm_track(8)
    track["frames"][4]["t_ms"] = track["frames"][3]["t_ms"]
    with pytest.raises(store.ActionError):
        store.validate_tracks({"arm": track})
    shifted = arm_track(8)
    for f in shifted["frames"]:
        f["t_ms"] += 100
    with pytest.raises(store.ActionError):
        store.validate_tracks({"arm": shifted})


def test_too_long_and_too_short_are_rejected():
    with pytest.raises(store.ActionError):
        store.validate_tracks({"arm": arm_track(2)})            # < MIN_FRAMES
    with pytest.raises(store.ActionError):
        store.validate_tracks({"arm": arm_track(4, step=10)})   # < MIN_DURATION_MS
    with pytest.raises(store.ActionError):
        store.validate_tracks({"arm": arm_track(store.MAX_FRAMES + 5, step=1)})


def test_space_must_match_the_track():
    track = arm_track(10)
    track["space"] = store.BASE_SPACE
    with pytest.raises(store.ActionError):
        store.validate_tracks({"arm": track})


# ---- description: length + control characters, not a character whitelist ---

def test_description_accepts_any_language():
    assert store.clean_text("点头 / Yes — ok!", 200, "description") == "点头 / Yes — ok!"


def test_description_rejects_control_characters():
    with pytest.raises(store.ActionError):
        store.clean_text("line\nbreak", 200, "description")
    with pytest.raises(store.ActionError):
        store.clean_text("esc\x1b[31m", 200, "description")


def test_description_length_is_capped():
    with pytest.raises(store.ActionError):
        store.clean_text("x" * (store.MAX_DESCRIPTION_CHARS + 1),
                         store.MAX_DESCRIPTION_CHARS, "description")


# ---- filesystem: one file, one truth, recoverable delete -----------------

def test_save_is_atomic_and_refuses_silent_overwrite(tmp_path):
    root = str(tmp_path)
    store.save_action(root, action("yes"))
    with pytest.raises(store.ActionError) as exc:
        store.save_action(root, action("yes"))
    assert exc.value.code == "action_exists"
    assert store.save_action(root, action("yes"), overwrite=True)["replaced"] is True
    assert not [p for p in tmp_path.iterdir() if ".tmp." in p.name]


def test_file_name_is_the_id(tmp_path):
    root = str(tmp_path)
    store.save_action(root, action("yes"))
    (tmp_path / "yes.json").rename(tmp_path / "no.json")
    with pytest.raises(store.ActionError):
        store.load_action(root, "no")


def test_list_reports_broken_files_without_losing_the_good_ones(tmp_path):
    root = str(tmp_path)
    store.save_action(root, action("yes"))
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    listing = store.list_actions(root)
    assert [a["action_id"] for a in listing["actions"]] == ["yes"]
    assert listing["invalid"][0]["action_id"] == "broken"


def test_delete_then_restore_round_trip(tmp_path):
    root = str(tmp_path)
    store.save_action(root, action("yes"))
    before = json.loads((tmp_path / "yes.json").read_text(encoding="utf-8"))
    name = store.delete_action(root, "yes")["trash_name"]
    assert store.list_actions(root)["actions"] == []
    store.restore_action(root, name)
    assert json.loads((tmp_path / "yes.json").read_text(encoding="utf-8")) == before


def test_restore_never_clobbers_a_reused_id(tmp_path):
    root = str(tmp_path)
    store.save_action(root, action("yes"))
    name = store.delete_action(root, "yes")["trash_name"]
    store.save_action(root, action("yes"))          # id taken back by a new take
    with pytest.raises(store.ActionError) as exc:
        store.restore_action(root, name)
    assert exc.value.code == "action_exists"


def test_purge_trash_only_drops_old_entries(tmp_path):
    root = str(tmp_path)
    store.save_action(root, action("yes"))
    store.delete_action(root, "yes")
    assert store.purge_trash(root, keep_days=30) == []
    assert len(store.purge_trash(root, keep_days=0, now=2 ** 31)) == 1


# ---- recorder / playback maths -------------------------------------------

def test_index_at_is_latest_only():
    times = [0, 33, 66, 99]
    assert player.index_at(times, -1) == -1
    assert player.index_at(times, 0) == 0
    assert player.index_at(times, 65) == 1
    assert player.index_at(times, 500) == 3      # a late tick skips, never replays


def test_recorder_shares_one_origin_across_tracks():
    rec = player.Recorder("full")
    for i in range(5):
        rec.sample(100.0 + i * 0.05, [i] * 6, (0.1, 0.0, 5.0))
    tracks = rec.tracks()
    assert [f["t_ms"] for f in tracks["arm"]["frames"]] == [0, 50, 100, 150, 200]
    assert ([f["t_ms"] for f in tracks["base"]["frames"]]
            == [f["t_ms"] for f in tracks["arm"]["frames"]])


def test_recorder_rejects_samples_faster_than_the_floor():
    rec = player.Recorder("arm")
    assert rec.sample(100.0, [0] * 6)
    assert not rec.sample(100.01, [1] * 6)       # 10 ms apart: no new information
    assert rec.sample(100.05, [2] * 6)


def test_recorder_only_keeps_the_tracks_its_scope_asked_for():
    rec = player.Recorder("base")
    rec.sample(100.0, [1] * 6, (0.1, 0.0, 0.0))
    assert set(rec.tracks()) == {"base"}


def test_recorder_needs_every_source_its_scope_requires():
    rec = player.Recorder("full")
    assert not rec.sample(100.0, None, (0.0, 0.0, 0.0))   # arm silent
    assert rec.t0 is None


def test_recorder_caps_duration():
    rec = player.Recorder("arm", max_duration_ms=200)
    rec.sample(0.0, [0] * 6)
    rec.sample(0.1, [1] * 6)
    rec.sample(0.5, [2] * 6)
    assert rec.capped
    assert rec.duration_ms == 100


def test_playback_holds_the_last_frame_until_done():
    act = action("yes", arm=arm_track(4, step=100))       # 0/100/200/300 ms
    play = player.Playback(act, 1000.0)
    assert play.targets(1000.0)["arm"]["t_ms"] == 0
    assert play.targets(1000.15)["arm"]["t_ms"] == 100
    assert not play.done(1000.15)
    assert play.targets(1000.29)["arm"]["t_ms"] == 200
    assert play.done(1000.30)
    assert play.applied["arm"] == 3


# ---- lease acquisition: all or nothing -----------------------------------

def test_full_action_starts_nothing_when_the_base_is_held(tmp_path):
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("dance", arm=arm_track(10), base=base_track(10)))
    ma.prio_last[bh.BASE_PRIO["pad"]] = __import__("time").time()
    job = FakeJob()
    with pytest.raises(store.ActionError) as exc:
        ma.play_start(job, "dance")
    assert exc.value.code == "busy"
    assert not ma.playing
    assert ma.arm.followed == []          # the arm half must not have moved


def test_arm_action_is_refused_while_a_human_holds_the_arm(tmp_path):
    import time
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("yes"))
    ma.note_human_arm("pad", time.time())
    with pytest.raises(store.ActionError) as exc:
        ma.play_start(FakeJob(), "yes")
    assert exc.value.code == "busy"


def test_arm_unavailable_has_its_own_error(tmp_path):
    ma = runtime(tmp_path, arm=None)
    store.save_action(ma.root, action("yes"))
    with pytest.raises(store.ActionError) as exc:
        ma.play_start(FakeJob(), "yes")
    assert exc.value.code == "arm_unavailable"


def test_base_only_action_ignores_the_arm_lease(tmp_path):
    import time
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("roll", base=base_track(10)))
    ma.note_human_arm("gui", time.time())
    assert ma.play_start(FakeJob(), "roll") is None
    assert ma.playing


def test_safety_off_refuses_to_start_and_kills_a_running_action(tmp_path):
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("yes"))
    ma.play_start(FakeJob(), "yes")
    ma.motion_on = False
    ma.on_safety_off()
    assert not ma.playing
    assert ma.last_result["result"] == "safety_cut"
    with pytest.raises(store.ActionError) as exc:
        ma.play_start(FakeJob(), "yes")
    assert exc.value.code == "safety_off"


# ---- preemption: the WHOLE action dies, base goes to zero ----------------

def test_human_arm_frame_preempts_the_whole_full_action(tmp_path):
    import time
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("dance", arm=arm_track(10), base=base_track(10)))
    job = FakeJob()
    ma.play_start(job, "dance")
    ma.note_human_arm("pad", time.time())
    assert not ma.playing
    assert job.result["result"] == "preempted"
    assert job.result["by"] == "pad"
    assert ma.needs_base_stop is True          # base_host must send a zero frame


def test_ros_base_frame_preempts_and_is_named(tmp_path):
    import time
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("roll", base=base_track(10)))
    job = FakeJob()
    ma.play_start(job, "roll")
    now = time.time()
    ma.prio_last[bh.BASE_PRIO["ros"]] = now
    assert ma.tick(now) is None
    assert job.result["result"] == "preempted"
    assert job.result["by"] == "ros"


def test_client_disconnect_aborts_playback(tmp_path):
    import time
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("yes"))
    job = FakeJob()
    ma.play_start(job, "yes")
    job.cancelled = True
    ma.tick(time.time())
    assert not ma.playing
    assert job.result["result"] == "aborted"
    assert job.result["by"] == "client"


def test_playback_reports_joints_clamped_by_a_narrowed_range(tmp_path):
    import time
    arm = FakeArm(limits={sid: (2000, 2100) for sid in ORDER})
    ma = runtime(tmp_path, arm=arm)
    # Recorded with a wide range, replayed after the range was narrowed to ±53.
    wide = arm_track(10)
    for i, frame in enumerate(wide["frames"]):
        frame["dq"] = [0, i * 100, 0, 0, 0, 0]
    store.save_action(ma.root, action("yes", arm=wide))
    job = FakeJob()
    now = time.time()
    ma.play_start(job, "yes")
    ma.play.t0 = now - 1.0                       # jump to the end
    ma.tick(now)
    assert not ma.playing
    assert bh.ID_LIFT in job.result["clamped_joints"]


# ---- recording gates ------------------------------------------------------

def test_record_start_refused_while_calibrating_or_playing(tmp_path):
    ma = runtime(tmp_path)
    ma.arm.cal_stage = "range"
    with pytest.raises(store.ActionError) as exc:
        ma.record_start("arm")
    assert exc.value.code == "calibrating"
    ma.arm.cal_stage = None
    store.save_action(ma.root, action("yes"))
    ma.play_start(FakeJob(), "yes")
    with pytest.raises(store.ActionError) as exc:
        ma.record_start("arm")
    assert exc.value.code == "playing"


def test_record_start_refused_when_motor_output_is_cut(tmp_path):
    ma = runtime(tmp_path)
    ma.motion_on = False
    with pytest.raises(store.ActionError) as exc:
        ma.record_start("arm")
    assert exc.value.code == "safety_off"


def test_record_start_needs_the_arm_only_for_arm_scopes(tmp_path):
    ma = runtime(tmp_path, arm=None)
    with pytest.raises(store.ActionError) as exc:
        ma.record_start("full")
    assert exc.value.code == "arm_unavailable"
    assert ma.record_start("base")["ok"] is True


def test_recorder_samples_the_canonical_base_target(tmp_path):
    ma = runtime(tmp_path)
    ma.record_start("base")
    ma.canon_base = (0.2, 0.0, 10.0)
    ma.tick(1000.0)
    ma.canon_base = (0.0, 0.0, 0.0)      # what base_host writes after a watchdog stop
    ma.tick(1000.1)
    frames = ma.rec.frames["base"]
    assert frames[0]["vx_mps"] == 0.2
    assert frames[1]["vx_mps"] == 0.0


def test_draft_shorter_than_the_floor_is_refused_not_saved(tmp_path):
    ma = runtime(tmp_path)
    ma.record_start("arm")
    ma.tick(1000.0)
    ma.tick(1000.05)
    with pytest.raises(store.ActionError):
        ma.record_stop()
    assert ma.draft is None
    assert not ma.recording


# ---- one in-use rule for both overwrite and delete -----------------------

def test_playing_action_can_be_neither_deleted_nor_overwritten(tmp_path):
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("yes"))
    ma.play_start(FakeJob(), "yes")
    ma.draft = {"draft_id": "d", "scope": "arm", "tracks": {"arm": arm_track(10)},
                "duration_ms": 297, "frames": {"arm": 10}, "capped": False}
    for call in (lambda: ma.delete("yes"),
                 lambda: ma.record_save("yes", overwrite=True)):
        with pytest.raises(store.ActionError) as exc:
            call()
        assert exc.value.code == "action_in_use"


def test_a_different_action_may_be_deleted_while_one_plays(tmp_path):
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("yes"))
    store.save_action(ma.root, action("no"))
    ma.play_start(FakeJob(), "yes")
    assert ma.delete("no")["ok"] is True


# ---- crash / restart is observable ---------------------------------------

def test_status_exposes_the_host_epoch_and_a_missing_draft(tmp_path):
    ma = runtime(tmp_path)
    status = ma.status()
    assert status["draft"] is None
    assert status["host_started_at"] > 0
    fresh = runtime(tmp_path)                   # a "restarted" base_host
    assert fresh.status()["host_started_at"] >= status["host_started_at"]
    assert fresh.status()["draft"] is None      # drafts never survive a restart


def test_unknown_control_op_is_refused_not_guessed(tmp_path):
    ma = runtime(tmp_path)
    job = FakeJob({"op": "delete_everything"})
    assert ma.dispatch(job) is False
    assert job.result["error"] == "unknown_op"


def test_play_job_stays_pending_until_playback_ends(tmp_path):
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("yes"))
    job = FakeJob({"op": "play", "action_id": "yes"})
    assert ma.dispatch(job) is True             # pending: no reply yet
    assert job.result is None


# ---- priority: motion_action is last ------------------------------------

def test_motion_action_ranks_below_every_live_sender():
    assert (bh.BASE_PRIO["pad"] < bh.BASE_PRIO["gui"] < bh.BASE_PRIO["ros"]
            < bh.BASE_PRIO["mcp"] < bh.BASE_PRIO["motion_action"])


def test_every_higher_source_blocks_a_playing_action():
    for name in ("pad", "gui", "ros", "mcp"):
        held = {bh.BASE_PRIO[name]: 10.0}
        assert bh.base_blocked(held, bh.MOTION_ACTION_PRIO, 10.2)
    assert not bh.base_blocked({}, bh.MOTION_ACTION_PRIO, 10.2)


def test_a_playing_action_never_blocks_a_live_sender():
    held = {bh.MOTION_ACTION_PRIO: 10.0}
    assert not bh.base_blocked(held, bh.BASE_PRIO["mcp"], 10.1)


def test_the_wire_cannot_claim_the_players_own_priority():
    """A remote `src=motion_action` frame would refresh the player's own mux
    slot: base_holder() only looks at strictly higher levels, so it would
    starve playback WITHOUT pre-empting it. Off the wire that tag is unknown
    and falls back to gui, which does pre-empt."""
    assert "motion_action" not in bh.WIRE_PRIO
    spoofed = bh.WIRE_PRIO.get("motion_action", bh.BASE_PRIO["gui"])
    assert spoofed == bh.BASE_PRIO["gui"] < bh.MOTION_ACTION_PRIO
    for name, level in bh.WIRE_PRIO.items():
        assert bh.BASE_PRIO[name] == level          # one table, no second truth


# ---- what counts as a human hand on the arm ------------------------------

@pytest.mark.parametrize("frame", [
    {"arm.dq": [0, 0, 0, 0, 0, 0]},                 # leader streaming its pose
    {"arm.relax": True},
    {"arm.mid": True},
    {"arm.calibrate": "start"},
    {"ee.vz": -0.4},                                # a pushed gamepad stick
    {"grip.v": 1.0},
])
def test_arm_input_is_detected(frame):
    assert bh.arm_input_present(frame)


@pytest.mark.parametrize("frame", [
    {},
    {"x.vel": 0.2, "y.vel": 0.0, "theta.vel": 0.0},         # base only
    {"ee.vf": 0.0, "ee.vpan": 0.0, "ee.vz": 0.0, "grip.v": 0.0},  # pad at rest
    {"arm.dq": [1, 2, 3]},                                  # not six joints
    {"ee.vz": "junk"},                                      # malformed, not a crash
])
def test_a_resting_pad_does_not_steal_the_arm(frame):
    assert not bh.arm_input_present(frame)


def test_a_relax_frame_kills_the_action_before_the_glide_guard_runs(tmp_path):
    """Regression: base_host used to clear `relaxing` whenever an action HAD
    been playing this tick, which swallowed the very relax command that
    pre-empted it. The guard is now `if ma.playing`, so this must be False."""
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("yes"))
    ma.play_start(FakeJob(), "yes")
    frame = {"arm.relax": True, "src": "gui"}
    assert bh.arm_input_present(frame)
    ma.note_human_arm(frame["src"], time.time())
    assert not ma.playing        # -> base_host keeps relaxing = True


# ---- stop must work WHILE an action plays --------------------------------

def test_stop_finishes_the_blocked_play_job_and_zeroes_the_base(tmp_path):
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("dance", arm=arm_track(10), base=base_track(10)))
    play = FakeJob({"op": "play", "action_id": "dance"})
    assert ma.dispatch(play) is True             # still blocked on the board
    stop = FakeJob({"op": "stop", "by": "gui"})
    assert ma.dispatch(stop) is False
    assert stop.result["ok"] is True
    assert play.result["result"] == "stopped"    # the long request got its answer
    assert play.result["by"] == "gui"
    assert ma.needs_base_stop is True
    assert not ma.playing


def test_stop_is_idempotent_when_nothing_plays(tmp_path):
    ma = runtime(tmp_path)
    job = FakeJob({"op": "stop"})
    assert ma.dispatch(job) is False
    assert job.result == {"ok": True, "result": "idle"}


def test_a_job_whose_client_already_left_never_starts_motion(tmp_path):
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("yes"))
    job = FakeJob({"op": "play", "action_id": "yes"})
    job.cancelled = True                          # peer hung up before this tick
    assert ma.dispatch(job) is False
    assert job.result["error"] == "aborted"
    assert not ma.playing
    assert ma.arm.followed == []


def test_shutdown_answers_a_blocked_client_and_drops_the_draft(tmp_path):
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("roll", base=base_track(10)))
    job = FakeJob({"op": "play", "action_id": "roll"})
    ma.dispatch(job)
    ma.record_discard()
    ma.draft = {"draft_id": "d"}
    ma.on_shutdown()
    assert job.result["result"] == "host_stopped"
    assert job.result["by"] == "base_host"
    assert ma.needs_base_stop is True
    assert ma.draft is None and not ma.recording


# ---- natural end: last frame applied, base still ends at zero ------------

def test_natural_end_applies_the_last_frame_and_still_zeroes_the_base(tmp_path):
    ma = runtime(tmp_path)
    arm = arm_track(10)                                   # 0..297 ms
    store.save_action(ma.root, action("dance", arm=arm, base=base_track(10)))
    job = FakeJob()
    now = time.time()
    ma.play_start(job, "dance")
    ma.play.t0 = now - 0.4                                # past the end
    assert ma.tick(now) is None                           # no trailing velocity
    assert ma.arm.followed[-1] == arm["frames"][-1]["dq"]  # last frame NOT skipped
    assert job.result["result"] == "succeeded"
    assert ma.needs_base_stop is True


def test_every_result_carries_a_distinct_event_stamp(tmp_path):
    ma = runtime(tmp_path)
    store.save_action(ma.root, action("yes"))
    stamps = []
    for _ in range(2):
        ma.play_start(FakeJob(), "yes")
        ma.play_stop(by="gui")
        stamps.append(ma.last_result["finished_at"])
    # A poller keys on this: two identical outcomes are still two events.
    assert stamps[0] != stamps[1]


# ---- undo restores the LAST delete, not the alphabetically last ----------

def test_trash_is_newest_first_so_undo_targets_the_last_delete(tmp_path):
    root = str(tmp_path)
    store.save_action(root, action("zebra"))
    store.save_action(root, action("apple"))
    old = store.delete_action(root, "zebra")["trash_name"]
    new = store.delete_action(root, "apple")["trash_name"]
    os.utime(os.path.join(store.trash_root(root), old), (1000, 1000))
    os.utime(os.path.join(store.trash_root(root), new), (2000, 2000))
    assert store.list_trash(root)[0] == new
    store.restore_action(root, store.list_trash(root)[0])
    assert [a["action_id"] for a in store.list_actions(root)["actions"]] == ["apple"]

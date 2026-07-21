"""Pure switch-executor / override / tail-ring / caption-dedup logic."""
import voice_switching as vs


# ---- MCP01 off-hook HID report byte format ---------------------------------
def test_offhook_report_bytes():
    assert vs.offhook_report(True) == b"\x03\x01"     # Report ID 3, bit0 set
    assert vs.offhook_report(False) == b"\x03\x00"
    assert len(vs.offhook_report(True)) == 2


# ---- switch resolution (ok / reverted / degraded) --------------------------
def test_resolve_switch_success_persists():
    r = vs.resolve_switch("edge", "melo", new_loaded=True, old_reloaded=False)
    assert r == {"applied": "melo", "status": "ok", "persist": True}


def test_resolve_switch_failed_reverts_no_persist():
    r = vs.resolve_switch("edge", "melo", new_loaded=False, old_reloaded=True)
    assert r == {"applied": "edge", "status": "reverted", "persist": False}


def test_resolve_switch_both_fail_is_degraded():
    r = vs.resolve_switch("edge", "melo", new_loaded=False, old_reloaded=False)
    assert r["applied"] is None and r["status"] == "degraded" and r["persist"] is False


# ---- concurrency guard -> 409 ---------------------------------------------
def test_engine_switcher_rejects_concurrent():
    sw = vs.EngineSwitcher()
    assert sw.try_begin("job1") is True
    assert sw.try_begin("job2") is False        # already busy -> caller returns 409
    sw.end()
    assert sw.try_begin("job3") is True         # freed after end()


# ---- ephemeral override set/clear revert ----------------------------------
def test_ephemeral_override_lifecycle():
    ov = vs.EphemeralOverride()
    assert not ov.active()
    ov.set("tts", {"engine": "melo"})
    assert ov.active() and ov.get() == {"tts": {"engine": "melo"}}
    assert ov.clear() is True                    # had something
    assert not ov.active() and ov.get() == {}
    assert ov.clear() is False                   # idempotent


def test_ephemeral_override_rejects_bad_axis():
    ov = vs.EphemeralOverride()
    try:
        ov.set("brain", {})
        assert False
    except ValueError:
        pass


# ---- tail ring incremental semantics --------------------------------------
def test_tail_ring_since_returns_strictly_after():
    tr = vs.TailRing(maxlen=10)
    a = tr.append("final", "你好")
    b = tr.append("final", "再见")
    got = tr.since(0)
    assert [e["text"] for e in got["events"]] == ["你好", "再见"]
    assert got["last_seq"] == b["seq"]
    # since the last seq -> nothing new
    assert tr.since(b["seq"])["events"] == []
    # since a -> only b
    assert [e["seq"] for e in tr.since(a["seq"])["events"]] == [b["seq"]]


def test_tail_ring_seq_monotonic_through_evictions():
    tr = vs.TailRing(maxlen=2)
    for i in range(5):
        tr.append("final", str(i))
    got = tr.since(0)
    assert got["last_seq"] == 5
    assert got["oldest_seq"] == 4               # only last 2 retained (seq 4,5)
    assert [e["text"] for e in got["events"]] == ["3", "4"]


def test_tail_ring_clear_keeps_seq_advancing():
    tr = vs.TailRing()
    tr.append("final", "x")
    tr.clear()
    ev = tr.append("final", "y")
    assert ev["seq"] == 2                        # not reset to 1 -> no stale replay


# ---- caption dedup + truncate ---------------------------------------------
def test_caption_dedup_skips_repeat_seq_frame():
    d = vs.CaptionDedup()
    cap = {"text": "一只猫", "seq": 5, "frame_ts": 1.0}
    assert d.accept(cap) == "一只猫"
    assert d.accept(dict(cap)) is None           # same seq+frame_ts -> skip
    assert d.accept({"text": "一只狗", "seq": 6, "frame_ts": 2.0}) == "一只狗"


def test_caption_dedup_skips_error_and_empty():
    d = vs.CaptionDedup()
    assert d.accept({"text": "x", "seq": 1, "frame_ts": 1.0, "error": "boom"}) is None
    assert d.accept({"text": "", "seq": 2, "frame_ts": 2.0}) is None
    assert d.accept({"text": None, "seq": 3, "frame_ts": 3.0}) is None
    assert d.accept("not a dict") is None


def test_truncate_caption():
    assert vs.truncate_caption("abc", 10) == "abc"
    long = "字" * 200
    out = vs.truncate_caption(long, 120)
    assert len(out) == 121 and out.endswith("…")   # 120 chars + ellipsis


def test_caption_dedup_truncates_long_text():
    d = vs.CaptionDedup(limit=5)
    assert d.accept({"text": "一二三四五六七", "seq": 1, "frame_ts": 1.0}) == "一二三四五…"

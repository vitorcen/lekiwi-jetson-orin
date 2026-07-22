"""base_host pure logic: wheel kinematics, wire encoding, priority mux.

These guard "which way does the robot go" — a sign flip here is only ever
found by driving into a wall.
"""
import math

import base_host as bh


# ---- raw_speed: STS wire format, bit 15 = reverse ------------------------

def test_raw_speed_wire_format():
    assert bh.raw_speed(100) == 100
    assert bh.raw_speed(-100) == (100 | 0x8000)
    assert bh.raw_speed(0) == 0
    assert bh.raw_speed(99.7) == 99          # trunc, not round


# ---- cksum: STS3215 datasheet ping packet FF FF 01 02 01 FB --------------

def test_cksum_datasheet_ping():
    assert bh.cksum([0x01, 0x02, 0x01]) == 0xFB


# ---- solve: 3-wheel omni kinematics (mount angles 150 / -90 / 30) --------

def test_solve_pure_forward_symmetry():
    out = bh.solve(0.1, 0.0, 0.0)
    # wheel 8 mounted at -90 deg: cos(-90)=0 -> no contribution from vx
    assert abs(out[8]) < 1e-9
    # wheels 7 and 9 mirror each other
    assert math.isclose(out[7], -out[9], rel_tol=1e-9)
    assert out[9] > 0
    # regression pin: cos(30)*0.1/0.05 * 4096/2pi
    assert math.isclose(out[9], 1129.12, abs_tol=0.01)


def test_solve_pure_strafe():
    out = bh.solve(0.0, 0.1, 0.0)
    # sin(150)=sin(30)=0.5, sin(-90)=-1
    assert math.isclose(out[7], out[9], rel_tol=1e-9)
    assert math.isclose(out[8], -2.0 * out[7], rel_tol=1e-9)


def test_solve_pure_rotation_all_equal():
    out = bh.solve(0.0, 0.0, 45.0)
    vals = list(out.values())
    assert math.isclose(vals[0], vals[1], rel_tol=1e-9)
    assert math.isclose(vals[1], vals[2], rel_tol=1e-9)


def test_solve_overspeed_scales_uniformly():
    raw = bh.solve(0.5, 0.3, 90.0)      # would exceed MAX_RAW unscaled?
    big = bh.solve(5.0, 3.0, 900.0)     # definitely exceeds
    peak = max(abs(v) for v in big.values())
    assert math.isclose(peak, bh.MAX_RAW, rel_tol=1e-9)
    # direction ratios preserved: big is a uniform scale of 10x-input solve
    ref = {sid: v * 10 for sid, v in raw.items()}
    ref_peak = max(abs(v) for v in ref.values())
    for sid in big:
        assert math.isclose(big[sid], ref[sid] * bh.MAX_RAW / ref_peak,
                            rel_tol=1e-6)


# ---- base_blocked: priority mux hold window (safety: pad mutes LLM) ------

def test_base_blocked_hold_window():
    held = {0: 10.0}                      # pad sent a base frame at t=10
    assert bh.base_blocked(held, 1, 10.0 + bh.BASE_HOLD_S - 0.01)
    assert not bh.base_blocked(held, 1, 10.0 + bh.BASE_HOLD_S + 0.01)


def test_base_blocked_top_priority_never_blocked():
    assert not bh.base_blocked({0: 10.0, 1: 10.0, 2: 10.0}, 0, 10.0)


def test_base_blocked_any_higher_level_blocks():
    # mcp (p=2) muted by gui (level 1) even with pad silent
    assert bh.base_blocked({1: 10.0}, 2, 10.2)
    # levels never seen default to "long ago" and must not block
    assert not bh.base_blocked({}, 3, 10.0)


# ---- safety master switch latch ------------------------------------------

def test_motion_latch_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(bh, "MOTION_FILE", str(tmp_path / "motion"))
    assert bh.read_motion() is True          # missing file -> default ON
    bh.write_motion(False)
    assert bh.read_motion() is False         # latched across "restart"
    bh.write_motion(True)
    assert bh.read_motion() is True


def test_motion_garbage_file_defaults_on(tmp_path, monkeypatch):
    p = tmp_path / "motion"
    p.write_text("wat\n")
    monkeypatch.setattr(bh, "MOTION_FILE", str(p))
    assert bh.read_motion() is True


# ---- sync_write_pkt: broadcast frame layout + checksum -------------------

def test_sync_write_pkt_layout():
    # 3 wheels, 2 bytes each: FF FF FE len 83 addr n [id d0 d1]*3 cksum
    pkt = bh.sync_write_pkt(bh.ADDR_SPEED, {7: [1, 2], 8: [3, 4], 9: [5, 6]})
    assert pkt[:2] == b"\xff\xff"
    assert pkt[2] == 0xFE                    # broadcast id
    assert pkt[3] == (2 + 1) * 3 + 4         # length field
    assert pkt[4] == 0x83                    # sync-write instruction
    assert pkt[5] == bh.ADDR_SPEED
    assert pkt[6] == 2                       # bytes per servo
    assert pkt[7:13] == bytes([7, 1, 2, 8, 3, 4])
    assert pkt[13:16] == bytes([9, 5, 6])
    assert pkt[-1] == bh.cksum(list(pkt[2:-1]))
    assert len(pkt) == 17


def test_sync_write_pkt_single_servo():
    pkt = bh.sync_write_pkt(bh.ADDR_TORQUE, {7: [1]})
    assert pkt[3] == (1 + 1) * 1 + 4
    assert pkt[6] == 1


# ---- frame_ttl_ok: stamped frames expire, legacy frames never ------------

def test_frame_ttl_unstamped_always_ok():
    assert bh.frame_ttl_ok({}, 100.0)
    assert bh.frame_ttl_ok({"ts": 1.0}, 100.0)          # ts without ttl
    assert bh.frame_ttl_ok({"ttl_s": 0.1}, 100.0)       # ttl without ts


def test_frame_ttl_expiry():
    f = {"ts": 10.0, "ttl_s": 0.2}
    assert bh.frame_ttl_ok(f, 10.15)
    assert not bh.frame_ttl_ok(f, 10.25)


def test_frame_ttl_garbage_passes():
    assert bh.frame_ttl_ok({"ts": "wat", "ttl_s": 0.2}, 10.0)


# ---- clamp_body: vector-norm final ceiling -------------------------------

def test_clamp_body_vector_norm():
    vx, vy, om = bh.clamp_body(1.0, 1.0, 0.0)
    assert math.isclose(math.hypot(vx, vy), bh.BODY_VMAX, rel_tol=1e-9)
    assert math.isclose(vx, vy, rel_tol=1e-9)            # direction preserved


def test_clamp_body_passthrough_inside_limits():
    assert bh.clamp_body(0.1, -0.1, 30.0) == (0.1, -0.1, 30.0)


def test_clamp_body_omega():
    assert bh.clamp_body(0.0, 0.0, 500.0)[2] == bh.BODY_WMAX
    assert bh.clamp_body(0.0, 0.0, -500.0)[2] == -bh.BODY_WMAX


# ---- current_owner -------------------------------------------------------

def test_current_owner_highest_fresh_wins():
    now = 100.0
    assert bh.current_owner({0: now - 0.1, 3: now - 0.1}, now) == 0
    assert bh.current_owner({3: now - 0.1}, now) == 3
    assert bh.current_owner({0: now - bh.BASE_HOLD_S - 1}, now) is None
    assert bh.current_owner({}, now) is None


# ---- ros priority sits between gui and mcp -------------------------------

def test_ros_priority_ordering():
    assert bh.BASE_PRIO["pad"] < bh.BASE_PRIO["gui"] < bh.BASE_PRIO["ros"] < bh.BASE_PRIO["mcp"]

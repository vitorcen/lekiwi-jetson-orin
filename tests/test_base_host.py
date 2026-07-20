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

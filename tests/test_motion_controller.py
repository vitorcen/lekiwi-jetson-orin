"""YawTurn pure state machine: convergence, wrap-around, and every guard
that keeps a closed loop from insisting on a wrong direction forever."""
import math

import motion_controller as mc


def wrap(a):
    return mc.shortest_angle(a)


def run_sim(goal, yaw0=0.0, gain=1.0, dt=0.04, max_s=30.0):
    """Ideal plant: yaw integrates the commanded omega. Feeds WRAPPED yaw
    (like real quaternion yaw) at 25 Hz."""
    yaw, t = yaw0, 0.0
    while t < max_s and not goal.status:
        om = goal.update(wrap(yaw), t)
        yaw += om * gain * dt
        t += dt
    return goal


# ---- shortest_angle ------------------------------------------------------

def test_shortest_angle_wrap():
    assert wrap(190.0) == -170.0
    assert wrap(-190.0) == 170.0
    assert wrap(180.0) == 180.0
    assert wrap(360.0) == 0.0
    assert wrap(-45.0) == -45.0


def test_yaw_deg_quat():
    s = math.sin(math.radians(45))
    assert abs(mc.yaw_deg(math.cos(math.radians(45)), 0, 0, s) - 90.0) < 1e-6


# ---- convergence ---------------------------------------------------------

def test_turn_converges_90():
    g = run_sim(mc.YawTurn(90.0, 0.0))
    assert g.status == "succeeded"
    assert abs(g.turned - 90.0) < mc.TOL_DEG
    assert abs(g.final_err) < mc.TOL_DEG


def test_turn_converges_negative_30():
    g = run_sim(mc.YawTurn(-30.0, 0.0))
    assert g.status == "succeeded"
    assert abs(g.turned + 30.0) < mc.TOL_DEG


def test_turn_wraps_through_180():
    # start at yaw 170°, turn +40 crosses the ±180 seam
    g = run_sim(mc.YawTurn(40.0, 0.0), yaw0=170.0)
    assert g.status == "succeeded"
    assert abs(g.turned - 40.0) < mc.TOL_DEG


def test_angle_clamped_to_180():
    assert mc.YawTurn(500.0, 0.0).angle == 180.0
    assert mc.YawTurn(-500.0, 0.0).angle == -180.0


# ---- command shaping -----------------------------------------------------

def test_cmd_clamped_and_floored():
    g = mc.YawTurn(90.0, 0.0)
    g.update(0.0, 0.0)                      # arm start_yaw
    cmd = g.update(0.0, 0.04)               # err=90 -> KP*90 clamped
    assert cmd == mc.OMEGA_MAX
    g2 = mc.YawTurn(3.0, 0.0)               # small err below floor
    g2.update(0.0, 0.0)
    assert g2.update(0.0, 0.04) == mc.OMEGA_MIN


# ---- guards --------------------------------------------------------------

def test_blocked_wheel_no_progress():
    g = run_sim(mc.YawTurn(90.0, 0.0), gain=0.0)     # commands, yaw frozen
    assert g.status == "no_progress"


def test_sensor_jump_kills_goal():
    g = mc.YawTurn(90.0, 0.0)
    g.update(0.0, 0.0)
    g.update(1.0, 0.04)
    assert g.update(80.0, 0.08) == 0.0               # 79° in one frame
    assert g.status == "sensor_jump"


def test_timeout():
    g = mc.YawTurn(90.0, 0.0)
    g.update(0.0, 0.0)
    g.update(0.5, 100.0)                             # way past deadline
    assert g.status == "timeout"


def test_result_shape():
    g = run_sim(mc.YawTurn(45.0, 0.0))
    r = g.result()
    assert r["status"] == "succeeded"
    assert r["target_deg"] == 45.0
    assert abs(r["turned_deg"] - 45.0) < mc.TOL_DEG

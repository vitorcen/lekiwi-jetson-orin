#!/usr/bin/env python3
"""Yaw closed-loop motion controller: IMU feedback -> base velocity frames.

The layer between LLM goals and the base wire (docs/imu-closed-loop-drive.html
P1). Accepts goal-style commands over a local ZMQ REP socket, closes the loop
on /imu/data yaw, and streams velocity frames to base_host as src="ros"
(pad/gui outrank it; raw mcp bursts do not).

  REP  tcp://127.0.0.1:5560   {"op":"turn_by","angle_deg":±180} -> {"accepted"...}
                              {"op":"stop"}    -> cancels the active goal
                              {"op":"status"}  -> goal state + imu freshness
  PUSH tcp://127.0.0.1:5555   {"src":"ros","seq",ts,"ttl_s", x/y/theta.vel}
  SUB  tcp://127.0.0.1:5556   base_host acks — detects "my frames are not
                              being applied" (muted by pad/gui, or host down)

Control law is deliberately dumb (P + clamps, NO integral term — integral
only accumulates disaster against static friction or a muting arbiter).
Termination is where the safety lives, every path explicit:
  succeeded | canceled | preempted_by_human | not_applied | sensor_stale
  | sensor_jump | no_progress | oscillating | timeout
There is NO fallback to open-loop time-based turning: if the IMU dies the
goal dies with a truthful status, and the LLM may explicitly choose
drive_move instead.  Controller death => base_host 0.5 s watchdog brakes.
"""
import json
import math
import threading
import time

import zmq
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Imu

BASE_ADDR = "tcp://127.0.0.1:5555"
ACK_ADDR = "tcp://127.0.0.1:5557"
REP_BIND = "tcp://127.0.0.1:5560"

ANGLE_MAX = 180.0        # deg, per goal
KP = 0.8                 # (deg/s) per deg of error
OMEGA_MAX = 25.0         # deg/s cruise ceiling (< base_host BODY_WMAX)
OMEGA_MIN = 6.0          # deg/s static-friction floor while outside tolerance
TOL_DEG = 2.0            # success window
RATE_TOL = 3.0           # deg/s, "actually stopped" for settle
SETTLE_N = 4             # consecutive in-window frames to succeed
NO_PROGRESS_S = 1.5      # no new best-error for this long -> no_progress
PROGRESS_EPS = 0.5       # deg improvement that counts as progress
JUMP_DEG = 45.0          # per-frame yaw jump = IMU epoch change / glitch
OSC_MAX = 8              # error sign flips before we give up as oscillating
IMU_STALE_S = 0.25       # feedback age that kills an active goal
ACK_GRACE_S = 0.6        # sent frames but no src=ros ack for this long
TTL_S = 0.25             # stamp on outgoing frames
FRAME_ID_MIN = 5         # frames sent before not_applied can trigger


def shortest_angle(a):
    """Wrap to (-180, 180]."""
    a = math.fmod(a, 360.0)
    if a > 180.0:
        a -= 360.0
    elif a <= -180.0:
        a += 360.0
    return a


def yaw_deg(qw, qx, qy, qz):
    """REP-103 yaw (CCW+) from quaternion, degrees."""
    return math.degrees(math.atan2(2 * (qw * qz + qx * qy),
                                   1 - 2 * (qy * qy + qz * qz)))


class YawTurn:
    """Pure yaw-goal state machine: feed it (yaw_deg, now) samples, it hands
    back an omega command until it terminates. No ROS, no ZMQ — testable."""

    def __init__(self, angle_deg, now):
        self.angle = max(-ANGLE_MAX, min(ANGLE_MAX, float(angle_deg)))
        self.made = now
        self.deadline = now + max(3.0, abs(self.angle) / OMEGA_MAX * 2 + 2.0)
        self.start_yaw = None
        self.prev_yaw = None
        self.prev_t = None
        self.turned = 0.0            # unwrapped accumulated rotation
        self.best_err = None
        self.best_t = now
        self.settle = 0
        self.flips = 0
        self.prev_sign = 0
        self.status = None           # terminal status string once done
        self.final_err = None

    def _finish(self, status, err):
        self.status = status
        self.final_err = err
        return 0.0

    def update(self, yaw, now):
        """One feedback sample -> omega command (deg/s). 0.0 once terminal."""
        if self.status:
            return 0.0
        if now > self.deadline:
            return self._finish("timeout", self.best_err)
        if self.start_yaw is None:
            self.start_yaw = self.prev_yaw = yaw
            self.prev_t = now
            return 0.0
        dyaw = shortest_angle(yaw - self.prev_yaw)
        if abs(dyaw) > JUMP_DEG:
            return self._finish("sensor_jump", None)
        dt = max(1e-3, now - self.prev_t)
        rate = dyaw / dt
        self.turned += dyaw
        self.prev_yaw, self.prev_t = yaw, now
        err = self.angle - self.turned

        # settle: inside tolerance AND actually stopped, several frames in a row
        if abs(err) < TOL_DEG and abs(rate) < RATE_TOL:
            self.settle += 1
            if self.settle >= SETTLE_N:
                return self._finish("succeeded", err)
            return 0.0
        self.settle = 0

        # progress + oscillation accounting
        if self.best_err is None or abs(err) < abs(self.best_err) - PROGRESS_EPS:
            self.best_err, self.best_t = err, now
        elif now - self.best_t > NO_PROGRESS_S:
            return self._finish("no_progress", err)
        sign = 1 if err > 0 else -1
        if self.prev_sign and sign != self.prev_sign:
            self.flips += 1
            if self.flips > OSC_MAX:
                return self._finish("oscillating", err)
        self.prev_sign = sign

        cmd = KP * err
        if abs(cmd) > OMEGA_MAX:
            cmd = math.copysign(OMEGA_MAX, cmd)
        if abs(err) >= TOL_DEG and abs(cmd) < OMEGA_MIN:
            cmd = math.copysign(OMEGA_MIN, cmd)
        return cmd

    def result(self):
        return {
            "status": self.status or "active",
            "target_deg": round(self.angle, 1),
            "turned_deg": round(self.turned, 1),
            "final_error_deg": None if self.final_err is None
            else round(self.final_err, 1),
            "elapsed_s": None,       # filled by the node
        }


class MotionController(Node):
    def __init__(self):
        super().__init__('motion_controller')
        ctx = zmq.Context.instance()
        self.push = ctx.socket(zmq.PUSH)
        self.push.setsockopt(zmq.LINGER, 0)
        self.push.setsockopt(zmq.SNDHWM, 10)
        self.push.setsockopt(zmq.SNDTIMEO, 0)
        self.push.connect(BASE_ADDR)
        self.sub = ctx.socket(zmq.SUB)
        self.sub.setsockopt_string(zmq.SUBSCRIBE, "")
        self.sub.setsockopt(zmq.RCVHWM, 32)
        self.sub.connect(ACK_ADDR)

        self.lock = threading.Lock()
        self.goal = None
        self.goal_t0 = 0.0
        self.last_result = None
        self.seq = 0
        self.frames = 0
        self.last_ros_ack = 0.0
        self.last_human_owner = ""
        self.motion_on = None        # from base_host 2 Hz state heartbeat
        self.yaw = None
        self.last_imu = 0.0

        self.create_subscription(Imu, '/imu/data', self.on_imu, 1)
        self.create_timer(0.1, self.watchdog)
        threading.Thread(target=self.serve, daemon=True).start()
        self.get_logger().info(f'motion_controller up: {REP_BIND} -> {BASE_ADDR}')

    # -- wire ------------------------------------------------------------
    def send_vel(self, om):
        self.seq += 1
        msg = {"src": "ros", "seq": self.seq, "ts": time.time(),
               "ttl_s": TTL_S, "x.vel": 0.0, "y.vel": 0.0, "theta.vel": om}
        try:
            self.push.send_string(json.dumps(msg), zmq.NOBLOCK)
            self.frames += 1
        except zmq.Again:
            pass

    def brake(self):
        for _ in range(3):
            self.send_vel(0.0)

    def drain_acks(self):
        now = time.monotonic()
        while True:
            try:
                m = json.loads(self.sub.recv_string(zmq.NOBLOCK))
            except zmq.Again:
                return
            except ValueError:
                continue
            if m.get("type") == "state":
                self.motion_on = m.get("motion_on")
                continue
            if m.get("type") != "ack":
                continue
            if m.get("owner") == "ros":
                self.last_ros_ack = now
            elif m.get("owner") in ("pad", "gui"):
                self.last_human_owner = m["owner"]

    # -- control ---------------------------------------------------------
    def on_imu(self, msg):
        q = msg.orientation
        self.yaw = yaw_deg(q.w, q.x, q.y, q.z)
        self.last_imu = time.monotonic()
        with self.lock:
            g = self.goal
            if g is None or g.status:
                return
            om = g.update(self.yaw, time.monotonic())
            if g.status:
                self.finish_locked(g)
            else:
                self.send_vel(om)

    def watchdog(self):
        self.drain_acks()
        now = time.monotonic()
        with self.lock:
            g = self.goal
            if g is None or g.status:
                return
            if now - self.last_imu > IMU_STALE_S:
                g._finish("sensor_stale", None)
            elif self.last_human_owner:
                g._finish("preempted_by_human", None)
            elif (self.frames >= FRAME_ID_MIN
                    and now - self.last_ros_ack > ACK_GRACE_S):
                g._finish("not_applied", None)
            if g.status:
                self.finish_locked(g)

    def finish_locked(self, g):
        self.brake()
        r = g.result()
        r["elapsed_s"] = round(time.monotonic() - self.goal_t0, 2)
        if g.status == "preempted_by_human":
            r["preempted_by"] = self.last_human_owner
        if g.status == "not_applied":
            r["motion_on"] = self.motion_on
            if self.motion_on is False:
                r["reason"] = ("安全开关(motion)关闭,轮子未上电 — 需在 GUI/手柄"
                               "打开运动开关;开环 drive_move 同样不会动,别重试")
        self.last_result = r
        self.goal = None
        self.get_logger().info(f'goal done: {r}')

    # -- goal API (REP, worker thread) -----------------------------------
    def serve(self):
        rep = zmq.Context.instance().socket(zmq.REP)
        rep.bind(REP_BIND)
        while True:
            try:
                req = json.loads(rep.recv_string())
            except ValueError:
                rep.send_string(json.dumps({"error": "bad json"}))
                continue
            rep.send_string(json.dumps(self.handle_req(req), ensure_ascii=False))

    # NOT named `handle` — rclpy.Node.handle is a property the base class
    # enters as a context manager in __init__; shadowing it breaks Node init.
    def handle_req(self, req):
        op = req.get("op")
        now = time.monotonic()
        if op == "status":
            with self.lock:
                g = self.goal
                return {
                    "active": g is not None,
                    "goal": g.result() if g else None,
                    "last_result": self.last_result,
                    "imu_ok": now - self.last_imu < IMU_STALE_S,
                    "imu_age_s": round(now - self.last_imu, 3)
                    if self.last_imu else None,
                    "motion_on": self.motion_on,
                }
        if op == "stop":
            with self.lock:
                g = self.goal
                if g:
                    g._finish("canceled", None)
                    self.finish_locked(g)
                    return {"stopped": True, "result": self.last_result}
            return {"stopped": False, "reason": "no active goal"}
        if op == "turn_by":
            try:
                angle = float(req["angle_deg"])
            except (KeyError, TypeError, ValueError):
                return {"error": "bad angle_deg"}
            if now - self.last_imu > IMU_STALE_S:
                return {"error": "sensor_stale",
                        "detail": "IMU 数据不新鲜,拒绝闭环转向;可显式用 drive_move 开环"}
            with self.lock:
                if self.goal is not None:
                    return {"error": "busy", "goal": self.goal.result()}
                self.goal = YawTurn(angle, now)
                self.goal_t0 = now
                self.frames = 0
                self.last_ros_ack = now      # grace restarts per goal
                self.last_human_owner = ""
            return {"accepted": True, "angle_deg": self.goal.angle
                    if self.goal else angle}
        return {"error": f"unknown op: {op}"}


def main():
    rclpy.init()
    node = None
    try:
        node = MotionController()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.brake()
            node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

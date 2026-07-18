#!/usr/bin/env python3
"""Minimal LeKiwi base host — drives the 3 wheels only, no calibration needed.

A drop-in for `lekiwi_host` when all you want is to teleop the base. The real
lekiwi_host forces interactive arm calibration on connect() (EOFErrors over SSH),
but the wheels never need calibration, so this speaks the same ZMQ wire contract
without any of that:

  bind  PULL  tcp://*:5555
  recv  one JSON string per command: {"x.vel": m/s, "y.vel": m/s, "theta.vel": deg/s}
        plus optional arm keys (all default 0, all -1..1):
        "ee.vf"   toward EXTENDED(+) / back toward REST(-)
        "ee.vz"   toward UPRIGHT(+)  / back toward REST(-)
        "ee.vpan" pan left(+) / right(-)
        "ee.vroll" wrist roll
        "grip.v"  gripper open(+) / close(-)
        "arm.relax" 1 = fold the arm back to REST then cut servo torque
                  (limp, safe to move by hand); any later arm input wakes it
        "arm.dq"  leader-arm follow: [6 raw deltas from the leader's zero
                  pose, ids 1..6]; target = ARM_REST_FULL + dq, clamped to
                  calibrated limits. Base keys may be omitted in such
                  messages (base is only driven when "x.vel" is present).

Arm control (servos 1-6, optional — skipped if the arm doesn't answer):
three-pose teleop, no calibration and no cartesian IK. lift/elbow/wrist
travel in joint space between REST (raw positions captured at boot — park
the arm at its rest pose before starting), EXT_RAW (reaching forward) and
UP_RAW (whole arm upright); holding an input pulls the pose toward that
target with an exponential approach (slows as it arrives). pan/roll/gripper
are plain raw increments with range clamps. Every write is rate-limited.
This matches how the operator thinks: stick forward = reach out, stick back
= come home, Y = stand up, A = settle down.

Kinematics is identical to lerobot's LeKiwi._body_to_wheel_raw (v0.5.2):
wheels 7/8/9 at 150/-90/30 deg, base_radius 0.125 m, wheel_radius 0.05 m,
raw = deg/s * 4096/360, over-speed scaled on all wheels together. Wheels run in
velocity mode (reg 33 = 1); speed to reg 46/47 with bit 15 as the sign.

Watchdog: if no command arrives for WATCHDOG_S, the base is stopped — same
dead-man behaviour as the real host, so a dropped client never runs away.
"""
import json
import math
import sys
import time

import serial
import zmq

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
BIND = sys.argv[2] if len(sys.argv) > 2 else "tcp://*:5555"
BAUD = 1000000
WATCHDOG_S = 0.5

ADDR_MODE, ADDR_TORQUE, ADDR_ACCEL, ADDR_SPEED, ADDR_LOCK = 33, 40, 41, 46, 55
ADDR_GOAL, ADDR_POS = 42, 56
WHEELS = {7: 240.0 - 90, 8: 0.0 - 90, 9: 120.0 - 90}   # id -> mounting angle (deg)
BASE_R, WHEEL_R, MAX_RAW = 0.125, 0.05, 2500

# ---- arm (SO-101 follower, ids 1-6) --------------------------------------
ID_PAN, ID_LIFT, ID_ELBOW, ID_WRIST, ID_ROLL, ID_GRIP = 1, 2, 3, 4, 5, 6
# Fully-extended pose (raw). Rest pose is captured live at boot; measured rest
# on this robot: pan 2067, lift 940, elbow 3095, wrist 1838, roll 1996,
# grip 1671. First guess for "extended" = servo centers (the canonical
# assembly middle pose) — push further toward horizontal after a live test.
# Post-calibration frame (lerobot-calibrate 2026-07-18 wrote homing offsets
# to servo EEPROM; 2048 = middle pose, upper arm vertical, forearm level).
# REST is a FIXED measured pose (arm parked/folded), not captured at boot —
# boot may find the arm anywhere mid-motion after a service restart.
# Calibrated ranges: lift [847,3244] elbow [897,3114] wrist [904,3193]
# pan [1097,3097] grip [1452,2959] roll full-turn.
REST_RAW = {ID_LIFT: 1001, ID_ELBOW: 3003, ID_WRIST: 1902}  # parked/folded
EXT_RAW = {ID_LIFT: 2500, ID_ELBOW: 1300, ID_WRIST: 2700}   # reach forward
UP_RAW = {ID_LIFT: 2048, ID_ELBOW: 1100, ID_WRIST: 2900}    # whole arm upright
POSE_K = 1.2                   # 1/s exponential approach rate at full input
PAN_LIM = (1120, 3075)         # absolute, from calibration (margin 25)
PAN_RATE = 500                 # raw counts/s at full input
ROLL_RANGE = 600               # roll is full-turn: keep boot-relative range
ROLL_RATE = 500
GRIP_LIM = (1470, 2940)        # absolute, from calibration (closed .. open)
GRIP_RATE = 600
# Leader-follow: full rest pose (all 6 joints, measured parked) and absolute
# clamps per joint (from calibration, margin ~25; roll is full-turn so a
# generous fixed window around rest).
ARM_REST_FULL = {ID_PAN: 2110, ID_LIFT: 1001, ID_ELBOW: 3003,
                 ID_WRIST: 1902, ID_ROLL: 2116, ID_GRIP: 1453}
ARM_LIMS = {ID_PAN: PAN_LIM, ID_LIFT: (870, 3220), ID_ELBOW: (920, 3090),
            ID_WRIST: (930, 3170), ID_ROLL: (1400, 2830), ID_GRIP: GRIP_LIM}
# Calibrated MIDDLE pose (every joint at its range center: upper arm vertical,
# forearm level, wrist straight). Leader-follow aligns here, not at REST —
# right angles are far easier to reproduce by eye on the leader, which is
# what alignment accuracy depends on (wrist especially).
ARM_MID = {ID_PAN: 2048, ID_LIFT: 2048, ID_ELBOW: 2048,
           ID_WRIST: 2048, ID_ROLL: 2048, ID_GRIP: 2205}
EE_STEP_MAX = 60               # max raw counts one joint may move per command


def cksum(b):
    return (~sum(b)) & 0xFF


def txrx(ser, pkt, wait=0.004):
    ser.reset_input_buffer()
    ser.write(pkt)
    ser.flush()
    time.sleep(wait)
    return ser.read(64)


def write(ser, sid, addr, data):
    body = [sid, len(data) + 3, 0x03, addr] + list(data)
    return txrx(ser, bytes([0xFF, 0xFF] + body + [cksum(body)]))


def read(ser, sid, addr, ln):
    body = [sid, 0x04, 0x02, addr, ln]
    r = txrx(ser, bytes([0xFF, 0xFF] + body + [cksum(body)]))
    i = r.find(bytes([0xFF, 0xFF, sid]))
    return None if i < 0 or len(r) < i + 6 + ln else r[i + 5:i + 5 + ln]


def le(v):
    return [v & 0xFF, (v >> 8) & 0xFF]


def raw_speed(v):
    """Signed counts/s -> STS wire format (bit 15 = reverse)."""
    return (abs(int(v)) | 0x8000) if v < 0 else int(v)


def solve(vx, vy, omega_deg):
    """Body velocity -> per-wheel raw speed (counts/s). Matches lerobot exactly."""
    w = math.radians(omega_deg)
    out = {}
    for sid, ang in WHEELS.items():
        a = math.radians(ang)
        linear = math.cos(a) * vx + math.sin(a) * vy + BASE_R * w
        out[sid] = linear / WHEEL_R * 4096 / (2 * math.pi)
    peak = max(abs(v) for v in out.values())
    if peak > MAX_RAW:
        out = {sid: v * MAX_RAW / peak for sid, v in out.items()}
    return out


def read_pos(ser, sid):
    r = read(ser, sid, ADDR_POS, 2)
    return None if r is None else (r[0] | (r[1] << 8)) & 0x0FFF


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Arm:
    """Three-pose joint-space teleop: REST (boot) <-> EXT_RAW / UP_RAW."""

    def __init__(self, ser):
        self.ser = ser
        self.raw = {}
        for sid in (ID_PAN, ID_LIFT, ID_ELBOW, ID_WRIST, ID_ROLL, ID_GRIP):
            p = read_pos(ser, sid)
            if p is None:
                raise OSError(f"arm servo {sid} silent")
            self.raw[sid] = p
        # Hold current pose: goal = present BEFORE torque on, so nothing jumps.
        for sid, p in self.raw.items():
            write(ser, sid, ADDR_GOAL, le(p))
            write(ser, sid, ADDR_ACCEL, [20])
            write(ser, sid, ADDR_TORQUE, [1])
        self.rest = {sid: float(v) for sid, v in REST_RAW.items()}
        self.pose = {sid: float(self.raw[sid]) for sid in REST_RAW}
        self.pan = float(self.raw[ID_PAN])
        self.pan_lim = PAN_LIM
        self.roll = float(self.raw[ID_ROLL])
        self.roll_lim = (self.roll - ROLL_RANGE, self.roll + ROLL_RANGE)
        self.grip = float(self.raw[ID_GRIP])
        self.grip_lim = GRIP_LIM
        self.relaxed = False
        print(f"[base_host] arm up at "
              f"{ {s: int(v) for s, v in self.pose.items()} } "
              f"pan={self.pan:.0f} roll={self.roll:.0f} grip={self.grip:.0f}",
              flush=True)

    def wake(self):
        """Re-energize after relax: resync to wherever gravity left the arm."""
        for sid in self.raw:
            p = read_pos(self.ser, sid)
            if p is not None:
                self.raw[sid] = p
            write(self.ser, sid, ADDR_GOAL, le(self.raw[sid]))
            write(self.ser, sid, ADDR_TORQUE, [1])
        self.pose = {sid: float(self.raw[sid]) for sid in REST_RAW}
        self.pan = float(self.raw[ID_PAN])
        self.roll = float(self.raw[ID_ROLL])
        self.grip = float(self.raw[ID_GRIP])
        self.relaxed = False
        print("[base_host] arm awake", flush=True)

    def relax_tick(self, dt):
        """One folding step toward REST; cut torque once it has arrived."""
        if self.relaxed:
            return
        f = min(1.0, POSE_K * dt)
        for sid in self.pose:
            self.pose[sid] += (self.rest[sid] - self.pose[sid]) * f
        for sid, g in self.pose.items():
            g = int(clamp(round(g), 0, 4095))
            g = clamp(g, self.raw[sid] - EE_STEP_MAX, self.raw[sid] + EE_STEP_MAX)
            if g != self.raw[sid]:
                write(self.ser, sid, ADDR_GOAL, le(g))
                self.raw[sid] = g
        if max(abs(self.pose[s] - self.rest[s]) for s in self.pose) < 25:
            for sid in self.raw:
                write(self.ser, sid, ADDR_TORQUE, [0])
            self.relaxed = True
            print("[base_host] arm at rest, torque released (limp)", flush=True)

    def mid_tick(self, dt):
        """Glide all six joints to the calibrated middle pose (torque stays).
        Returns True once arrived."""
        if self.relaxed:
            self.wake()
        f = min(1.0, POSE_K * dt)
        for sid in self.pose:
            self.pose[sid] += (ARM_MID[sid] - self.pose[sid]) * f
        self.pan += (ARM_MID[ID_PAN] - self.pan) * f
        self.roll += (ARM_MID[ID_ROLL] - self.roll) * f
        self.grip += (ARM_MID[ID_GRIP] - self.grip) * f
        goals = {ID_PAN: self.pan, ID_ROLL: self.roll, ID_GRIP: self.grip,
                 **self.pose}
        err = 0.0
        for sid, g in goals.items():
            err = max(err, abs(g - ARM_MID[sid]))
            g = int(clamp(round(g), 0, 4095))
            g = clamp(g, self.raw[sid] - EE_STEP_MAX, self.raw[sid] + EE_STEP_MAX)
            if g != self.raw[sid]:
                write(self.ser, sid, ADDR_GOAL, le(g))
                self.raw[sid] = g
        if err < 15:
            print("[base_host] arm at middle pose", flush=True)
            return True
        return False

    def follow(self, dq):
        """Leader-arm joint passthrough: middle pose + leader delta, clamped."""
        if self.relaxed:
            self.wake()
        now = time.time()
        if now - getattr(self, "_flog", 0) > 1.0:   # 1 Hz visibility
            self._flog = now
            print(f"[base_host] follow dq={[int(v) for v in dq]} "
                  f"raw={ {s: self.raw[s] for s in sorted(self.raw)} }", flush=True)
        order = (ID_PAN, ID_LIFT, ID_ELBOW, ID_WRIST, ID_ROLL, ID_GRIP)
        for i, sid in enumerate(order):
            t = clamp(ARM_MID[sid] + dq[i], *ARM_LIMS[sid])
            g = int(clamp(round(t), self.raw[sid] - EE_STEP_MAX,
                          self.raw[sid] + EE_STEP_MAX))
            if g != self.raw[sid]:
                write(self.ser, sid, ADDR_GOAL, le(g))
                self.raw[sid] = g
        # Keep stick-teleop state continuous with where follow left the arm.
        self.pose = {sid: float(self.raw[sid]) for sid in REST_RAW}
        self.pan = float(self.raw[ID_PAN])
        self.roll = float(self.raw[ID_ROLL])
        self.grip = float(self.raw[ID_GRIP])

    def step(self, vf, vpan, vz, vroll, gv, dt):
        # Pose axes: +vf pulls toward EXTENDED, +vz toward UPRIGHT, either
        # negative pulls back toward REST. Exponential approach: fast when
        # far, easing in as it arrives; both pulls may blend.
        if self.relaxed:
            self.wake()
        pulls = []
        if vf:
            pulls.append((EXT_RAW if vf > 0 else self.rest, abs(vf)))
        if vz:
            pulls.append((UP_RAW if vz > 0 else self.rest, abs(vz)))
        for target, w in pulls:
            f = min(1.0, POSE_K * w * dt)
            for sid in self.pose:
                self.pose[sid] += (target[sid] - self.pose[sid]) * f
        self.pan = clamp(self.pan + vpan * PAN_RATE * dt, *self.pan_lim)
        self.roll = clamp(self.roll + vroll * ROLL_RATE * dt, *self.roll_lim)
        self.grip = clamp(self.grip + gv * GRIP_RATE * dt, *self.grip_lim)
        goals = {ID_PAN: self.pan, ID_ROLL: self.roll, ID_GRIP: self.grip,
                 **self.pose}
        for sid, g in goals.items():
            g = int(clamp(round(g), 0, 4095))
            cur = self.raw[sid]
            g = clamp(g, cur - EE_STEP_MAX, cur + EE_STEP_MAX)  # rate limit
            if g != cur:
                write(self.ser, sid, ADDR_GOAL, le(g))
                self.raw[sid] = g


def ensure_wheel_mode(ser):
    for sid in WHEELS:
        m = read(ser, sid, ADDR_MODE, 1)
        if m is None:
            sys.exit(f"ABORT: wheel {sid} did not answer on {PORT}")
        if m[0] != 1:
            write(ser, sid, ADDR_LOCK, [0])
            write(ser, sid, ADDR_MODE, [1])
            write(ser, sid, ADDR_LOCK, [1])
        write(ser, sid, ADDR_ACCEL, [30])
        write(ser, sid, ADDR_TORQUE, [1])


def drive(ser, speeds):
    for sid, v in speeds.items():
        write(ser, sid, ADDR_SPEED, le(raw_speed(v)))


def stop(ser):
    for sid in WHEELS:
        write(ser, sid, ADDR_SPEED, le(0))


def main():
    ser = serial.Serial(PORT, BAUD, timeout=0.02)
    ensure_wheel_mode(ser)
    print(f"[base_host] wheels {sorted(WHEELS)} in velocity mode on {PORT}", flush=True)
    try:
        arm = Arm(ser)
    except OSError as e:
        arm = None
        print(f"[base_host] arm disabled: {e}", flush=True)

    ctx = zmq.Context()
    sock = ctx.socket(zmq.PULL)
    sock.setsockopt(zmq.CONFLATE, 1)          # only ever act on the newest command
    sock.bind(BIND)
    poller = zmq.Poller()
    poller.register(sock, zmq.POLLIN)
    print(f"[base_host] listening on {BIND}", flush=True)

    last_cmd = time.time()
    moving = False
    relaxing = False
    mid_seek = False
    try:
        while True:
            socks = dict(poller.poll(timeout=50))
            if sock in socks:
                try:
                    data = json.loads(sock.recv_string())
                    base = ("x.vel" in data)
                    if base:
                        vx, vy, om = (float(data["x.vel"]), float(data["y.vel"]),
                                      float(data["theta.vel"]))
                    ee = [float(data.get(k, 0.0)) for k in
                          ("ee.vf", "ee.vpan", "ee.vz", "ee.vroll", "grip.v")]
                    dq = data.get("arm.dq")
                    if dq is not None:
                        dq = [float(v) for v in dq[:6]]
                    if data.get("arm.relax"):
                        relaxing = True
                        print("[base_host] arm relax requested", flush=True)
                    if data.get("arm.mid"):
                        mid_seek = True
                        relaxing = False
                        print("[base_host] arm middle-pose requested", flush=True)
                except Exception as e:
                    print(f"[base_host] bad command: {e}", flush=True)
                    continue
                # Serial errors must NOT be swallowed as "bad command": if the
                # USB serial drops it re-enumerates under a new tty and this fd
                # is dead forever. Let OSError escape -> exit(1) -> systemd
                # restarts us against the stable /dev/serial/by-id path.
                now = time.time()
                if base:
                    speeds = solve(vx, vy, om)
                    drive(ser, speeds)
                    moving = any(abs(v) > 1 for v in speeds.values())
                if arm and dq is not None and len(dq) == 6:
                    relaxing = False
                    mid_seek = False
                    arm.follow(dq)
                if arm and any(ee):
                    relaxing = False          # any arm input cancels/wakes
                    mid_seek = False
                    arm.step(*ee, dt=min(0.1, now - last_cmd))
                last_cmd = now
            elif moving and time.time() - last_cmd > WATCHDOG_S:
                stop(ser)
                moving = False
                print("[base_host] watchdog: stopped (no command)", flush=True)
            if arm and relaxing:
                arm.relax_tick(0.05)
                if arm.relaxed:
                    relaxing = False
            if arm and mid_seek and arm.mid_tick(0.05):
                mid_seek = False
    except KeyboardInterrupt:
        pass
    except OSError as e:
        print(f"[base_host] serial died: {e} — exiting for restart", flush=True)
        sys.exit(1)
    finally:
        try:
            stop(ser)
            for sid in WHEELS:
                write(ser, sid, ADDR_TORQUE, [0])
            ser.close()
            print("[base_host] stopped, torque released", flush=True)
        except OSError:
            pass  # serial already gone


if __name__ == "__main__":
    main()

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
        "safety.motion" 1/0 master switch (latched in MOTION_FILE): 0 freezes
                  actuation — wheels stopped, arm goals dropped, holding
                  torque kept — while recv/mux/telemetry run normally, so
                  the command chain can be debugged with motors dead.

Arm control (servos 1-6, optional — skipped if the arm doesn't answer):
direct per-joint velocity teleop, no calibration and no cartesian IK. Left
stick fwd/back integrates LIFT(id2) + ELBOW(id3) as a coordinated reach
(ELBOW opposite sign, so forward = reach out, back = retract); Y/A integrates
WRIST(id4) pitch up/down; left stick left/right = pan(id1); X/B = roll(id5);
triggers = gripper(id6). Every joint clamps to its calibrated range and every
write is rate-limited. REST_RAW (parked pose) is still used by the relax/mid
glide helpers, not by live teleop.

Kinematics is identical to lerobot's LeKiwi._body_to_wheel_raw (v0.5.2):
wheels 7/8/9 at 150/-90/30 deg, base_radius 0.125 m, wheel_radius 0.05 m,
raw = deg/s * 4096/360, over-speed scaled on all wheels together. Wheels run in
velocity mode (reg 33 = 1); speed to reg 46/47 with bit 15 as the sign.

Watchdog: if no command arrives for WATCHDOG_S, the base is stopped — same
dead-man behaviour as the real host, so a dropped client never runs away.
"""
import json
import math
import os
import sys
import time

import serial
import zmq

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyACM0"
BIND = sys.argv[2] if len(sys.argv) > 2 else "tcp://*:5555"
BAUD = 1000000
WATCHDOG_S = 0.5

# Base-velocity priority mux (ported from rdk-x5 cmd_vel_mux): the physical
# gamepad ALWAYS outranks software senders; the human GUI outranks the LLM.
# A source owns the base for BASE_HOLD_S after its last base frame — pad's
# release-zero therefore also pins the bus, so holding the pad e-stop
# suppresses MCP motion. Untagged messages rank as "gui" (legacy binaries).
BASE_PRIO = {"pad": 0, "gui": 1, "mcp": 2}
BASE_PRIO_NAME = {0: "pad", 1: "gui", 2: "mcp"}
BASE_HOLD_S = 0.5

# Servo-battery telemetry: the STS3215s run off the WitMotion 11.1 V (3S) pack,
# so a wheel servo's Present-Voltage register is a stand-in for pack voltage
# (the Orin cannot read it any other way — this daemon owns the only serial
# port). Published to a world-readable file the GUI polls over ssh. The Orin's
# OWN supply (E351S -> EV60-T1219 DC-DC -> 19 V) is regulated away and is not
# measurable on-board; the GUI shows board power (VDD_IN) for the host instead.
BATT_FILE = "/tmp/lekiwi_batt"
ARM_FILE = "/tmp/lekiwi_arm"   # "limp" | "holding" | "none" for the GUI statusbar
# Safety master switch, latched across service restarts (/tmp -> a full reboot
# re-arms). "0" = FREEZE actuation: wheels zeroed, no new arm goals (holding
# torque stays so a raised arm doesn't drop). Everything upstream — recv,
# priority mux, owner logs, battery telemetry — keeps running, so the whole
# command chain can be exercised with the motors safely dead.
MOTION_FILE = "/tmp/lekiwi_motion"
BATT_PERIOD_S = 5.0

ADDR_MODE, ADDR_TORQUE, ADDR_ACCEL, ADDR_SPEED, ADDR_LOCK = 33, 40, 41, 46, 55
ADDR_GOAL, ADDR_POS, ADDR_VOLT = 42, 56, 62
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
REST_NEAR = 150  # raw counts (~13°): within this of REST at startup -> stay limp
ARM_IDLE_RELAX_S = 60  # no arm command for this long -> fold to REST + torque off
POSE_K = 1.2                   # 1/s exp approach (relax/mid glide only)
# Direct per-joint velocity teleop (raw counts/s at full stick). Left stick
# fwd/back drives LIFT(id2)+ELBOW(id3) as a coordinated reach: from REST toward
# reach-forward LIFT rises but ELBOW opens the other way, hence ELBOW's negative
# sign in step(). Y/A drives WRIST(id4) pitch up/down. Each clamps to ARM_LIMS.
LIFT_RATE, ELBOW_RATE, WRIST_RATE = 700, 800, 700
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


def base_blocked(prio_last, p, now):
    """True while any strictly-higher-priority source still holds the base bus
    (sent a base frame within BASE_HOLD_S). Safety-critical: this is what lets
    the physical pad's stream — including its release-zero — mute the LLM."""
    return any(now - prio_last.get(q, -1.0) < BASE_HOLD_S for q in range(p))


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


def read_volt(ser, sid):
    """Present Voltage (reg 62) in volts, or None if the servo is silent."""
    r = read(ser, sid, ADDR_VOLT, 1)
    return None if r is None else r[0] / 10.0


def write_batt(v):
    """Publish pack voltage atomically to BATT_FILE (best effort)."""
    try:
        tmp = BATT_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(f"{v:.1f}\n")
        os.replace(tmp, BATT_FILE)
    except OSError:
        pass


def write_motion(on):
    """Latch the safety switch state atomically to MOTION_FILE (best effort)."""
    try:
        tmp = MOTION_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(("1" if on else "0") + "\n")
        os.replace(tmp, MOTION_FILE)
    except OSError:
        pass


def read_motion():
    """Boot state of the safety switch: default ON, missing/garbage file = ON."""
    try:
        with open(MOTION_FILE) as f:
            return f.read().strip() != "0"
    except OSError:
        return True


def write_arm_state(s):
    """Publish arm torque state atomically to ARM_FILE (best effort)."""
    try:
        tmp = ARM_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(s + "\n")
        os.replace(tmp, ARM_FILE)
    except OSError:
        pass


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Arm:
    """Direct per-joint velocity teleop: left stick = lift+elbow, Y/A = wrist."""

    def __init__(self, ser):
        self.ser = ser
        self.raw = {}
        for sid in (ID_PAN, ID_LIFT, ID_ELBOW, ID_WRIST, ID_ROLL, ID_GRIP):
            p = read_pos(ser, sid)
            if p is None:
                raise OSError(f"arm servo {sid} silent")
            self.raw[sid] = p
        # Power-on posture policy: if the gravity-loaded joints are already at
        # the parked pose, stay limp (torque off — bench-friendly). Only when
        # the arm is clearly raised do we lock it to keep it from dropping,
        # e.g. when base_host restarts mid-operation.
        # Only the load-bearing joints decide: a limp wrist merely droops,
        # while a raised LIFT/ELBOW would crash down without torque.
        parked = all(
            abs(self.raw[sid] - REST_RAW[sid]) < REST_NEAR
            for sid in (ID_LIFT, ID_ELBOW)
        )
        for sid, p in self.raw.items():
            write(ser, sid, ADDR_ACCEL, [20])
            if not parked:
                # Hold current pose: goal = present BEFORE torque on, no jump.
                write(ser, sid, ADDR_GOAL, le(p))
                write(ser, sid, ADDR_TORQUE, [1])
        self.rest = {sid: float(v) for sid, v in REST_RAW.items()}
        self.pose = {sid: float(self.raw[sid]) for sid in REST_RAW}
        self.pan = float(self.raw[ID_PAN])
        self.pan_lim = PAN_LIM
        self.roll = float(self.raw[ID_ROLL])
        self.roll_lim = (self.roll - ROLL_RANGE, self.roll + ROLL_RANGE)
        self.grip = float(self.raw[ID_GRIP])
        self.grip_lim = GRIP_LIM
        self.relaxed = parked
        write_arm_state("limp" if parked else "holding")
        print(f"[base_host] arm {'limp (parked)' if parked else 'holding'} at "
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
        write_arm_state("holding")
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
            write_arm_state("limp")
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
        # Direct per-joint velocity. Left stick fwd/back drives LIFT + ELBOW as
        # a coordinated reach (ELBOW opposite sign so +vf reaches forward); Y/A
        # drives WRIST pitch. Each integrates at its own rate, clamped to limits.
        if self.relaxed:
            self.wake()
        self.pose[ID_LIFT] = clamp(self.pose[ID_LIFT] + vf * LIFT_RATE * dt,
                                   *ARM_LIMS[ID_LIFT])
        self.pose[ID_ELBOW] = clamp(self.pose[ID_ELBOW] - vf * ELBOW_RATE * dt,
                                    *ARM_LIMS[ID_ELBOW])
        self.pose[ID_WRIST] = clamp(self.pose[ID_WRIST] - vz * WRIST_RATE * dt,
                                    *ARM_LIMS[ID_WRIST])
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
        write_arm_state("none")
        print(f"[base_host] arm disabled: {e}", flush=True)

    ctx = zmq.Context()
    sock = ctx.socket(zmq.PULL)
    # No CONFLATE: with several PUSH peers (pad/GUI/MCP) conflation could drop
    # a high-priority frame in favor of a low-priority one that arrived later.
    # Instead every poll drains the whole queue and arbitrates per message.
    sock.setsockopt(zmq.RCVHWM, 64)
    sock.bind(BIND)
    poller = zmq.Poller()
    poller.register(sock, zmq.POLLIN)
    print(f"[base_host] listening on {BIND}", flush=True)

    last_cmd = time.time()
    last_batt = 0.0
    last_arm_cmd = time.time()
    # Base-velocity priority mux (ported from rdk-x5 cmd_vel_mux): a source
    # keeps the base bus for BASE_HOLD_S after its last base frame; lower
    # priority base frames are dropped meanwhile (arm keys pass regardless).
    # Untagged legacy senders rank as "gui" so an old GUI binary still
    # outranks the LLM; only the physical pad outranks everything.
    prio_last = {}                 # priority level -> last base frame time
    base_owner = -1                # last announced owner (for log edges)
    last_base = time.time()        # last APPLIED base command (watchdog feed;
                                   # arm-only traffic must not refresh it)
    moving = False
    relaxing = False
    mid_seek = False
    motion_on = read_motion()
    if not motion_on:
        print("[base_host] SAFETY: motion output DISABLED (latched)", flush=True)
    try:
        while True:
            socks = dict(poller.poll(timeout=50))
            while sock in socks:
                try:
                    raw = sock.recv_string(zmq.NOBLOCK)
                except zmq.Again:
                    break
                try:
                    data = json.loads(raw)
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
                    motion_req = data.get("safety.motion")
                except Exception as e:
                    print(f"[base_host] bad command: {e}", flush=True)
                    continue
                # Serial errors must NOT be swallowed as "bad command": if the
                # USB serial drops it re-enumerates under a new tty and this fd
                # is dead forever. Let OSError escape -> exit(1) -> systemd
                # restarts us against the stable /dev/serial/by-id path.
                now = time.time()
                if motion_req is not None and bool(motion_req) != motion_on:
                    motion_on = bool(motion_req)
                    write_motion(motion_on)
                    if not motion_on:
                        stop(ser)
                        moving = False
                    print(f"[base_host] SAFETY: motion output "
                          f"{'ENABLED' if motion_on else 'DISABLED'}", flush=True)
                if not motion_on:
                    # Freeze latched glides too, or re-enabling would replay a
                    # relax/mid request queued while the switch was off.
                    relaxing = mid_seek = False
                if base:
                    p = BASE_PRIO.get(data.get("src"), BASE_PRIO["gui"])
                    if base_blocked(prio_last, p, now):
                        base = False          # a higher-priority source owns it
                    else:
                        prio_last[p] = now
                        if p != base_owner:
                            base_owner = p
                            print(f"[base_host] base owner -> "
                                  f"{BASE_PRIO_NAME.get(p, '?')}", flush=True)
                if base and motion_on:
                    speeds = solve(vx, vy, om)
                    drive(ser, speeds)
                    moving = any(abs(v) > 1 for v in speeds.values())
                    last_base = now
                if arm and motion_on and dq is not None and len(dq) == 6:
                    relaxing = False
                    mid_seek = False
                    arm.follow(dq)
                    last_arm_cmd = now
                if arm and motion_on and any(ee):
                    relaxing = False          # any arm input cancels/wakes
                    mid_seek = False
                    arm.step(*ee, dt=min(0.1, now - last_cmd))
                    last_arm_cmd = now
                if data.get("arm.mid") or data.get("arm.relax"):
                    last_arm_cmd = now
                last_cmd = now
            if moving and time.time() - last_base > WATCHDOG_S:
                stop(ser)
                moving = False
                print("[base_host] watchdog: stopped (no command)", flush=True)
            # Idle auto-relax: torque holding costs power/heat for nothing, so
            # after ARM_IDLE_RELAX_S without arm input, glide to REST and limp.
            if (arm and motion_on and not arm.relaxed and not relaxing
                    and not mid_seek
                    and time.time() - last_arm_cmd > ARM_IDLE_RELAX_S):
                relaxing = True
                print("[base_host] arm idle -> auto relax", flush=True)
            if arm and motion_on and relaxing:
                arm.relax_tick(0.05)
                if arm.relaxed:
                    relaxing = False
            if arm and motion_on and mid_seek and arm.mid_tick(0.05):
                mid_seek = False
            # Battery telemetry: one wheel-servo voltage read every few seconds.
            # Cheap (~4 ms) and rare, so it never disturbs the 20 Hz drive loop;
            # a serial death here escapes as OSError like any other, triggering
            # the systemd restart against the stable by-id path.
            now_b = time.time()
            if now_b - last_batt >= BATT_PERIOD_S:
                last_batt = now_b
                for sid in WHEELS:
                    v = read_volt(ser, sid)
                    if v:
                        write_batt(v)
                        break
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
            write_arm_state("limp")
            print("[base_host] stopped, torque released", flush=True)
        except OSError:
            pass  # serial already gone


if __name__ == "__main__":
    main()

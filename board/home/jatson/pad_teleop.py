#!/usr/bin/env python3
"""LeKiwi gamepad teleop daemon (board-side, runs at boot via systemd).

Reads a USB gamepad with evdev and streams base-velocity commands over ZMQ
to base_host.py (or the original lekiwi_host) on tcp://127.0.0.1:5555 —
the exact same wire protocol the desktop GUI uses, so both can coexist
(ZMQ PULL fair-queues multiple PUSH peers) and base_host stays the single
owner of the serial bus.

Controls (works on DragonRise 0079:181c and Xbox Wireless Controller):
  RIGHT hand drives the base, LEFT hand drives the arm.
  right stick  : base — Y forward/back, X rotation; diagonal = arc-turn
  D-pad up/down: base forward/back, digital
  D-pad left/rt: base rotate CCW/CW, digital (keyboard Q/E)
  left stick   : arm — fwd = reach out toward EXTENDED pose, back = return
                 to REST; X = pan left/right
  Y / A (held) : arm toward UPRIGHT pose / back down toward REST
  X / B (held) : wrist roll left / right
  RB / RT      : gripper open (BTN_TR) / close — RT is BTN_TR2 on the
                 DragonRise pad, ABS_GAS (analog trigger) on Xbox; held
  START        : fold the arm to REST, then cut its torque (limp, safe to
                 hand-pose); touching any arm input wakes it back up
  LB / LT      : base speed down (BTN_TL) / up — LT is BTN_TL2 on the
                 DragonRise pad, ABS_BRAKE (analog trigger) on Xbox
  SELECT       : momentary e-stop — everything zero WHILE HELD (no latch;
                 a latched e-stop proved unrecoverable on no-name pads whose
                 printed labels don't match their event codes)

Safety: commands stream only while sticks are deflected; a single zero is
sent on release. base_host's own idle watchdog remains the backstop.
Hotplug: if the pad disappears the daemon sends zero and waits for it.
"""
import json
import sys
import time

import zmq
from evdev import InputDevice, ecodes, list_devices

ENDPOINT = sys.argv[1] if len(sys.argv) > 1 else "tcp://127.0.0.1:5555"
# Second, one-directional channel: a generic log bus the desktop GUI subscribes
# to. Any board process may PUB {"src","text"} here; the GUI timestamps and
# renders it in its bottom log panel. We use it to echo every button/axis so the
# operator can press the pad and see which control it maps to.
PUB_ENDPOINT = sys.argv[2] if len(sys.argv) > 2 else "tcp://*:5556"
LEVELS = [(0.10, 30.0), (0.25, 60.0), (0.40, 90.0)]  # base (m/s, deg/s)
DEADZONE = 0.18
HZ = 20

# Buttons acted on while HELD (not on press-edge): EE up/down, wrist roll, grip.
from evdev import ecodes as _ec
HELD_BTNS = {_ec.BTN_WEST, _ec.BTN_SOUTH, _ec.BTN_NORTH, _ec.BTN_EAST,
             _ec.BTN_TR, _ec.BTN_TR2}

# Human labels for the GUI log — what each control actually does in this mapping.
BTN_LABELS = {
    _ec.BTN_SELECT: "急停(按住)", _ec.BTN_START: "收臂松弛",
    _ec.BTN_TL: "降速", _ec.BTN_TL2: "升速",
    _ec.BTN_WEST: "臂上升", _ec.BTN_SOUTH: "臂下降",
    _ec.BTN_NORTH: "腕左旋", _ec.BTN_EAST: "腕右旋",
    _ec.BTN_TR: "爪张开", _ec.BTN_TR2: "爪合拢",
}
AXIS_LABELS = {
    _ec.ABS_RZ: "底盘前后", _ec.ABS_Z: "底盘旋转",
    _ec.ABS_HAT0Y: "十字前后", _ec.ABS_HAT0X: "十字转向",
    _ec.ABS_Y: "臂前伸/收", _ec.ABS_X: "臂平移",
    _ec.ABS_BRAKE: "升速(LT)", _ec.ABS_GAS: "爪合拢(RT)",
}
# Xbox reports LT/RT as these analog axes (rest 0, full ~max), not BTN_TL2/TR2.
TRIGGER_AXES = {_ec.ABS_BRAKE, _ec.ABS_GAS}


def key_name(code):
    n = ecodes.keys.get(code, code)
    return n[0] if isinstance(n, list) else n


def abs_name(code):
    n = ecodes.ABS.get(code, code)
    return n[0] if isinstance(n, list) else n


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def find_pad():
    """First device that declares BTN_GAMEPAD is our pad."""
    for path in list_devices():
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if ecodes.BTN_GAMEPAD in dev.capabilities().get(ecodes.EV_KEY, []):
            return dev
        dev.close()
    return None


def make_norm(dev):
    """Per-axis normalizer to [-1, 1] with deadzone, from the device's absinfo."""
    info = dict(dev.capabilities().get(ecodes.EV_ABS, []))

    def norm(code, value):
        ai = info.get(code)
        if value is None or ai is None or ai.max == ai.min:
            return 0.0  # unknown axis/value must mean "centered", never "deflected"
        center = (ai.max + ai.min) / 2.0
        n = (value - center) / ((ai.max - ai.min) / 2.0)
        if abs(n) < DEADZONE:
            return 0.0
        return (n - DEADZONE * (1 if n > 0 else -1)) / (1.0 - DEADZONE)

    return norm


def main():
    ctx = zmq.Context()
    sock = ctx.socket(zmq.PUSH)
    sock.setsockopt(zmq.SNDHWM, 1)  # never queue a backlog of stale commands
    sock.connect(ENDPOINT)
    log(f"PUSH -> {ENDPOINT}")

    pub = ctx.socket(zmq.PUB)
    pub.setsockopt(zmq.SNDHWM, 20)   # log is best-effort; drop under backlog
    pub.bind(PUB_ENDPOINT)
    log(f"PUB  -> {PUB_ENDPOINT}")

    def emit(text):
        try:
            pub.send_string(json.dumps({"src": "pad", "text": text}),
                            zmq.NOBLOCK)
        except zmq.Again:
            pass  # no subscriber / slow link: the live log is disposable

    def send(x, y, theta, vf=0.0, vpan=0.0, vz=0.0, vroll=0.0, gv=0.0, relax=0):
        try:
            sock.send_string(
                f'{{"src": "pad", "x.vel": {x:.3f}, "y.vel": {y:.3f}, "theta.vel": {theta:.1f},'
                f' "ee.vf": {vf:.2f}, "ee.vpan": {vpan:.2f}, "ee.vz": {vz:.2f},'
                f' "ee.vroll": {vroll:.2f}, "grip.v": {gv:.1f}, "arm.relax": {relax}}}',
                zmq.NOBLOCK,
            )
        except zmq.Again:
            pass  # host down; its watchdog keeps the base stopped anyway

    while True:
        dev = find_pad()
        if dev is None:
            time.sleep(2.0)
            continue
        log(f"pad: {dev.name} @ {dev.path}")
        emit(f"手柄就绪 {dev.name}")
        norm = make_norm(dev)
        import os
        os.set_blocking(dev.fd, False)

        def axis_level(code, value):
            # Coarse, de-bounced level so the log echoes meaningful moves only:
            # D-pad is -1/0/1, triggers are pressed/released, sticks step by 0.25.
            if code in (ecodes.ABS_HAT0X, ecodes.ABS_HAT0Y):
                return float(value)
            if code in TRIGGER_AXES:
                return 1.0 if value > 128 else 0.0
            return round(norm(code, value) * 4) / 4

        axis_lvl = {}          # last emitted level per axis, to suppress jitter

        # Prime every axis with its REAL current position. An empty dict would
        # default untouched axes to raw 0 — on a 0..255/center-128 pad that
        # reads as full deflection and drives the base with nobody touching it.
        axes = {
            code: dev.absinfo(code).value
            for code, _ in dev.capabilities().get(ecodes.EV_ABS, [])
        }

        absinfo = dict(dev.capabilities().get(ecodes.EV_ABS, []))

        def trig(code):
            # Analog trigger "pressed": raw past mid-scale. Triggers rest at min
            # (not center), so norm() (built for centered sticks) can't be used.
            ai = absinfo.get(code)
            return ai is not None and axes.get(code, ai.min) > (ai.min + ai.max) / 2

        speed = 1
        lt_prev = False        # analog LT edge, so one pull = one speed step
        estop_held = False
        held = set()           # face buttons currently held (EE up/down, grip)
        was_active = False
        last_cmd_log = 0.0
        try:
            while True:
                try:
                    for ev in dev.read():
                        if ev.type == ecodes.EV_ABS:
                            axes[ev.code] = ev.value
                            lvl = axis_level(ev.code, ev.value)
                            if axis_lvl.get(ev.code) != lvl:
                                axis_lvl[ev.code] = lvl
                                lab = AXIS_LABELS.get(ev.code, "")
                                emit(f"{abs_name(ev.code)} {lvl:+.2f}"
                                     + (f"  {lab}" if lab else ""))
                        elif ev.type == ecodes.EV_KEY:
                            if ev.value in (0, 1):   # press / release edge
                                lab = BTN_LABELS.get(ev.code, "")
                                emit(f"{key_name(ev.code)} "
                                     f"{'↓' if ev.value else '↑'}"
                                     + (f"  {lab}" if lab else ""))
                            if ev.code == ecodes.BTN_SELECT:
                                estop_held = ev.value != 0
                                if estop_held:
                                    send(0.0, 0.0, 0.0)
                                    log("e-stop held (release to resume)")
                            elif ev.code in HELD_BTNS:
                                if ev.value == 1:
                                    held.add(ev.code)
                                elif ev.value == 0:
                                    held.discard(ev.code)
                            elif ev.value == 1:
                                if ev.code == ecodes.BTN_START:
                                    send(0.0, 0.0, 0.0, relax=1)
                                    log("arm relax requested (fold + limp)")
                                elif ev.code == ecodes.BTN_TL:
                                    speed = max(0, speed - 1)
                                    log(f"speed -> {LEVELS[speed]}")
                                elif ev.code == ecodes.BTN_TL2:
                                    speed = min(len(LEVELS) - 1, speed + 1)
                                    log(f"speed -> {LEVELS[speed]}")
                                else:
                                    # No-name pads mismatch printed labels vs
                                    # codes; log presses so mapping is fixable.
                                    log(f"button {ecodes.keys.get(ev.code, ev.code)}")
                except BlockingIOError:
                    pass

                xy, th = LEVELS[speed]
                # Base = right hand. Up on stick = negative raw = forward.
                x = -norm(ecodes.ABS_RZ, axes.get(ecodes.ABS_RZ)) * xy
                theta = -norm(ecodes.ABS_Z, axes.get(ecodes.ABS_Z)) * th
                y = 0.0
                # D-pad, digital full-scale: up/down drives, left/right turns.
                hx = axes.get(ecodes.ABS_HAT0X, 0)
                hy = axes.get(ecodes.ABS_HAT0Y, 0)
                if hy:
                    x = -hy * xy
                if hx:
                    theta = -hx * th

                # Xbox LT (analog) = speed up, rising-edge only. The DragonRise
                # BTN_TL2 path in the EV_KEY branch above handles the digital pad.
                lt_now = trig(ecodes.ABS_BRAKE)
                if lt_now and not lt_prev:
                    speed = min(len(LEVELS) - 1, speed + 1)
                    log(f"speed -> {LEVELS[speed]}")
                lt_prev = lt_now

                # Arm = left hand, all -1..1: stick fwd = reach out, back =
                # return to rest; Y/A = upright/down; X/B = wrist roll;
                # triggers = gripper.
                vf = -norm(ecodes.ABS_Y, axes.get(ecodes.ABS_Y))
                vpan = norm(ecodes.ABS_X, axes.get(ecodes.ABS_X))  # left/right un-reversed
                vz = float((ecodes.BTN_WEST in held) - (ecodes.BTN_SOUTH in held))
                vroll = float((ecodes.BTN_NORTH in held) - (ecodes.BTN_EAST in held))
                # Gripper: RB open / RT close, held. RT = BTN_TR2 (DragonRise)
                # or ABS_GAS analog trigger (Xbox).
                gv = float(ecodes.BTN_TR in held) - float(
                    ecodes.BTN_TR2 in held or trig(ecodes.ABS_GAS))

                active = (not estop_held) and (x or y or theta or vf or vpan
                                               or vz or vroll or gv)
                if active:
                    send(x, y, theta, vf, vpan, vz, vroll, gv)
                    now = time.time()
                    if now - last_cmd_log > 1.0:   # visible pulse in the journal
                        log(f"cmd x={x:+.2f} th={theta:+.1f} ee f={vf:+.2f} "
                            f"pan={vpan:+.2f} z={vz:+.0f} roll={vroll:+.0f} g={gv:+.0f}")
                        last_cmd_log = now
                elif was_active:
                    send(0.0, 0.0, 0.0)
                    log("released -> zero")
                was_active = bool(active)
                time.sleep(1.0 / HZ)
        except OSError:
            log("pad disconnected")
            emit("手柄断开")
            send(0.0, 0.0, 0.0)
            try:
                dev.close()
            except OSError:
                pass
            time.sleep(2.0)


if __name__ == "__main__":
    main()

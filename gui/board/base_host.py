#!/usr/bin/env python3
"""Minimal LeKiwi base host — drives the 3 wheels only, no calibration needed.

A drop-in for `lekiwi_host` when all you want is to teleop the base. The real
lekiwi_host forces interactive arm calibration on connect() (EOFErrors over SSH),
but the wheels never need calibration, so this speaks the same ZMQ wire contract
without any of that:

  bind  PULL  tcp://*:5555
  recv  one JSON string per command: {"x.vel": m/s, "y.vel": m/s, "theta.vel": deg/s}

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
WHEELS = {7: 240.0 - 90, 8: 0.0 - 90, 9: 120.0 - 90}   # id -> mounting angle (deg)
BASE_R, WHEEL_R, MAX_RAW = 0.125, 0.05, 2500


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

    ctx = zmq.Context()
    sock = ctx.socket(zmq.PULL)
    sock.setsockopt(zmq.CONFLATE, 1)          # only ever act on the newest command
    sock.bind(BIND)
    poller = zmq.Poller()
    poller.register(sock, zmq.POLLIN)
    print(f"[base_host] listening on {BIND}", flush=True)

    last_cmd = time.time()
    moving = False
    try:
        while True:
            socks = dict(poller.poll(timeout=50))
            if sock in socks:
                try:
                    data = json.loads(sock.recv_string())
                    speeds = solve(float(data["x.vel"]), float(data["y.vel"]), float(data["theta.vel"]))
                    drive(ser, speeds)
                    moving = any(abs(v) > 1 for v in speeds.values())
                    last_cmd = time.time()
                except Exception as e:
                    print(f"[base_host] bad command: {e}", flush=True)
            elif moving and time.time() - last_cmd > WATCHDOG_S:
                stop(ser)
                moving = False
                print("[base_host] watchdog: stopped (no command)", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop(ser)
        for sid in WHEELS:
            write(ser, sid, ADDR_TORQUE, [0])
        ser.close()
        print("[base_host] stopped, torque released", flush=True)


if __name__ == "__main__":
    main()

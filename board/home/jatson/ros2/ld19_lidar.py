#!/usr/bin/env python3
"""LDROBOT LD19 (D300-family) serial lidar -> sensor_msgs/LaserScan on /scan.

Protocol (fixed 47-byte packets, streams on power-up, no commands needed):
  0x54 0x2C | speed u16 (deg/s) | start_angle u16 (0.01 deg) |
  12 x [dist u16 (mm), confidence u8] | end_angle u16 (0.01 deg) |
  timestamp u16 (ms) | crc8
Verified on this unit: header 54 2C visible in the raw stream at 230400 baud.

- Port is the by-id path (CH9102 serial 5AE2008090) — the servo bus is the
  SAME 1a86 chip family, so bare /dev/ttyACM* indexes must never be used here.
- LD19 spins clockwise; angles are converted to REP-103 CCW before binning.
- One revolution is accumulated (start-angle wrap detected), binned into
  fixed-width slots, published once per rev (~10 Hz).
- Same crash discipline as depth_preview.py: reader thread death or a stalled
  stream -> os._exit(1) so systemd restarts a clean process (hot-unplug safe).
"""
import math
import os
import threading
import time

import serial
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import LaserScan

PORT = os.environ.get(
    'LD19_PORT',
    '/dev/serial/by-id/usb-1a86_USB_Single_Serial_5AE2008090-if00')
BAUD = 230400
TOPIC = '/scan'
FRAME_ID = 'laser'
BINS = 450                 # 4500 samples/s / 10 Hz rev -> ~0.8 deg per bin
RANGE_MIN = 0.02
RANGE_MAX = 12.0
MIN_CONF = 15              # LD19 confidence below this is speckle noise
STALE_TIMEOUT = 3.0
FIRST_GRACE = 8.0

PKT_LEN = 47
HDR0, HDR1 = 0x54, 0x2C

_CRC = []
for i in range(256):
    c = i
    for _ in range(8):
        c = ((c << 1) ^ 0x4D) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    _CRC.append(c)


def crc8(buf):
    c = 0
    for b in buf:
        c = _CRC[(c ^ b) & 0xFF]
    return c


def bin_points(pts, n=BINS):
    """One revolution of (lidar_angle_deg, dist_m, conf) -> (ranges, intens).

    LD19 angles grow CLOCKWISE; REP-103 wants CCW positive, hence the 360-ang
    flip. Bins with no valid return stay inf. Multiple returns per bin keep
    the NEAREST — safety-conservative for obstacle logic downstream."""
    ranges = [float('inf')] * n
    intens = [0.0] * n
    for ang, dist, conf in pts:
        if conf < MIN_CONF or not (RANGE_MIN <= dist <= RANGE_MAX):
            continue
        b = int(((360.0 - ang) % 360.0) / 360.0 * n) % n
        if dist < ranges[b]:
            ranges[b] = dist
            intens[b] = float(conf)
    return ranges, intens


class LD19(Node):
    def __init__(self):
        super().__init__('ld19_lidar')
        self.pub = self.create_publisher(LaserScan, TOPIC, 1)
        self.ser = serial.Serial(PORT, BAUD, timeout=1.0)
        self.points = []           # (angle_deg_lidar, dist_m, conf) of current rev
        self.prev_start = None
        self.last_mono = None
        self.fatal = False
        self.start_mono = time.monotonic()
        self.rev_speed = 3600.0    # deg/s, updated from packets
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.create_timer(0.5, self._watchdog)
        self.get_logger().info(f'ld19_lidar up: {PORT} -> {TOPIC} ({BINS} bins)')

    # -- serial side ----------------------------------------------------
    def _read_loop(self):
        buf = bytearray()
        try:
            while True:
                buf += self.ser.read(256)
                while True:
                    i = buf.find(HDR0)
                    if i < 0:
                        buf.clear()
                        break
                    if len(buf) - i < PKT_LEN:
                        del buf[:i]
                        break
                    pkt = bytes(buf[i:i + PKT_LEN])
                    if pkt[1] != HDR1 or crc8(pkt[:46]) != pkt[46]:
                        del buf[:i + 1]          # false header, resync
                        continue
                    del buf[:i + PKT_LEN]
                    self._packet(pkt)
        except Exception as e:                   # noqa: BLE001
            self.get_logger().error(f'serial read failed: {e}')
            self.fatal = True

    def _packet(self, p):
        speed = p[2] | p[3] << 8                 # deg/s
        start = (p[4] | p[5] << 8) / 100.0
        end = (p[42] | p[43] << 8) / 100.0
        if speed:
            self.rev_speed = float(speed)
        span = (end - start) % 360.0
        # revolution boundary: start angle wrapped past 0
        if self.prev_start is not None and start < self.prev_start - 180.0:
            self._flush_rev()
        self.prev_start = start
        for k in range(12):
            o = 6 + 3 * k
            dist = (p[o] | p[o + 1] << 8) / 1000.0   # m
            conf = p[o + 2]
            ang = (start + span * k / 11.0) % 360.0
            self.points.append((ang, dist, conf))
        self.last_mono = time.monotonic()

    def _flush_rev(self):
        pts, self.points = self.points, []
        if len(pts) < 100:                       # partial first rev, drop
            return
        n = BINS
        ranges, intens = bin_points(pts, n)
        rev_t = 360.0 / self.rev_speed if self.rev_speed else 0.1
        m = LaserScan()
        m.header.stamp = self.get_clock().now().to_msg()
        m.header.frame_id = FRAME_ID
        m.angle_min = 0.0
        m.angle_max = 2.0 * math.pi * (n - 1) / n
        m.angle_increment = 2.0 * math.pi / n
        m.time_increment = rev_t / n
        m.scan_time = rev_t
        m.range_min = RANGE_MIN
        m.range_max = RANGE_MAX
        m.ranges = ranges
        m.intensities = intens
        self.pub.publish(m)

    # -- lifecycle ------------------------------------------------------
    def _watchdog(self):
        now = time.monotonic()
        if self.fatal:
            self._die('serial thread died')
        if self.last_mono is None:
            if now - self.start_mono > FIRST_GRACE:
                self._die('no lidar packets after startup')
        elif now - self.last_mono > STALE_TIMEOUT:
            self._die('lidar stream stalled')

    def _die(self, why):
        self.get_logger().error(f'{why} — exiting for systemd restart')
        os._exit(1)


def main():
    rclpy.init()
    node = None
    try:
        node = LD19()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

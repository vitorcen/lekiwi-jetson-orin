#!/usr/bin/env python3
"""10-DOF IMU (CH340 serial) -> /imu/data + /imu/mag + /imu/temp + /imu/pressure.

Wire protocol (reverse-engineered on this unit, 115200 8N1, streams on
power-up, no commands needed). Frame: 0x7E 0x23 | len u8 (TOTAL frame bytes)
| type u8 | payload | checksum u8 (sum of all preceding bytes & 0xFF).
Four frame types cycle continuously:
  0x04 len 23: 9 x int16 LE — accel xyz (/2048 g), gyro xyz (raw, assumed
               +/-2000 dps full scale -> /16.384 dps), mag xyz (raw counts)
  0x16 len 21: quaternion 4 x float32 LE, order (w, x, y, z) — verified:
               yaw/roll recomputed from it match the euler frame exactly
  0x26 len 17: euler 3 x float32 LE (roll, pitch, yaw) radians — not
               republished; /imu/data carries the quaternion
  0x32 len 21: 4 x float32 LE — altitude m, temperature C, pressure Pa,
               pressure2 Pa (near-duplicate of [2], unused)

- Port is the by-id path; this CH340 exposes no unique serial number, so the
  path is stable only while it is the sole 1a86:7523 device on the board
  (servo bus and lidar are CH9102 == different by-id names, no collision).
- GYRO_SCALE is an assumption pending a field check (unit is stationary at
  dev time; raw stream shows exact zeros, so the sign/scale cannot be
  verified remotely). Revisit before feeding an EKF.
- Mag is raw counts published as-is in MagneticField (NOT Tesla) — scale
  factor of this unknown magnetometer is TBD; heading display in the GUI
  only needs ratios.
- Same crash discipline as ld19_lidar.py: reader death or a stalled stream
  -> os._exit(1) so systemd restarts a clean process (hot-unplug safe).
"""
import math
import os
import struct
import threading
import time

import serial
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import FluidPressure, Imu, MagneticField, Temperature
from fps_diag import FpsDiag

PORT = os.environ.get(
    'IMU_PORT', '/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0')
BAUD = 115200
FRAME_ID = 'imu_link'
G = 9.80665
ACCEL_SCALE = G / 2048.0                 # +/-16g int16, gravity-verified
GYRO_SCALE = math.radians(1.0 / 16.384)  # ASSUMED +/-2000 dps int16
STALE_TIMEOUT = 3.0
FIRST_GRACE = 8.0
BARO_PERIOD = 0.5                        # baro republish cap (device ~rate)

HDR0, HDR1 = 0x7E, 0x23
T_RAW9, T_QUAT, T_EULER, T_BARO = 0x04, 0x16, 0x26, 0x32


class Imu10Dof(Node):
    def __init__(self):
        super().__init__('imu_10dof')
        self.pub_imu = self.create_publisher(Imu, '/imu/data', 1)
        self.pub_mag = self.create_publisher(MagneticField, '/imu/mag', 1)
        self.pub_temp = self.create_publisher(Temperature, '/imu/temp', 1)
        self.pub_press = self.create_publisher(FluidPressure, '/imu/pressure', 1)
        self.diag = FpsDiag(self, 'imu_10dof')
        self.ser = serial.Serial(PORT, BAUD, timeout=1.0)
        self.raw9 = None               # latest (ax..mz) int16 tuple
        self.last_mono = None
        self.last_baro_pub = 0.0
        self.fatal = False
        self.start_mono = time.monotonic()
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.create_timer(0.5, self._watchdog)
        self.get_logger().info(f'imu_10dof up: {PORT} -> /imu/*')

    # -- serial side ----------------------------------------------------
    def _read_loop(self):
        buf = bytearray()
        try:
            while True:
                buf += self.ser.read(128)
                while True:
                    i = buf.find(HDR0)
                    if i < 0:
                        buf.clear()
                        break
                    if len(buf) - i < 5:
                        del buf[:i]
                        break
                    if buf[i + 1] != HDR1:
                        del buf[:i + 1]          # false header, resync
                        continue
                    flen = buf[i + 2]
                    if not 5 <= flen <= 64:
                        del buf[:i + 1]
                        continue
                    if len(buf) - i < flen:
                        del buf[:i]
                        break
                    frame = bytes(buf[i:i + flen])
                    if sum(frame[:-1]) & 0xFF != frame[-1]:
                        del buf[:i + 1]
                        continue
                    del buf[:i + flen]
                    self._frame(frame[3], frame[4:-1])
        except Exception as e:                   # noqa: BLE001
            self.get_logger().error(f'serial read failed: {e}')
            self.fatal = True

    def _frame(self, ftype, pl):
        self.last_mono = time.monotonic()
        if ftype == T_RAW9 and len(pl) == 18:
            self.raw9 = struct.unpack('<9h', pl)
        elif ftype == T_QUAT and len(pl) == 16:
            self._pub_imu(struct.unpack('<4f', pl))
        elif ftype == T_BARO and len(pl) == 16:
            self._pub_baro(struct.unpack('<4f', pl))
        # T_EULER dropped: quaternion is the single orientation truth

    # -- publishers -----------------------------------------------------
    def _pub_imu(self, quat):
        if self.raw9 is None:
            return
        w, x, y, z = quat
        ax, ay, az, gx, gy, gz, mx, my, mz = self.raw9
        now = self.get_clock().now().to_msg()
        m = Imu()
        m.header.stamp = now
        m.header.frame_id = FRAME_ID
        m.orientation.w, m.orientation.x = float(w), float(x)
        m.orientation.y, m.orientation.z = float(y), float(z)
        m.angular_velocity.x = gx * GYRO_SCALE
        m.angular_velocity.y = gy * GYRO_SCALE
        m.angular_velocity.z = gz * GYRO_SCALE
        m.linear_acceleration.x = ax * ACCEL_SCALE
        m.linear_acceleration.y = ay * ACCEL_SCALE
        m.linear_acceleration.z = az * ACCEL_SCALE
        self.pub_imu.publish(m)
        mg = MagneticField()
        mg.header.stamp = now
        mg.header.frame_id = FRAME_ID
        mg.magnetic_field.x = float(mx)      # raw counts, scale TBD
        mg.magnetic_field.y = float(my)
        mg.magnetic_field.z = float(mz)
        self.pub_mag.publish(mg)
        self.diag.bump()

    def _pub_baro(self, vals):
        now_m = time.monotonic()
        if now_m - self.last_baro_pub < BARO_PERIOD:
            return
        self.last_baro_pub = now_m
        _alt, temp_c, press_pa, _ = vals
        now = self.get_clock().now().to_msg()
        t = Temperature()
        t.header.stamp = now
        t.header.frame_id = FRAME_ID
        t.temperature = float(temp_c)
        self.pub_temp.publish(t)
        p = FluidPressure()
        p.header.stamp = now
        p.header.frame_id = FRAME_ID
        p.fluid_pressure = float(press_pa)
        self.pub_press.publish(p)

    # -- lifecycle ------------------------------------------------------
    def _watchdog(self):
        now = time.monotonic()
        if self.fatal:
            self._die('serial thread died')
        if self.last_mono is None:
            if now - self.start_mono > FIRST_GRACE:
                self._die('no imu frames after startup')
        elif now - self.last_mono > STALE_TIMEOUT:
            self._die('imu stream stalled')

    def _die(self, why):
        self.get_logger().error(f'{why} — exiting for systemd restart')
        os._exit(1)


def main():
    rclpy.init()
    node = None
    try:
        node = Imu10Dof()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

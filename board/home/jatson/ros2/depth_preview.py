#!/usr/bin/env python3
"""Astra Pro depth -> pseudo-color JPEG on /depth_preview/compressed.

Ported from the yahboom-rdk-x5 astra_preview.py, battle-tested findings intact:

- NO vendor astra_camera ROS driver: its laser/LDP handling leaves depth
  all-zero on Astra Pro units. Raw OpenNI2 delivers stable depth.
- Depth at 320x240: 640x480 16-bit (~18MB/s) would hog the single USB2 root
  this board hangs everything on (servo bus, UVC cams, this camera).
- NO color stream here at all: the Astra's UVC color (2bc5:0501) is
  isochronous and starves the depth bulk transfer (30fps -> ~1.5fps). The GUI
  RGB box stays reserved until camera ownership is sorted out with vlm-daemon.
- Only NEW depth frames are published; a stalled sensor stops the stream and a
  watchdog exits the process so systemd restarts it clean (recovers hot-unplug).

Colormap: near = red, far = blue, no-return = black.
Requires: pip3 install primesense; OpenNI2 redist (with liborbbec.so driver)
at $ASTRA_OPENNI_LIB (default ~/openni2_redist, from orbbec/ros2_astra_camera).
"""
import os
import time
import threading
import numpy as np
import cv2
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from primesense import openni2
from primesense import _openni2 as c_api

OPENNI_LIB = os.environ.get('ASTRA_OPENNI_LIB',
                            os.path.expanduser('~/openni2_redist'))
TOPIC = '/depth_preview/compressed'
NEAR_MM = 300      # red end
FAR_MM = 4000      # blue end
PUB_HZ = 15.0      # sensor is 30fps; 15 is smooth and cheap
JPEG_Q = 80
FIRST_FRAME_GRACE = 12.0   # allow this long for the first depth frame
STALE_TIMEOUT = 3.0        # no new depth frame for this long -> exit for restart

# mm -> colormap index as a 65536-entry uint8 LUT: per frame the colorize is a
# single fancy-index instead of float math over the whole image.
_MM = np.arange(65536, dtype=np.float32)
_DN = np.clip((_MM - NEAR_MM) / (FAR_MM - NEAR_MM), 0.0, 1.0)
DEPTH_LUT = ((1.0 - _DN) * 255.0).astype(np.uint8)      # near -> 255 -> red


class DepthPreview(Node):
    def __init__(self):
        super().__init__('depth_preview')
        self.pub = self.create_publisher(CompressedImage, TOPIC, 1)
        self.run = True
        self.latest = None
        self.seq = 0
        self.last_mono = None
        self.fatal = False
        self.last_pub_seq = 0     # == seq: nothing to publish until a frame lands
        self.start_mono = time.monotonic()

        if not os.path.isdir(OPENNI_LIB):
            raise RuntimeError(f'OpenNI lib dir not found: {OPENNI_LIB} (set $ASTRA_OPENNI_LIB)')
        openni2.initialize(OPENNI_LIB)
        self.dev = openni2.Device.open_any()
        self.depth = self.dev.create_depth_stream()
        self.depth.set_video_mode(c_api.OniVideoMode(
            pixelFormat=c_api.OniPixelFormat.ONI_PIXEL_FORMAT_DEPTH_1_MM,
            resolutionX=320, resolutionY=240, fps=30))
        # OpenNI2 defaults to selfie-mirror output (Kinect heritage) — real
        # world orientation wants it OFF, matching the front camera.
        self.depth.set_mirroring_enabled(False)
        self.depth.start()

        threading.Thread(target=self._depth_loop, daemon=True).start()
        self.create_timer(1.0 / PUB_HZ, self._tick)
        self.get_logger().info(f'depth_preview up: OpenNI2 direct 320x240 -> {TOPIC} @{PUB_HZ}Hz')

    def _depth_loop(self):
        while self.run:
            try:
                f = self.depth.read_frame()
                a = np.frombuffer(f.get_buffer_as_uint16(), dtype=np.uint16)
                self.latest = a.reshape(f.height, f.width).copy()
                self.seq += 1
                self.last_mono = time.monotonic()
            except Exception as e:                       # noqa: BLE001
                self.get_logger().error(f'depth read failed: {e}')
                self.fatal = True
                return

    def _fatal_exit(self, why):
        # os._exit: never unload native libs while the read thread may still be
        # inside OpenNI. systemd Restart= rebuilds a clean process.
        self.get_logger().error(f'{why} — exiting for systemd restart')
        os._exit(1)

    def _tick(self):
        now = time.monotonic()
        if self.fatal:
            self._fatal_exit('depth thread died')
        if self.last_mono is None:
            if now - self.start_mono > FIRST_FRAME_GRACE:
                self._fatal_exit('no depth frame after startup')
            return
        if now - self.last_mono > STALE_TIMEOUT:
            self._fatal_exit('depth stalled')
        if self.seq == self.last_pub_seq:                # no new frame -> don't re-send
            return
        self.last_pub_seq = self.seq
        d = self.latest
        color = cv2.applyColorMap(DEPTH_LUT[d], cv2.COLORMAP_JET)
        color[d == 0] = 0
        ok, jpg = cv2.imencode('.jpg', color, [cv2.IMWRITE_JPEG_QUALITY, JPEG_Q])
        if not ok:
            return
        m = CompressedImage()
        m.header.stamp = self.get_clock().now().to_msg()
        m.format = 'jpeg'
        m.data = jpg.tobytes()
        self.pub.publish(m)

    def shutdown(self):
        self.run = False
        for fn in (self.depth.stop, openni2.unload):
            try:
                fn()
            except Exception:                            # noqa: BLE001
                pass


def main():
    rclpy.init()
    node = None
    try:
        node = DepthPreview()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.shutdown()
            node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

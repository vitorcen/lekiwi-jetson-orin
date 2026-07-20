#!/usr/bin/env python3
"""Front camera -> /front_cam/compressed, forwarded from vlm-daemon.

vlm-daemon is the SINGLE owner of the front UVC camera (same rule as
base_host owning the serial bus) — this node never opens /dev/video*.
It polls the daemon's /frame.jpg over HTTP (already-encoded JPEG, Bearer
token) and republishes as CompressedImage.

Demand-driven: frames are fetched only while /front_cam/compressed has
subscribers, so an idle GUI keeps the vlm capture pipeline idle too.
"""
import os
import time
import urllib.request

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from fps_diag import FpsDiag

VLM_URL = os.environ.get('VLM_URL', 'http://127.0.0.1:8090')
TOKEN_FILE = os.environ.get(
    'VLM_TOKEN_FILE', '/home/jetson/work/lekiwi-jetson-orin/vlm/token')
TOPIC = '/front_cam/compressed'
PUB_HZ = 10.0
HTTP_TIMEOUT = 2.0


def is_new_frame(jpeg, ts, last_ts):
    """Publish gate. ts is the daemon's X-Frame-Ts header (may be absent):
    without it dedup is impossible — publishing duplicates beats going silent
    (ts=None must NEVER compare equal to last_ts=None and mute the feed)."""
    return bool(jpeg) and (ts is None or ts != last_ts)


class FrontCam(Node):
    def __init__(self):
        super().__init__('front_cam')
        with open(TOKEN_FILE) as f:
            self.token = f.read().strip()
        self.pub = self.create_publisher(CompressedImage, TOPIC, 1)
        self.diag = FpsDiag(self, 'front_cam')
        self.last_ts = None          # X-Frame-Ts of last published frame
        self.errs = 0
        self.create_timer(1.0 / PUB_HZ, self._tick)
        self.get_logger().info(f'front_cam up: {VLM_URL}/frame.jpg -> {TOPIC}')

    def _tick(self):
        if self.pub.get_subscription_count() == 0:
            return                   # nobody watching -> let vlm capture idle
        req = urllib.request.Request(
            f'{VLM_URL}/frame.jpg',
            headers={'Authorization': f'Bearer {self.token}'})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                ts = r.headers.get('X-Frame-Ts')
                xfps = r.headers.get('X-Fps')
                jpeg = r.read()
            self.errs = 0
            if xfps is not None:
                # vlm-daemon's own measured capture rate — the camera ability
                self.diag.gauge('cap_fps', xfps)
        except Exception as e:       # noqa: BLE001 — daemon restart/510 etc.
            self.errs += 1
            if self.errs in (1, 10):     # log first hit + once when persistent
                self.get_logger().warning(f'frame fetch failed: {e}')
            return
        if not is_new_frame(jpeg, ts, self.last_ts):
            return                   # stale frame -> don't re-send
        self.last_ts = ts
        m = CompressedImage()
        m.header.stamp = self.get_clock().now().to_msg()
        m.format = 'jpeg'
        m.data = jpeg
        self.pub.publish(m)
        self.diag.bump()


def main():
    rclpy.init()
    node = None
    try:
        node = FrontCam()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

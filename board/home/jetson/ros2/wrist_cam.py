#!/usr/bin/env python3
"""Wrist camera (Sunplus "2M", 1bcf:2281) -> /wrist_cam/compressed.

This UVC camera has no other owner, so the node opens it directly — but only
ON DEMAND: the device is opened when /wrist_cam/compressed gains a subscriber
and released when the last one leaves. Everything here shares one USB2 root
hub with the Astra depth stream and the servo bus; an always-on video stream
would eat bandwidth for nothing.
"""
import os
import threading
import time

import cv2
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage
from fps_diag import FpsDiag

DEV = os.environ.get(
    'WRIST_CAM_DEV', '/dev/v4l/by-id/usb-XHH-260128-A_2M-video-index0')
TOPIC = '/wrist_cam/compressed'
W, H, FPS = 640, 480, 30
PUB_HZ = 10.0
JPEG_Q = 80


class WristCam(Node):
    def __init__(self):
        super().__init__('wrist_cam')
        self.pub = self.create_publisher(CompressedImage, TOPIC, 1)
        self.diag = FpsDiag(self, 'wrist_cam')
        self.want = False            # demand flag, flipped by the timer
        self.latest = None
        self.seq = 0
        self.last_pub_seq = 0     # == seq: nothing to publish until a frame lands
        threading.Thread(target=self._cap_loop, daemon=True).start()
        self.create_timer(1.0 / PUB_HZ, self._tick)
        self.get_logger().info(f'wrist_cam up: {DEV} -> {TOPIC} (on demand)')

    def _cap_loop(self):
        cap = None
        while True:
            if not self.want:
                if cap is not None:
                    cap.release()
                    cap = None
                    self.get_logger().info('camera released (no subscribers)')
                time.sleep(0.3)
                continue
            if cap is None:
                cap = cv2.VideoCapture(DEV, cv2.CAP_V4L2)
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, W)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, H)
                cap.set(cv2.CAP_PROP_FPS, FPS)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if not cap.isOpened():
                    cap.release()
                    cap = None
                    self.get_logger().warning(f'open {DEV} failed, retrying')
                    time.sleep(2.0)
                    continue
                self.get_logger().info('camera opened')
            ok, frame = cap.read()
            if not ok:
                cap.release()
                cap = None           # hot-unplug / stall -> reopen path
                self.get_logger().warning('read failed, reopening')
                time.sleep(1.0)
                continue
            self.latest = frame
            self.seq += 1
            self.diag.bump('cap_fps')          # true camera delivery rate

    def _tick(self):
        self.want = self.pub.get_subscription_count() > 0
        if not self.want or self.seq == self.last_pub_seq:
            return
        self.last_pub_seq = self.seq
        ok, jpg = cv2.imencode('.jpg', self.latest,
                               [cv2.IMWRITE_JPEG_QUALITY, JPEG_Q])
        if not ok:
            return
        m = CompressedImage()
        m.header.stamp = self.get_clock().now().to_msg()
        m.format = 'jpeg'
        m.data = jpg.tobytes()
        self.pub.publish(m)
        self.diag.bump()


def main():
    rclpy.init()
    node = None
    try:
        node = WristCam()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()

"""Self-reported rates on /diagnostics (1 Hz, DiagnosticArray).

Publishers are the only honest source of their own rates — any subscriber-side
measurement is polluted by rosbridge throttling. Each node counts real events:

  bump('fps')      — frames actually published (ROS topic output rate)
  bump('cap_fps')  — frames actually captured off the sensor (camera ability)
  gauge(key, v)    — a rate measured elsewhere (e.g. vlm-daemon's X-Fps header)

The GUI subscribes to /diagnostics and shows 采集/发布/预览 side by side.
"""
import time

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue


class FpsDiag:
    def __init__(self, node, name):
        self.name = name
        self.node = node
        self.counts = {}
        self.gauges = {}
        self.t0 = time.monotonic()
        self.pub = node.create_publisher(DiagnosticArray, '/diagnostics', 1)
        node.create_timer(1.0, self._tick)

    def bump(self, key='fps'):
        self.counts[key] = self.counts.get(key, 0) + 1

    def gauge(self, key, value):
        self.gauges[key] = float(value)

    def _tick(self):
        now = time.monotonic()
        dt = now - self.t0
        self.t0 = now
        vals = [KeyValue(key=k, value=f'{c / dt:.1f}' if dt > 0 else '0.0')
                for k, c in self.counts.items()]
        vals += [KeyValue(key=k, value=f'{v:.1f}')
                 for k, v in self.gauges.items()]
        for k in self.counts:
            self.counts[k] = 0
        st = DiagnosticStatus(level=DiagnosticStatus.OK, name=self.name,
                              values=vals)
        m = DiagnosticArray()
        m.header.stamp = self.node.get_clock().now().to_msg()
        m.status = [st]
        self.pub.publish(m)

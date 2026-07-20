"""Import board-side nodes on a dev machine with no ROS / serial / camera.

The nodes import hardware libs at module top; the pure logic under test never
calls them, so stubs are enough. rclpy.node.Node must be a real class (the
nodes subclass it) — a MagicMock instance cannot be a base class.

Run:  uv run --with pytest --with numpy pytest tests/ -q
"""
import pathlib
import sys
import types
from unittest.mock import MagicMock

BOARD = pathlib.Path(__file__).resolve().parents[1] / 'board' / 'home' / 'jatson'
sys.path[:0] = [str(BOARD), str(BOARD / 'ros2')]


class _Node:
    pass


def _stub(name, **attrs):
    m = sys.modules.get(name)
    if m is None or isinstance(m, MagicMock):
        m = types.ModuleType(name)
        sys.modules[name] = m
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


rclpy = _stub('rclpy')
rclpy.node = _stub('rclpy.node', Node=_Node)
_stub('sensor_msgs')
_stub('sensor_msgs.msg', LaserScan=MagicMock(), CompressedImage=MagicMock())
_stub('diagnostic_msgs')
_stub('diagnostic_msgs.msg', DiagnosticArray=MagicMock(),
      DiagnosticStatus=MagicMock(), KeyValue=MagicMock())
_stub('serial', Serial=MagicMock())
_stub('zmq', PULL=7, RCVHWM=23, POLLIN=1, NOBLOCK=1,
      Context=MagicMock(), Poller=MagicMock(), Again=Exception)
_stub('cv2', applyColorMap=MagicMock(), imencode=MagicMock(),
      COLORMAP_JET=2, CAP_V4L2=200, VideoCapture=MagicMock(),
      VideoWriter_fourcc=MagicMock(), IMWRITE_JPEG_QUALITY=1,
      CAP_PROP_FOURCC=6, CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4,
      CAP_PROP_FPS=5, CAP_PROP_BUFFERSIZE=38)
prim = _stub('primesense', openni2=MagicMock())
prim._openni2 = MagicMock()
sys.modules['primesense._openni2'] = prim._openni2

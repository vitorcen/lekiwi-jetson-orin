"""imu_read pure logic in drive/mcp_server.py: euler/heading math, ISA
altitude, and the payload builder. The MCP/zmq/aiohttp sides are stubbed by
conftest; loaded via importlib because vlm/mcp_server.py shadows the name."""
import importlib.util
import math
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    'drive_mcp_server', ROOT / 'drive' / 'mcp_server.py')
dmcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dmcp)


def test_euler_identity_quat():
    e = dmcp.imu_euler_deg(1.0, 0.0, 0.0, 0.0)
    assert all(abs(e[k]) < 1e-9 for k in ('roll_deg', 'pitch_deg', 'yaw_deg'))


def test_euler_pure_yaw_90():
    # 90° CCW yaw: q = (cos45, 0, 0, sin45)
    s = math.sin(math.radians(45))
    e = dmcp.imu_euler_deg(math.cos(math.radians(45)), 0.0, 0.0, s)
    assert abs(e['yaw_deg'] - 90.0) < 1e-6
    assert abs(e['roll_deg']) < 1e-6 and abs(e['pitch_deg']) < 1e-6


def test_euler_gimbal_clamp():
    # slightly out-of-range asin input must not raise
    e = dmcp.imu_euler_deg(0.7071068, 0.0, 0.7071069, 0.0)
    assert abs(e['pitch_deg'] - 90.0) < 0.1


def test_heading_ccw_yaw_to_compass():
    assert dmcp.imu_heading_deg(0.0) == 0.0
    assert dmcp.imu_heading_deg(90.0) == 270.0    # CCW+ yaw -> CW compass
    assert dmcp.imu_heading_deg(-90.0) == 90.0
    assert dmcp.imu_heading_deg(-450.0) == 90.0   # wraps


def test_isa_altitude():
    assert abs(dmcp.isa_altitude_m(101325.0)) < 1e-6
    # ~1000 m ISA is ~89875 Pa
    assert abs(dmcp.isa_altitude_m(89875.0) - 1000.0) < 20.0


def _imu_msg(w=1.0, x=0.0, y=0.0, z=0.0):
    return {
        'orientation': {'w': w, 'x': x, 'y': y, 'z': z},
        'angular_velocity': {'x': 0.0, 'y': 0.0, 'z': math.radians(10.0)},
        'linear_acceleration': {'x': 0.0, 'y': 0.0, 'z': 9.81},
    }


def test_payload_full():
    p = dmcp.imu_payload({
        'imu': _imu_msg(),
        'mag': {'magnetic_field': {'x': 120.0, 'y': -30.0, 'z': 500.0}},
        'temp': {'temperature': 31.24},
        'pressure': {'fluid_pressure': 100325.0},
    })
    assert p['missing'] == []
    assert p['orientation']['heading_deg'] == 0.0
    assert p['angular_velocity_dps']['z'] == 10.0
    assert p['linear_acceleration_mps2']['z'] == 9.81
    assert p['magnetic_raw'] == {'x': 120.0, 'y': -30.0, 'z': 500.0}
    assert p['temperature_c'] == 31.2
    assert p['pressure_hpa'] == 1003.2
    assert p['altitude_m_isa'] > 0
    assert 'heading_note' in p


def test_payload_partial_baro_missing():
    p = dmcp.imu_payload({'imu': _imu_msg()})
    assert sorted(p['missing']) == ['mag', 'pressure', 'temp']
    assert 'orientation' in p and 'pressure_hpa' not in p


def test_payload_empty():
    p = dmcp.imu_payload({})
    assert sorted(p['missing']) == ['imu', 'mag', 'pressure', 'temp']

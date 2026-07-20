"""LD19 lidar protocol: CRC gate + angle binning.

CRC wrong in either direction = silent lidar or ghost obstacles.
Binning direction wrong = mirrored world: obstacle on the left painted on
the right — the worst kind of bug to find by eye.
"""
import math

import ld19_lidar as ld

# Two CRC-valid packets captured from the real unit (2026-07-20, serial
# 5AE2008090). Golden: byte 46 is the device's own CRC over bytes 0..45.
REAL_PACKETS = [bytes.fromhex(h) for h in (
    '542c090edb48a600b8a600b9a700b7a700b9a800b7a800b8'
    'a900b8aa00b8ab00b8ab00b7ac00b7ad00b72a4c4f5e3c',
    '542c250e774cad00b8ad00b9ae00bab000bab100bab200b6'
    'b400b6b600b0b900b6bb00b1bc00afbf00afc84f525eaa',
)]


def test_crc_table_known_prefix():
    # first entries of the published LDROBOT poly-0x4D table
    assert ld._CRC[:4] == [0x00, 0x4D, 0x9A, 0xD7]


def test_crc8_matches_device_on_real_packets():
    for pkt in REAL_PACKETS:
        assert len(pkt) == ld.PKT_LEN
        assert ld.crc8(pkt[:46]) == pkt[46]


def test_crc8_rejects_corruption():
    pkt = bytearray(REAL_PACKETS[0])
    pkt[10] ^= 0x01
    assert ld.crc8(pkt[:46]) != pkt[46]


# ---- bin_points: CW -> CCW flip, nearest-wins, validity filters ----------

def _bin_of(lidar_ang, n=ld.BINS):
    return int(((360.0 - lidar_ang) % 360.0) / 360.0 * n) % n


def test_bin_direction_flip():
    n = ld.BINS
    # obstacle straight ahead (lidar 0 deg) stays at REP-103 angle 0
    ranges, _ = ld.bin_points([(0.0, 1.0, 200)], n)
    assert ranges[0] == 1.0
    # lidar 90 deg is CLOCKWISE-right; REP-103 CCW puts it at 270 deg
    ranges, _ = ld.bin_points([(90.0, 2.0, 200)], n)
    assert ranges[int(270.0 / 360.0 * n)] == 2.0
    assert all(math.isinf(r) for i, r in enumerate(ranges)
               if i != int(270.0 / 360.0 * n))


def test_bin_nearest_return_wins():
    pts = [(10.0, 3.0, 200), (10.0, 1.5, 100)]
    ranges, intens = ld.bin_points(pts)
    b = _bin_of(10.0)
    assert ranges[b] == 1.5
    assert intens[b] == 100.0


def test_bin_filters_invalid():
    pts = [
        (20.0, 1.0, ld.MIN_CONF - 1),    # low confidence -> speckle
        (30.0, 0.0, 200),                # dist 0 = no return
        (40.0, ld.RANGE_MAX + 1, 200),   # beyond spec range
        (50.0, ld.RANGE_MIN / 2, 200),   # inside dead zone
    ]
    ranges, _ = ld.bin_points(pts)
    assert all(math.isinf(r) for r in ranges)

"""Camera publish gates + depth colormap LUT.

The front_cam ts=None case is a live specimen: sentinel collision
(None == None) would silently mute the feed forever — same bug family as the
wrist_cam seq=0/last=-1 encode-None crash fixed on 2026-07-20.
"""
import numpy as np

import front_cam
import depth_preview as dp


# ---- front_cam.is_new_frame ----------------------------------------------

def test_first_frame_publishes():
    assert front_cam.is_new_frame(b'\xff\xd8', '1.0', None)


def test_duplicate_ts_suppressed():
    assert not front_cam.is_new_frame(b'\xff\xd8', '1.0', '1.0')


def test_new_ts_publishes():
    assert front_cam.is_new_frame(b'\xff\xd8', '2.0', '1.0')


def test_missing_ts_header_never_mutes():
    # daemon without X-Frame-Ts: ts=None must not equal last_ts=None
    assert front_cam.is_new_frame(b'\xff\xd8', None, None)


def test_empty_payload_never_publishes():
    assert not front_cam.is_new_frame(b'', '2.0', '1.0')


# ---- depth_preview DEPTH_LUT: near=255(red end), far=0(blue), monotone ---

def test_lut_anchor_points():
    assert dp.DEPTH_LUT[dp.NEAR_MM] == 255
    assert dp.DEPTH_LUT[dp.FAR_MM] == 0


def test_lut_clips_outside_range():
    assert np.all(dp.DEPTH_LUT[: dp.NEAR_MM] == 255)
    assert np.all(dp.DEPTH_LUT[dp.FAR_MM:] == 0)


def test_lut_monotone_decreasing():
    assert np.all(np.diff(dp.DEPTH_LUT.astype(int)) <= 0)

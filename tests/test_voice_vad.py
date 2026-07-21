"""Pure VAD front-end logic: the shared open/close segmenter and digital make-up gain.
No sherpa / webrtcvad — those are imported lazily inside the engine classes and never
touched here. Run: uv run --with pytest --with numpy pytest tests/ -q"""
import numpy as np
import pytest

import voice_vad as vv


# ---- segment_voiced: the pure open/close aggregation ----------------------

def test_no_speech_yields_no_segments():
    assert vv.segment_voiced([False] * 20, min_speech_frames=3, min_silence_frames=3) == []


def test_open_needs_min_consecutive_speech():
    # two voiced frames then silence never reaches min_speech=3 -> no segment
    flags = [True, True, False, False, False, False]
    assert vv.segment_voiced(flags, 3, 3) == []


def test_single_clean_segment():
    # 4 voiced open (>=3), then 3 silence close (>=3). Segment spans voiced+trailing silence.
    flags = [True] * 4 + [False] * 3 + [False] * 2
    segs = vv.segment_voiced(flags, 3, 3)
    assert len(segs) == 1
    start, end = segs[0]
    assert start == 0
    assert end == 7            # 4 voiced + 3 silence frames included before close


def test_short_silence_does_not_split():
    # a 2-frame silence gap (< min_silence=3) stays inside one segment
    flags = [True] * 4 + [False, False] + [True] * 4 + [False] * 3
    segs = vv.segment_voiced(flags, 3, 3)
    assert len(segs) == 1


def test_two_segments_when_silence_long_enough():
    flags = ([True] * 4 + [False] * 3) + ([True] * 4 + [False] * 3)
    segs = vv.segment_voiced(flags, 3, 3)
    assert len(segs) == 2


def test_flush_emits_open_segment_without_trailing_silence():
    flags = [True] * 5                         # opens, never closes
    assert vv.segment_voiced(flags, 3, 3, flush=True) == [(0, 5)]
    assert vv.segment_voiced(flags, 3, 3, flush=False) == []


def test_leading_silence_is_dropped_from_start():
    flags = [False] * 5 + [True] * 4 + [False] * 3
    segs = vv.segment_voiced(flags, 3, 3)
    assert segs[0][0] == 5                      # segment starts at first voiced frame


def test_min_speech_one_frame_opens_immediately():
    flags = [True, False, False, False]
    assert vv.segment_voiced(flags, 1, 3) == [(0, 4)]


# ---- pre-roll look-back ----------------------------------------------------

def test_preroll_zero_is_stock_behaviour():
    flags = [False] * 5 + [True] * 4 + [False] * 3
    assert (vv.segment_voiced(flags, 3, 3, pre_roll_frames=0)
            == vv.segment_voiced(flags, 3, 3))          # default is 0 -> identical


def test_preroll_backfills_leading_frames():
    flags = [False] * 5 + [True] * 4 + [False] * 3
    # onset at frame 5; pre_roll=2 pulls the segment start back to frame 3
    segs = vv.segment_voiced(flags, 3, 3, pre_roll_frames=2)
    assert segs[0][0] == 3


def test_preroll_clamped_when_not_enough_leading_frames():
    # onset at frame 1, only 1 leading frame -> pre_roll=5 can't go below 0
    flags = [False] + [True] * 4 + [False] * 3
    segs = vv.segment_voiced(flags, 3, 3, pre_roll_frames=5)
    assert segs[0][0] == 0                              # clamped at stream start


def test_preroll_spans_across_silence_gap_frames():
    # leading silence must be included as pre-roll, proving the look-back rolls over silence
    flags = [False] * 10 + [True] * 3 + [False] * 3
    segs = vv.segment_voiced(flags, 3, 3, pre_roll_frames=4)
    assert segs[0][0] == 6                              # onset 10 - pre_roll 4


# ---- Segmenter.active reflects in-speech state ----------------------------

def test_segmenter_active_toggles():
    seg = vv._Segmenter(2, 2)
    assert not seg.active
    seg.push(True, np.array([0.0], dtype=np.float32))
    assert not seg.active                        # 1 voiced < min_speech=2
    seg.push(True, np.array([0.0], dtype=np.float32))
    assert seg.active                            # opened
    seg.push(False, np.array([0.0], dtype=np.float32))
    seg.push(False, np.array([0.0], dtype=np.float32))
    assert not seg.active                        # closed


# ---- apply_gain: identity at 0, clip at boundaries ------------------------

def test_gain_zero_is_identity_same_object():
    x = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    assert vv.apply_gain(x, 0) is x              # no allocation on the stock path


def test_gain_negative_or_junk_is_identity():
    x = np.array([0.1, -0.2], dtype=np.float32)
    assert vv.apply_gain(x, -5) is x
    assert vv.apply_gain(x, "bad") is x


def test_gain_amplifies_and_clips():
    x = np.array([0.1, -0.1, 0.9, -0.9], dtype=np.float32)
    y = vv.apply_gain(x, 20)                      # 20 dB -> x10
    assert y.dtype == np.float32
    assert np.isclose(y[0], 1.0)                 # 0.1*10 clipped to 1.0
    assert np.isclose(y[1], -1.0)
    assert y.max() <= 1.0 and y.min() >= -1.0


def test_gain_6db_roughly_doubles_below_clip():
    x = np.array([0.1], dtype=np.float32)
    y = vv.apply_gain(x, 6.0206)                  # ~x2
    assert np.isclose(y[0], 0.2, atol=1e-3)


# ---- frame math -----------------------------------------------------------

def test_frames_rounds_up_and_floor_one():
    assert vv._frames(0.25) == 13                 # 0.25/0.02 = 12.5 -> 13
    assert vv._frames(0.55) == 28                 # 0.55/0.02 = 27.5 -> 28
    assert vv._frames(0.0) == 1                   # never zero


# ---- make_vad refuses unknown / unavailable engines -----------------------

def test_make_vad_unknown_engine_raises():
    with pytest.raises(ValueError):
        vv.make_vad("bogus", {}, "/tmp/nope")


def test_make_vad_unavailable_engine_raises(tmp_path):
    # empty models dir -> silero/ten unavailable; no silent fallback, a hard raise.
    with pytest.raises(ValueError):
        vv.make_vad("silero", {}, str(tmp_path))

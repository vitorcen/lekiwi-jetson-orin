"""Pure ASR-observability logic: outcome classification, counters, dBFS, self-test
similarity. No numpy/sherpa/hardware — only voice_asr_obs."""
import voice_asr_obs as ao


# ---- segment outcome classification ---------------------------------------
def test_classify_accepted():
    assert ao.classify_segment("今天天气怎么样") == ao.ACCEPTED


def test_classify_empty_asr():
    assert ao.classify_segment("") == ao.EMPTY_ASR
    assert ao.classify_segment("   ") == ao.EMPTY_ASR
    assert ao.classify_segment(None) == ao.EMPTY_ASR


def test_classify_filler_beats_too_short():
    # "嗯" is 1 char AND a filler -> must read as filler, not too_short.
    assert ao.classify_segment("嗯") == ao.FILLER
    assert ao.classify_segment("嗯嗯") == ao.FILLER


def test_classify_too_short():
    assert ao.classify_segment("好") == ao.TOO_SHORT      # 1 char, not a filler


def test_classify_strips_whitespace():
    assert ao.classify_segment("你 好") == ao.ACCEPTED    # 2 chars after strip


# ---- dBFS conversion ------------------------------------------------------
def test_dbfs_full_scale_and_floor():
    assert ao.dbfs(1.0) == 0.0
    assert ao.dbfs(0.0) < -100          # floored, never -inf
    assert ao.dbfs(0.1) == -20.0


# ---- counters -------------------------------------------------------------
def test_asr_stats_counts_every_segment():
    s = ao.AsrStats()
    for o in (ao.ACCEPTED, ao.EMPTY_ASR, ao.EMPTY_ASR, ao.FILLER, ao.GATE):
        s.record(o)
    snap = s.snapshot()
    assert snap["segments"] == 5
    assert snap["empty_asr"] == 2
    assert snap["accepted"] == 1 and snap["filler"] == 1 and snap["gate"] == 1
    assert snap["too_short"] == 0


def test_asr_stats_snapshot_is_a_copy():
    s = ao.AsrStats()
    snap = s.snapshot()
    snap["segments"] = 999
    assert s.snapshot()["segments"] == 0


# ---- self-test similarity -------------------------------------------------
def test_similarity_identical_and_disjoint():
    assert ao.similarity("今天天气怎么样", "今天天气怎么样") == 1.0
    assert ao.similarity("今天天气怎么样", "abcdef") < 0.5


def test_selftest_pass_threshold():
    # one-char slip should still pass at 0.5
    assert ao.selftest_pass("今天天气怎么样", "今天天气怎么样啊") is True
    assert ao.selftest_pass("", "今天天气怎么样") is False

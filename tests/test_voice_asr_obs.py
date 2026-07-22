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


# ---- barge-in echo discrimination -----------------------------------------
RECENT = [(100.0, "今天天气晴朗,适合出门散步。"),
          (101.0, "记得带上水杯和帽子。")]


def test_echo_exact_and_fragment_of_one_sentence():
    assert ao.is_echo("今天天气晴朗,适合出门散步", RECENT, 102.0) is True
    assert ao.is_echo("适合出门散步", RECENT, 102.0) is True     # substring
    assert ao.is_echo("", RECENT, 102.0) is True                # empty → echo


def test_echo_straddling_two_sentences_caught_by_coverage():
    # tail of sentence 1 + head of sentence 2: matches NO single sentence
    # above sim, used to leak back in as a fake user turn (2026-07-22)
    cand = "适合出门散步记得带上水杯"
    for _, sent in RECENT:
        r = ao.similarity(cand, sent)
        assert r < 0.55, f"precondition: single-sentence sim must miss ({r})"
    assert ao.is_echo(cand, RECENT, 102.0) is True


def test_real_interruption_not_swallowed():
    assert ao.is_echo("帮我看看客厅里有什么", RECENT, 102.0) is False


def test_short_command_never_eaten_by_coverage():
    # <4 chars skips the coverage fallback: 停/等等 must survive even when the
    # played text happens to contain those characters
    recent = [(100.0, "我们等等再停下来休息。")]
    assert ao.is_echo("等一下", recent, 101.0) is False
    assert ao.is_echo("好的呢", recent, 101.0) is False


def test_echo_window_expires():
    assert ao.is_echo("适合出门散步", RECENT, 200.0) is False    # 20s window passed

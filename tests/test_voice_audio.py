import numpy as np
import pytest

from voice_audio import SentenceAccumulator, resample_pcm16


def test_resample_pcm16_converts_16k_to_24k_without_changing_level():
    source = np.full(160, 1234, dtype=np.int16)

    output = resample_pcm16(source, 16000, 24000)

    assert output.dtype == np.int16
    assert len(output) == 240
    assert np.all(output == 1234)


def test_resample_pcm16_preserves_empty_and_same_rate_buffers():
    empty = np.array([], dtype=np.int16)
    source = np.array([-32768, 0, 32767], dtype=np.int16)

    assert resample_pcm16(empty, 16000, 24000).size == 0
    assert np.array_equal(resample_pcm16(source, 24000, 24000), source)


def test_resample_pcm16_rejects_invalid_rates():
    with pytest.raises(ValueError):
        resample_pcm16(np.zeros(1, dtype=np.int16), 0, 24000)


def test_sentence_accumulator_waits_for_chinese_punctuation():
    text = (
        "小坏蛋，我没有卡车呀，我就是那个小黑车——三轮小机器人。"
        "不过刚才试了，轮子好像没听我的。你那边程序开着没？"
    )
    accumulator = SentenceAccumulator()
    sentences = []

    for char in text:
        sentences.extend(accumulator.push(char))
    sentences.extend(accumulator.flush())

    assert sentences == [
        "小坏蛋，我没有卡车呀，",
        "我就是那个小黑车——三轮小机器人。",
        "不过刚才试了，轮子好像没听我的。",
        "你那边程序开着没？",
    ]


def test_sentence_accumulator_bounds_text_without_punctuation():
    accumulator = SentenceAccumulator()
    sentences = list(accumulator.push("甲" * 80))
    sentences.extend(accumulator.flush())

    assert "".join(sentences) == "甲" * 80
    assert max(map(len, sentences)) <= 48


def test_sentence_accumulator_discards_punctuation_after_forced_split():
    accumulator = SentenceAccumulator()

    sentences = list(accumulator.push("甲" * 32))
    sentences.extend(accumulator.push("，后续内容。"))
    sentences.extend(accumulator.flush())

    assert sentences == ["甲" * 32, "后续内容。"]

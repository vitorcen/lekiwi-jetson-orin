import numpy as np
import pytest

from voice_audio import resample_pcm16


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

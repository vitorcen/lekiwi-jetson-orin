#!/usr/bin/env python3
"""Pure ASR-observability helpers: per-segment outcome classification, a since-boot
counter, dBFS conversion and the self-test similarity metric.

Every silent ASR failure the operator complains about ("经常没识别到") is one of a
handful of outcomes decided here; the daemon feeds these functions the numpy-measured
rms/peak and the sherpa decode text, then emits/counts the result. Stdlib-only and
side-effect free so the classification is unit-tested off the board.
"""

from __future__ import annotations

import difflib
import math
import re

# Single-char fillers that must not reach the LLM (mirrors daemon._FILLER).
FILLERS = {"嗯", "啊", "哦", "呃", "唉", "呀", "哎", "嗯嗯"}

# Outcome enum for one VAD-cut segment.
ACCEPTED = "accepted"      # text passed all filters -> brain / transcription station
EMPTY_ASR = "empty_asr"    # VAD cut a segment but ASR decoded nothing
FILLER = "filler"          # a bare filler word, dropped before the LLM
TOO_SHORT = "too_short"    # shorter than min_chars, dropped before the LLM
GATE = "gate"              # energy/length gate dropped it BEFORE ASR (barge path)


def classify_segment(text, *, min_chars=2, fillers=FILLERS):
    """Outcome of a segment AFTER an ASR decode. 'gate' is decided pre-ASR by the
    caller, so it is never returned here. Order matters: a 1-char filler ('嗯') must
    read as FILLER, not TOO_SHORT."""
    stripped = re.sub(r"\s+", "", text or "")
    if not stripped:
        return EMPTY_ASR
    if stripped in fillers:
        return FILLER
    if len(stripped) < min_chars:
        return TOO_SHORT
    return ACCEPTED


def dbfs(amp):
    """Linear amplitude (0..1) -> dBFS, floored so silence maps to a finite number."""
    return round(20.0 * math.log10(max(float(amp), 1e-9)), 1)


class AsrStats:
    """Since-boot segment counters surfaced on /health.asr_stats. One record() per
    VAD segment, whatever its outcome — so the operator can see that VAD IS cutting
    segments but they all decode empty, etc."""

    def __init__(self):
        self.d = {"segments": 0, ACCEPTED: 0, EMPTY_ASR: 0,
                  FILLER: 0, TOO_SHORT: 0, GATE: 0}

    def record(self, outcome):
        self.d["segments"] += 1
        if outcome in self.d:
            self.d[outcome] += 1
        return outcome

    def snapshot(self):
        return dict(self.d)


def similarity(a, b):
    """difflib ratio on whitespace-stripped strings — the self-test pass metric."""
    na = re.sub(r"\s+", "", a or "")
    nb = re.sub(r"\s+", "", b or "")
    if not na and not nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def selftest_pass(asr_text, expected, threshold=0.5):
    """Self-test passes when the recognized text is ≥threshold similar to expected.
    Bisects 'acoustic problem' vs 'model problem': a clean synthesized human voice
    fed straight through VAD+ASR must decode, no microphone involved."""
    return similarity(asr_text, expected) >= threshold

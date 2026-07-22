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


# --------------------------------------------------------------------------- #
# barge-in echo discrimination (pure text logic, unit-testable off-board)
# --------------------------------------------------------------------------- #
_PUNCT_RE = re.compile(r"[\s,。!?、;:·~——…‘’“”\"'!?,.;:()()\-]+")


def norm_text(t):
    """Strip whitespace + zh/en punctuation, leaving a comparable char sequence."""
    return _PUNCT_RE.sub("", t or "")


def is_echo(text, recent, now, *, window_s=20.0, sim=0.55, cover=0.70):
    """Is `text` (ASR of a mic segment heard while SPEAKING) an echo of our own
    playback? `recent` = [(ts, sentence)] in play order.

    Two layers: per-sentence containment/similarity, then a cross-sentence
    fallback — a VAD segment often straddles two played sentences (tail of one
    + head of the next), matching NO single sentence above `sim`, which used to
    leak the echo back in as a fake user turn. The fallback measures how much of
    the candidate is covered by matching blocks against the concatenation of
    recent sentences; only for candidates ≥4 chars so short real commands
    (停/等等/别说了) can never be swallowed by scattered one-char matches."""
    cand = norm_text(text)
    if not cand:
        return True
    refs = []
    for ts, sent in recent:
        if now - ts > window_s:
            continue
        ref = norm_text(sent)
        if not ref:
            continue
        refs.append(ref)
        if cand in ref or ref in cand:
            return True
        if difflib.SequenceMatcher(None, cand, ref).ratio() >= sim:
            return True
    concat = "".join(refs)
    if len(cand) >= 4 and concat:
        m = difflib.SequenceMatcher(None, cand, concat)
        if sum(b.size for b in m.get_matching_blocks()) / len(cand) >= cover:
            return True
    return False

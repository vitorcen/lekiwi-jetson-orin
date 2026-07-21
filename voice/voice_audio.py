"""Audio-side pure helpers, extracted from daemon.py (pure movement, no behavior
change): the LLM-delta -> TTS sentence accumulator, the VAD-segment replay ring,
and wav reading. Nothing here touches daemon state."""

from __future__ import annotations

import base64
import os
import re
import wave

import numpy as np

# --------------------------------------------------------------------------- #
# 句子累积器:LLM token 流 → 适合 TTS 的短句
# --------------------------------------------------------------------------- #
_HARD_BOUND = set("。!?;\n！?；")
_SOFT_BOUND = set(",、,")
_MD_STRIP = re.compile(r"[*_`#>|~\[\]()]")     # markdown 记号
_URL_RE = re.compile(r"https?://\S+")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⌀-⏿]"
)
_CODEFENCE_RE = re.compile(r"```")


class SentenceAccumulator:
    """把 LLM delta 流累积成 8-40 字的短句。首段抢首音低延迟(≥8 字即可提交,
    最长 18 字强制),后续段目标 20-40 字。剥掉 markdown/URL/emoji/代码围栏。"""

    def __init__(self) -> None:
        self.buf = ""
        self.first_done = False

    @staticmethod
    def _clean(text: str) -> str:
        text = _CODEFENCE_RE.sub(" ", text)    # 代码围栏记号剥掉
        text = _URL_RE.sub(" ", text)
        text = _MD_STRIP.sub("", text)
        text = _EMOJI_RE.sub("", text)
        return text

    def _absorb(self, cut: int) -> int:
        """切点向后吞掉紧跟的标点/收尾引号,避免"。"孤儿到下一段开头。"""
        b = self.buf
        n = len(b)
        while cut < n and (b[cut] in _HARD_BOUND or b[cut] in _SOFT_BOUND
                           or b[cut] in "”』」)】…"):
            cut += 1
        return cut

    def _cut_at(self) -> int:
        """返回可切分位置(字符 index,含);无则 -1。标点边界绝对优先,
        强切只是最后手段且先回头找最近的标点。"""
        b = self.buf
        n = len(b)
        first = not self.first_done
        hard_min = 2 if first else 12      # 首段"好的。"这种天然短句直接放行,抢首音
        soft_min = 8 if first else 16
        force = 24 if first else 42        # 放宽强切,给正在路上的标点留时间
        # 硬边界:立即切
        for i, ch in enumerate(b):
            if ch in _HARD_BOUND and i + 1 >= hard_min:
                return self._absorb(i + 1)
        # 软边界(逗号类):够长才切
        for i, ch in enumerate(b):
            if ch in _SOFT_BOUND and i + 1 >= soft_min:
                return self._absorb(i + 1)
        # 超长强切:先从 force 往回找任意标点(≥4 字),实在没有才按词边界斩
        if n >= force:
            for j in range(min(n, force), 4, -1):
                if b[j - 1] in _HARD_BOUND or b[j - 1] in _SOFT_BOUND:
                    return self._absorb(j)
            j = min(n, force)
            while j > 4 and b[j - 1].isascii() and b[j - 1].isalpha():
                j -= 1   # 别拆断英文单词
            return j if j > 4 else min(n, force)
        return -1

    def push(self, delta: str):
        """吞入 delta,产出零或多个可提交短句(生成器)。"""
        self.buf += self._clean(delta)
        # 上一段刚在强切点吐出后,迟到 delta 里领头的标点没了归属——直接丢弃,
        # 否则会被当成下一段的开头念出停顿。
        if self.first_done:
            self.buf = self.buf.lstrip("。!?;！?；,、, \n")
        while True:
            cut = self._cut_at()
            if cut <= 0:
                break
            sent = self.buf[:cut].strip()
            self.buf = self.buf[cut:]
            if sent:
                self.first_done = True
                yield sent

    def flush(self):
        """冲刷残余(整轮结束时调用)。"""
        sent = self.buf.strip()
        self.buf = ""
        if sent:
            self.first_done = True
            yield sent


# --------------------------------------------------------------------------- #
# 段音频回放:最近 N 段原始 PCM 存 tmp(16k mono wav,环形覆盖)
# --------------------------------------------------------------------------- #
class SegStore:
    """Ring of the last N VAD segments as 16k mono wav under a tmp dir. Cleared on
    daemon start; seg_id is monotonic and path()/read_b64() return None once evicted.
    tmp-only — segment audio never enters the repo."""

    def __init__(self, directory: str, keep: int = 10) -> None:
        self.dir = directory
        self.keep = max(1, keep)
        self.seq = 0
        try:
            os.makedirs(self.dir, exist_ok=True)
            for f in os.listdir(self.dir):
                if f.startswith("seg-") and f.endswith(".wav"):
                    try:
                        os.unlink(os.path.join(self.dir, f))
                    except OSError:
                        pass
        except OSError:
            pass

    def _path(self, seg_id: int) -> str:
        return os.path.join(self.dir, f"seg-{seg_id}.wav")

    def save(self, samples: np.ndarray) -> int:
        """samples: float32 [-1,1] @16k → wav. Returns seg_id (0 on failure)."""
        self.seq += 1
        sid = self.seq
        try:
            pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
            with wave.open(self._path(sid), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(pcm.tobytes())
            old = sid - self.keep
            if old > 0:
                try:
                    os.unlink(self._path(old))
                except OSError:
                    pass
        except (OSError, ValueError):
            return 0
        return sid

    def path(self, seg_id: int) -> str | None:
        """段 wav 的绝对路径,已被环形覆盖/不存在 → None。"""
        p = self._path(seg_id)
        if seg_id <= 0 or not os.path.exists(p):
            return None
        return p

    def read_b64(self, seg_id: int) -> str | None:
        p = self.path(seg_id)
        if p is None:
            return None
        try:
            with open(p, "rb") as fh:
                return base64.b64encode(fh.read()).decode("ascii")
        except OSError:
            return None


def read_wav_16k(path: str) -> np.ndarray:
    """Read a mono 16k s16le wav → float32 [-1,1]. Best-effort: non-16k is accepted
    as-is (the self-test wav is generated at 16k, so this is only a guard rail)."""
    with wave.open(path, "rb") as w:
        frames = w.readframes(w.getnframes())
        ch = w.getnchannels()
    arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    return arr

#!/usr/bin/env python3
"""LeKiwi 语音前端 daemon —— 常开麦克风、VAD 截句、SenseVoice ASR、Hermes 流式
问答、edge-tts/Melo 双通道 TTS,半双工播报,HTTP 控制面 :8092。

设计要点(见 voice-daemon-spec.md):
  - 状态机 IDLE → LISTENING → THINKING → SPEAKING → LISTENING,常开窗口 WINDOW_S。
  - 每轮 generation ID 单调递增;所有异步回调(SSE token / edge-tts 音频块 / ASR 结果)
    带 generation,过期即丢弃 —— 这是打断能"立即静音"的根基。
  - 打断模式(barge-in,VOICE_BARGE_IN=0 可关):SPEAKING 不闭麦,VAD 段过
    时长≥0.55s、RMS 能量门、ASR 文本与近期播报句相似度(回声判别)三重门限才算
    真插话——MCP01 硬件 AEC 只有部分抑制,单靠 VAD 会被自己的播报误触发。
    命中停止词(停/别说了…)只打断;其余作为新一轮输入直接起轮。
    THINKING 期间仍闭麦丢弃并 reset VAD;播放结束 250ms 后恢复正常喂 VAD。
  - 所有音频子进程(arecord/aplay/ffmpeg)随状态机回收,绝不留僵尸。
  - ONNX 推理(ASR/Melo)一律丢 ThreadPoolExecutor,不阻塞事件循环。
  - 任一异常路径(edge-tts 超时、Hermes 5xx、音频卡拔掉)都降级/上报 /health,不让
    daemon 崩溃。
  - 风格与 vlm/daemon.py 对齐(token 文件、aiohttp、SSE 端点、监听地址)。
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from aiohttp import web
import aiohttp

# --------------------------------------------------------------------------- #
# Config(全部可用环境变量覆盖;默认值针对本板)
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


HTTP_HOST = _env("VOICE_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(_env("VOICE_HTTP_PORT", "8092"))
TOKEN_FILE = os.path.expanduser(_env("VOICE_TOKEN_FILE", os.path.join(HERE, "token")))

WINDOW_S = float(_env("VOICE_WINDOW_S", "1800"))          # 常开窗口(秒)
HALF_DUPLEX_RESUME = 0.25                                 # 播放结束后恢复喂 VAD 的静默
LEVEL_PEAK_S = 3.0        # 麦克风峰值电平的保持窗口(秒)
INTERRUPT_SETTLE = 0.20                                   # 打断后回 LISTENING 前的沉降

# 语音打断(barge-in):SPEAKING 时不闭麦,VAD 段过三重门限才算真打断。
# MCP01 的硬件 AEC 只有部分抑制(实测 1kHz 探测音仍泄漏),所以单靠能量/VAD
# 会被自己的播报误触发——最后一道门是 ASR 文本与近期播报句的相似度比对:
# 识别出的就是自己正在说的话 → 回声,丢弃;是别的内容 → 用户在插话。
BARGE_IN = _env("VOICE_BARGE_IN", "1").lower() not in ("0", "false", "no")
BARGE_MIN_S = 0.55            # 打断语音最短时长(太短多为回声碎片/杂音)
BARGE_MIN_RMS = 0.020         # 能量门(归一化 RMS,残余回声底噪之上)
BARGE_ECHO_SIM = 0.55         # 与近期播报句相似度 ≥ 此值 → 判回声
BARGE_ECHO_WINDOW_S = 20.0    # 只与最近这段时间的播报句比对
# 停止词:命中只打断不起轮(允许单字"停",绕过最短长度过滤)
STOP_WORDS = {"停", "停停", "停一下", "停下", "别说了", "闭嘴", "安静", "等等", "先停"}

# Hermes API server
HERMES_BASE = _env("VOICE_HERMES_BASE", "http://127.0.0.1:8642").rstrip("/")
HERMES_SESSION = _env("VOICE_HERMES_SESSION", "voice")
HERMES_ENV = os.path.expanduser(
    _env("VOICE_HERMES_ENV", "~/.hermes/profiles/robot/.env")
)
HERMES_TURN_TIMEOUT = float(_env("VOICE_HERMES_TIMEOUT", "60"))

# TTS
EDGE_VOICE = _env("VOICE_EDGE_VOICE", "zh-CN-XiaoxiaoNeural")
EDGE_FIRST_TIMEOUT = 2.5        # 首个音频包超时(实测本板网络首包 ~1.4s,规格 1.0s
                                #  太紧会让 edge 每次都被误判失败退回 Melo → 放宽到 2.5s)
EDGE_TOTAL_TIMEOUT = 6.0        # 整句超时(首包之后)
EDGE_SR = 24000                 # ffmpeg 解码目标采样率
BREAKER_FAILS = 3               # 连续失败触发熔断
BREAKER_COOLDOWN = 300.0        # 熔断后直接走 Melo 的时长
BREAKER_PROBE = 60.0            # 熔断期间后台探测间隔

# 事件反馈环形缓冲(/feed 增量拉取用)
FEED_RING = 200

# 状态常量
IDLE, LISTENING, THINKING, SPEAKING = "idle", "listening", "thinking", "speaking"

START_TS = time.time()

# SenseVoice 标签形如 <|zh|><|HAPPY|><|Speech|>,ASR 正文里剥掉
_TAG_RE = re.compile(r"<\|[^|]*\|>")
# 语气词单字(过短/纯语气 → 不上 LLM)
_FILLER = {"嗯", "啊", "哦", "呃", "唉", "呀", "哎", "嗯嗯"}


def _find_ffmpeg() -> str | None:
    """探测 ffmpeg(PATH 里可能没有 ~/.local/bin)。"""
    cand = os.environ.get("VOICE_FFMPEG")
    if cand and os.path.exists(cand):
        return cand
    for p in ("~/.local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        p = os.path.expanduser(p)
        if os.path.exists(p):
            return p
    return shutil.which("ffmpeg")


FFMPEG = _find_ffmpeg()


def _load_or_make_token() -> str:
    """读 token;缺失则生成 0600 文件(照 vlm 风格,但自动生成而非报错退出)。"""
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_hex(24)
    with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
        fh.write(tok + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    print(f"[voice-daemon] generated token file {TOKEN_FILE}", flush=True)
    return tok


TOKEN = _load_or_make_token()


def _load_hermes_key() -> str | None:
    """从 ~/.hermes/profiles/robot/.env 解析 API_SERVER_KEY。绝不落日志。"""
    try:
        with open(HERMES_ENV, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("API_SERVER_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return val or None
    except OSError:
        return None
    return None


HERMES_KEY = _load_hermes_key()


def _rss_mb() -> float:
    """当前进程 RSS(MB),读 /proc/self/status。"""
    try:
        with open("/proc/self/status", "r") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except OSError:
        pass
    return 0.0


class ProbeFailed(Exception):
    """探测命令本身没跑成(超时/fork 失败)。这**不代表**声卡不在——板子内存吃紧
    换页时,一个自身部分被换出的进程 fork+exec 一个小命令也能拖过超时。把这种
    情况当成"设备缺失"会把好好的音频通路拆掉,曾经就是这么误报的。"""


PROBE_TIMEOUT_S = float(_env("VOICE_PROBE_TIMEOUT_S", "15"))


def _discover_card(which: str) -> str | None:
    """解析 `arecord -l` / `aplay -l`,返回 card 名(含 MCP01 或 USB Audio)。
    which ∈ {"capture","playback"} → arecord/aplay。不写死编号。
    命中不到返回 None(真的没这张卡);跑不动抛 ProbeFailed(信息不足)。"""
    tool = "arecord" if which == "capture" else "aplay"
    try:
        out = subprocess.run(
            [tool, "-l"], capture_output=True, text=True, timeout=PROBE_TIMEOUT_S
        ).stdout
    except Exception as e:                       # noqa: BLE001
        raise ProbeFailed(f"{tool} -l: {e!r}") from e
    # 行形如: card 0: MCP01 [MCP01], device 0: USB Audio [USB Audio]
    best = None
    for m in re.finditer(r"card \d+: (\S+) \[([^\]]*)\], device \d+: ([^\[]*)", out):
        name, desc, dev = m.group(1), m.group(2), m.group(3)
        blob = f"{name} {desc} {dev}"
        if "MCP01" in blob:
            return name           # 精确命中优先
        if "USB Audio" in blob and best is None:
            best = name
    return best


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
# Daemon 核心
# --------------------------------------------------------------------------- #
class Daemon:
    def __init__(self) -> None:
        self.state = IDLE
        self.generation = 0                      # 每轮单调递增;打断即 +1
        self.deadline = 0.0                      # LISTENING 窗口到期时刻
        self.mic_resume_ts = 0.0                 # 半双工:此刻前不喂 VAD
        self.mic_dbfs = -99.0                    # 最近一块 PCM 的 RMS 电平
        self.mic_peak_dbfs = -99.0               # LEVEL_PEAK_S 窗口内的峰值
        self.mic_peak_ts = 0.0

        # 音频设备(按名发现,失败置 None → /health audio:missing)
        self.cap_card: str | None = None
        self.play_card: str | None = None
        self.audio_ok = False

        # 子进程句柄
        self._arecord: asyncio.subprocess.Process | None = None
        self._cap_task: asyncio.Task | None = None
        self._cur_ffmpeg: asyncio.subprocess.Process | None = None
        self._cur_aplay: asyncio.subprocess.Process | None = None

        # 轮次/朗读任务
        self.turn_task: asyncio.Task | None = None
        self.say_task: asyncio.Task | None = None

        # barge-in:近期播报句(回声比对参照)+ 在途检测任务
        self._recent_tts: deque[tuple[float, str]] = deque(maxlen=8)
        self._barge_task: asyncio.Task | None = None

        # 音频缺失去重 + 恢复提示配对
        self._audio_err_ts = 0.0
        self._audio_was_broken = False
        self._probe_warn_ts = 0.0

        # 模型(启动时加载)
        self.rec = None                          # SenseVoice OfflineRecognizer
        self.vad = None                          # Silero VAD
        self.tts = None                          # Melo OfflineTts
        self.asr_loaded = False
        self.tts_local_loaded = False
        self._asr_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")
        self._tts_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="melo")

        # edge-tts 熔断
        self.edge_fail_streak = 0
        self.breaker_until = 0.0                 # >now 表示熔断中(直接走 Melo)

        # 事件源:SSE 订阅者 + /feed 环形缓冲
        self.sse_subscribers: set[asyncio.Queue] = set()
        self.feed_ring: deque[dict] = deque(maxlen=FEED_RING)
        self.feed_seq = 0

        self.last_error: dict | None = None

    # -- 事件发布(SSE + /feed 环形缓冲同源) ---------------------------- #
    def emit(self, etype: str, **fields) -> None:
        self.feed_seq += 1
        ev = {"type": etype, "seq": self.feed_seq, "ts": round(time.time(), 3)}
        ev.update(fields)
        self.feed_ring.append(ev)
        if etype == "error":
            self.last_error = ev
        for q in list(self.sse_subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def set_state(self, s: str) -> None:
        if s != self.state:
            self.state = s
            self.emit("state", state=s)

    def refresh_deadline(self, window_s: float | None = None) -> None:
        w = WINDOW_S if window_s is None else window_s
        self.deadline = time.time() + w

    # -- 子进程回收工具 -------------------------------------------------- #
    @staticmethod
    def _kill(p: asyncio.subprocess.Process | None) -> None:
        if p is not None and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass

    async def _reap(self, p: asyncio.subprocess.Process | None) -> None:
        if p is not None:
            try:
                await asyncio.wait_for(p.wait(), timeout=1.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass

    # ------------------------------------------------------------------ #
    # 音频设备发现
    # ------------------------------------------------------------------ #
    def _warn_probe(self, e: Exception) -> None:
        """探测跑不动时的提示,60s 至多一条。故意不是 error:音频还在用,这只是
        "板子忙得连 fork 都慢了"的信号——多半是内存吃紧在换页。"""
        if time.time() - self._probe_warn_ts < 60.0:
            return
        self._probe_warn_ts = time.time()
        print(f"[voice-daemon] audio probe slow/failed, keeping current cards: {e}",
              flush=True)

    async def discover_audio(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            cap = await loop.run_in_executor(None, _discover_card, "capture")
            play = await loop.run_in_executor(None, _discover_card, "playback")
        except ProbeFailed as e:
            # 探测跑不动 ≠ 声卡没了。已经握着可用卡就原样留着,别把好通路拆了;
            # watch loop 5s 后自然再试一次。从没发现过卡则维持缺失态。
            if self.audio_ok:
                self._warn_probe(e)
                return
            cap = play = None
        else:
            self._probe_warn_ts = 0.0
        self.cap_card, self.play_card = cap, play
        self.audio_ok = bool(self.cap_card and self.play_card)
        if not self.audio_ok:
            # 缺失期去重:capture 重启每 0.3s 会走到这里,10s 至多报一次
            if time.time() - self._audio_err_ts >= 10.0:
                self._audio_err_ts = time.time()
                self.emit("error", message="audio device missing")
            self._audio_was_broken = True
        elif self.cap_card:
            if self._audio_was_broken:
                # 对称提示:之前报过缺失,恢复也要让用户看见;同时清掉挂着的旧错
                self._audio_was_broken = False
                self._audio_err_ts = 0.0
                self.last_error = None
                self.emit("audio", status="recovered", card=self.cap_card,
                          message=f"音频设备已恢复({self.cap_card})")
            # 声卡上电默认音量只有 29%(-20dB):播报听不见、麦克风能量不够触发
            # VAD。ALSA 混音器设置不随重启保留(alsactl store 要 root),所以每次
            # 发现设备后由 daemon 自己拉到位。控件名对 MCP01 实测:PCM=播放,Mic=采集。
            for args in (["sset", "PCM,0", "90%", "unmute"],
                         ["sset", "PCM,1", "90%"],
                         ["sset", "Mic", "85%", "cap"]):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "amixer", "-c", self.cap_card, *args,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL)
                    await proc.wait()
                except OSError:
                    pass

    async def audio_watch_loop(self) -> None:
        """音频卡丢失/卡名失一致时重试发现(拔掉声卡不让 daemon 崩)。
        故障期 5s 快速重试,正常期 30s 巡检。"""
        while True:
            broken = not self.audio_ok or not (self.cap_card and self.play_card)
            if broken:
                await self.discover_audio()
                broken = not self.audio_ok
            await asyncio.sleep(5 if broken else 30)

    def _playback_dead(self, rc) -> None:
        """aplay 非零退出 = 播放设备已失效(拔插/卡名变了):置 audio_ok=False
        让 watch loop 5s 内重新发现,并向 feed 上报,不再无声装成功。"""
        self.audio_ok = False
        self.emit("error", message=f"aplay exited rc={rc}: playback device lost, rediscovering")

    def cap_dev(self) -> str:
        return f"plughw:CARD={self.cap_card}"

    def play_dev(self) -> str:
        return f"plughw:CARD={self.play_card}"

    # ------------------------------------------------------------------ #
    # 采集:arecord 常驻(state != IDLE 期间),320ms 块喂 VAD
    # ------------------------------------------------------------------ #
    async def start_capture(self) -> None:
        if self._cap_task and not self._cap_task.done():
            return
        if not self.audio_ok:
            await self.discover_audio()
            if not self.audio_ok:
                return
        self._cap_task = asyncio.create_task(self._capture_loop())

    async def stop_capture(self) -> None:
        t = self._cap_task
        self._cap_task = None
        self._kill(self._arecord)
        await self._reap(self._arecord)
        self._arecord = None
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def _capture_loop(self) -> None:
        """常驻读 arecord raw s16le@16k,320ms 一块。LISTENING 且过了半双工静默才
        喂 VAD;其余状态丢弃并保持 VAD 干净。arecord 死了限速重启。"""
        chunk_bytes = int(0.32 * 16000) * 2   # 320ms * 16k * int16
        backoff = 0.5
        while self.state != IDLE and self._cap_task is asyncio.current_task():
            cmd = [
                "arecord", "-D", self.cap_dev(),
                "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw", "-q",
            ]
            try:
                self._arecord = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except Exception as exc:
                self.emit("error", message=f"arecord spawn failed: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8.0)
                continue
            backoff = 0.5
            try:
                while self.state != IDLE and self._cap_task is asyncio.current_task():
                    try:
                        data = await self._arecord.stdout.readexactly(chunk_bytes)
                    except asyncio.IncompleteReadError as e:
                        data = e.partial
                        if not data:
                            break        # arecord 退出 → 外层重启
                    if not data:
                        break
                    self._handle_chunk(data)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.emit("error", message=f"capture read error: {exc}")
            finally:
                self._kill(self._arecord)
                await self._reap(self._arecord)
                self._arecord = None
            if self.state == IDLE:
                break
            # arecord 死了(多为设备拔插)→ 完整重发现:同时刷新 cap/play 卡名
            # 和音量。绝不能只翻 audio_ok 不更新卡名——那会留下 audio_ok=True 但
            # cap/play_card=None 的死角:watch loop 被短路,播放全落到
            # plughw:CARD=None 上,无声且不报错(2026-07-19 实锅)。
            await self.discover_audio()
            await asyncio.sleep(0.3)                            # 限速重启

    def _note_level(self, pcm) -> None:
        """本块 RMS -> dBFS,并保留最近 LEVEL_PEAK_S 秒的最大值。峰值才是有用的
        那个数:说话是断续的,平均会被静音段拉平。"""
        if pcm.size == 0:
            return
        x = pcm.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x)))
        self.mic_dbfs = float(20.0 * np.log10(max(rms, 1e-9)))
        now = time.time()
        if self.mic_dbfs >= self.mic_peak_dbfs or now - self.mic_peak_ts > LEVEL_PEAK_S:
            self.mic_peak_dbfs = self.mic_dbfs
            self.mic_peak_ts = now

    def _handle_chunk(self, data: bytes) -> None:
        """一块 PCM。LISTENING 走正常截句;SPEAKING 开麦做打断检测;其余丢弃。"""
        # 电平遥测:在闸门之前算,丢弃期也照报——"麦克风到底听没听见我"是排查
        # ASR 不出字的第一个问题,而这台 MCP01 带硬件降噪门(静时输出近似静音,
        # ALSA 增益调了没用),光看波形本底判断不了,必须有说话时的实测值。
        self._note_level(np.frombuffer(data, dtype=np.int16))
        listening = (self.state == LISTENING and time.time() >= self.mic_resume_ts)
        barge = (BARGE_IN and self.state == SPEAKING)
        if not (listening or barge):
            # 丢弃期间保持 VAD 干净,避免把机器人自己的话截成段
            if self.vad is not None:
                try:
                    self.vad.reset()
                except Exception:
                    pass
            return
        if self.vad is None:
            return
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            self.vad.accept_waveform(samples)
        except Exception:
            return
        while not self.vad.empty():
            seg = self.vad.front.samples
            self.vad.pop()
            seg_arr = np.array(seg, dtype=np.float32)
            # 只在 LISTENING 起轮;拿到段立刻改 THINKING 停止再截句(单轮保证)
            if self.state == LISTENING and (self.turn_task is None or self.turn_task.done()):
                self.set_state(THINKING)
                gen = self.generation
                self.turn_task = asyncio.create_task(self._asr_then_turn(gen, seg_arr))
            elif (self.state == SPEAKING and barge
                    and (self._barge_task is None or self._barge_task.done())):
                self._barge_task = asyncio.create_task(
                    self._barge_check(self.generation, seg_arr))

    # ------------------------------------------------------------------ #
    # ASR → 校验 → 起轮
    # ------------------------------------------------------------------ #
    def _asr_sync(self, samples: np.ndarray) -> str:
        stream = self.rec.create_stream()
        stream.accept_waveform(16000, samples)
        self.rec.decode_stream(stream)
        text = stream.result.text or ""
        return _TAG_RE.sub("", text).strip()

    async def _asr_then_turn(self, gen: int, samples: np.ndarray) -> None:
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(self._asr_pool, self._asr_sync, samples)
        except Exception as exc:
            self.emit("error", message=f"asr failed: {exc}")
            if gen == self.generation:
                self.set_state(LISTENING)
                self.refresh_deadline()
            return
        if gen != self.generation:
            return
        # 过短 / 纯语气词 → 丢弃不上 LLM
        stripped = re.sub(r"\s+", "", text)
        if len(stripped) < 2 or stripped in _FILLER:
            self.set_state(LISTENING)
            self.refresh_deadline()
            return
        self.emit("user_text", text=text)
        await self.run_turn(gen, text)

    # ------------------------------------------------------------------ #
    # barge-in:SPEAKING 中检出的语音段 → 能量门 → ASR → 回声/停止词判别
    # ------------------------------------------------------------------ #
    @staticmethod
    def _norm_text(t: str) -> str:
        """去空白与中英标点,留下可比对的字符序列。"""
        return re.sub(r"[\s,。!?、;:·~——…‘’“”\"'!?,.;:()()\-]+", "", t)

    def _is_echo(self, text: str) -> bool:
        """识别文本与近期播报句相似 → 判为自身回声(AEC 残余)。"""
        cand = self._norm_text(text)
        if not cand:
            return True
        now = time.time()
        for ts, sent in list(self._recent_tts):
            if now - ts > BARGE_ECHO_WINDOW_S:
                continue
            ref = self._norm_text(sent)
            if not ref:
                continue
            if cand in ref or ref in cand:
                return True
            if difflib.SequenceMatcher(None, cand, ref).ratio() >= BARGE_ECHO_SIM:
                return True
        return False

    async def _barge_check(self, gen: int, samples: np.ndarray) -> None:
        if len(samples) < int(BARGE_MIN_S * 16000):
            return
        rms = float(np.sqrt(np.mean(samples * samples)))
        if rms < BARGE_MIN_RMS:
            return
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(self._asr_pool, self._asr_sync, samples)
        except Exception:
            return
        if gen != self.generation or self.state != SPEAKING:
            return                        # 这轮播报已结束/已被别的路径打断
        nt = self._norm_text(text)
        if not nt or self._is_echo(text):
            return
        if nt in STOP_WORDS:
            self.emit("barge_in", text=text, action="stop")
            await self.do_interrupt()
            return
        if len(nt) < 2 or nt in _FILLER:
            return
        # 真插话:打断当前播报,把这段话直接作为新一轮输入(不用重说)
        self.emit("barge_in", text=text, action="turn")
        await self._abort_playback()
        await asyncio.sleep(INTERRUPT_SETTLE)
        if self.vad is not None:
            try:
                self.vad.reset()
            except Exception:
                pass
        self.refresh_deadline()
        self.emit("user_text", text=text)
        self.turn_task = asyncio.create_task(self.run_turn(self.generation, text))

    # ------------------------------------------------------------------ #
    # 一轮:Hermes 流式 → 句子累积器 → TTS 队列(pipeline + 背压)
    # ------------------------------------------------------------------ #
    async def run_turn(self, gen: int, text: str) -> None:
        if gen != self.generation:
            return
        self.set_state(THINKING)
        # 待播队列上限 2 句(满则 Hermes 消费挂起 → 天然背压)
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        speak = asyncio.create_task(self._speak_worker(gen, q))
        try:
            await asyncio.wait_for(
                self._hermes_stream(gen, text, q), timeout=HERMES_TURN_TIMEOUT
            )
        except asyncio.TimeoutError:
            self.emit("error", message="hermes turn timeout")
            await self._play_local_phrase(gen, "抱歉,我这边超时了")
        except asyncio.CancelledError:
            speak.cancel()
            raise
        except Exception as exc:
            self.emit("error", message=f"hermes error: {exc}")
            await self._play_local_phrase(gen, "抱歉,我这边出错了")
        finally:
            try:
                await q.put((gen, None))        # 结束哨兵
            except Exception:
                pass
        try:
            await speak
        except asyncio.CancelledError:
            pass
        if gen == self.generation:
            self.set_state(LISTENING)
            self.refresh_deadline()

    async def _hermes_stream(self, gen: int, text: str, q: asyncio.Queue) -> None:
        """POST /chat/stream,解析 SSE。assistant.delta→累积器;tool.started→转发;
        assistant.completed→冲刷;error→本地报错短语。"""
        acc = SentenceAccumulator()
        headers = {"Accept": "text/event-stream"}
        if HERMES_KEY:
            headers["Authorization"] = f"Bearer {HERMES_KEY}"
        url = f"{HERMES_BASE}/api/sessions/{HERMES_SESSION}/chat/stream"
        timeout = aiohttp.ClientTimeout(total=None, sock_read=HERMES_TURN_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, json={"message": text}, headers=headers) as resp:
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    self.emit("error", message=f"hermes HTTP {resp.status}")
                    await self._play_local_phrase(gen, "抱歉,我暂时连不上大脑")
                    return
                async for evt, data in self._iter_sse(resp):
                    if gen != self.generation:
                        return
                    # 只有 assistant.delta 是要朗读的增量 token。注意 tool.progress
                    # (尤其 _thinking)也带 delta 字段但那是完整推理文本,绝不能喂累积器
                    # (否则整段答案会被重复朗读一遍)。
                    if evt == "assistant.delta":
                        delta = data.get("delta", "")
                        if delta:
                            self.emit("assistant_delta", delta=delta)
                            for sent in acc.push(delta):
                                await q.put((gen, sent))
                    elif evt == "tool.started":
                        tn = data.get("tool_name", "?")
                        if not tn.startswith("_"):          # 跳过 _thinking 之类内部工具
                            self.emit("tool", tool_name=tn)
                    elif evt == "assistant.completed":
                        # 冲刷累积器残余(deltas 已覆盖全文,这里只补尾巴)
                        for sent in acc.flush():
                            await q.put((gen, sent))
                    elif evt == "error":
                        self.emit("error", message=data.get("message", "hermes error"))
                        await self._play_local_phrase(gen, "抱歉,我这边出错了")
                        return
                    elif evt in ("done", "run.completed"):
                        break
                for sent in acc.flush():
                    await q.put((gen, sent))

    @staticmethod
    async def _iter_sse(resp):
        """极简 SSE 解析:产出 (event, data_dict)。data 非 JSON 时包成 {'raw':...}。"""
        event = "message"
        async for raw in resp.content:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                event = "message"
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                payload = line[5:].strip()
                if payload == "[DONE]":
                    yield "done", {}
                    continue
                try:
                    data = json.loads(payload)
                    if not isinstance(data, dict):
                        data = {"raw": data}
                except json.JSONDecodeError:
                    data = {"raw": payload}
                # event 名有时在 data.type 里(assistant.delta 等)
                etype = data.get("type", event)
                yield etype, data

    # ------------------------------------------------------------------ #
    # 朗读工作者:从队列取句,逐句合成+播放(SPEAKING)
    # ------------------------------------------------------------------ #
    async def _speak_worker(self, gen: int, q: asyncio.Queue) -> None:
        try:
            while True:
                g, sent = await q.get()
                if g != self.generation or gen != self.generation:
                    continue
                if sent is None:
                    return                          # 本轮结束哨兵
                if self.state in (THINKING, LISTENING):
                    self.set_state(SPEAKING)
                await self._synth_and_play(gen, sent)
        except asyncio.CancelledError:
            return

    async def _play_local_phrase(self, gen: int, phrase: str) -> None:
        """错误/超时时,用本地 Melo 播固定短语(不走 edge)。"""
        if gen != self.generation:
            return
        if self.state in (THINKING, LISTENING):
            self.set_state(SPEAKING)
        self._recent_tts.append((time.time(), phrase))     # barge-in 回声参照
        await self._melo_play(gen, phrase)
        self.emit("tts", sentence=phrase, backend="melo")

    # ------------------------------------------------------------------ #
    # TTS 双通道:edge 主 / Melo 兜底 + 熔断
    # ------------------------------------------------------------------ #
    async def _synth_and_play(self, gen: int, sentence: str) -> None:
        self._recent_tts.append((time.time(), sentence))   # barge-in 回声参照
        if not self.audio_ok:
            await self.discover_audio()     # 设备刚回来时当句即恢复,不等巡检
        backend = None
        breaker_open = time.time() < self.breaker_until
        if not breaker_open and FFMPEG:
            ok = await self._edge_play(gen, sentence)
            if ok:
                backend = "edge"
                self.edge_fail_streak = 0
            else:
                self.edge_fail_streak += 1
                if self.edge_fail_streak >= BREAKER_FAILS:
                    self.breaker_until = time.time() + BREAKER_COOLDOWN
                    self.edge_fail_streak = 0
        if backend is None and gen == self.generation:
            ok = await self._melo_play(gen, sentence)
            backend = "melo" if ok else "failed"
        if gen == self.generation:
            self.emit("tts", sentence=sentence, backend=backend)

    def _after_playback(self) -> None:
        """播放结束:清子进程句柄 + 设半双工恢复时刻。"""
        self._cur_ffmpeg = None
        self._cur_aplay = None
        self.mic_resume_ts = time.time() + HALF_DUPLEX_RESUME
        if self.vad is not None:
            try:
                self.vad.reset()
            except Exception:
                pass

    async def _edge_play(self, gen: int, text: str) -> bool:
        """edge-tts 流式 mp3 → ffmpeg 解码 s16le@24k → aplay。首包 1s、整句 5s 超时。"""
        import edge_tts
        if not (self.audio_ok and self.play_card):
            return False
        ff = ap = None
        pump = None
        try:
            ff = await asyncio.create_subprocess_exec(
                FFMPEG, "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0", "-f", "s16le", "-ar", str(EDGE_SR), "-ac", "1", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            ap = await asyncio.create_subprocess_exec(
                "aplay", "-D", self.play_dev(),
                "-f", "S16_LE", "-r", str(EDGE_SR), "-c", "1", "-t", "raw", "-q",
                stdin=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._cur_ffmpeg, self._cur_aplay = ff, ap

            async def _pump():
                # ffmpeg 解码输出泵到 aplay
                while True:
                    buf = await ff.stdout.read(4096)
                    if not buf:
                        break
                    ap.stdin.write(buf)
                    await ap.stdin.drain()
                try:
                    ap.stdin.close()
                except Exception:
                    pass
            pump = asyncio.create_task(_pump())

            comm = edge_tts.Communicate(text, EDGE_VOICE)
            it = comm.stream().__aiter__()
            got_audio = False
            first = True
            start = time.time()
            while True:
                if gen != self.generation:
                    raise asyncio.CancelledError()
                try:
                    if first:
                        chunk = await asyncio.wait_for(it.__anext__(), EDGE_FIRST_TIMEOUT)
                    else:
                        remain = EDGE_TOTAL_TIMEOUT - (time.time() - start)
                        chunk = await asyncio.wait_for(it.__anext__(), max(0.1, remain))
                except StopAsyncIteration:
                    break
                first = False
                if chunk.get("type") == "audio" and chunk.get("data"):
                    got_audio = True
                    ff.stdin.write(chunk["data"])
                    await ff.stdin.drain()
            try:
                ff.stdin.close()
            except Exception:
                pass
            await pump
            await asyncio.wait_for(ap.wait(), timeout=8.0)
            if ap.returncode != 0:
                if gen == self.generation:      # 被打断 kill 的非零码不算设备故障
                    self._playback_dead(ap.returncode)
                return False
            return got_audio
        except Exception:
            # 超时/断管/gen 过期 → 失败,交给 Melo 兜底
            if pump:
                pump.cancel()
            self._kill(ff)
            self._kill(ap)
            return False
        finally:
            await self._reap(ff)
            await self._reap(ap)
            self._after_playback()

    def _melo_sync(self, text: str) -> np.ndarray:
        audio = self.tts.generate(text, sid=0, speed=1.0)
        samp = np.asarray(audio.samples, dtype=np.float32)
        pcm = np.clip(samp, -1.0, 1.0)
        return (pcm * 32767.0).astype(np.int16)

    async def _melo_play(self, gen: int, text: str) -> bool:
        """本地 Melo:float32@44100 → int16 → aplay -r 44100。合成丢 executor。"""
        if self.tts is None or not self.audio_ok:
            return False
        loop = asyncio.get_running_loop()
        ap = None
        try:
            pcm16 = await loop.run_in_executor(self._tts_pool, self._melo_sync, text)
            if gen != self.generation:
                return False
            ap = await asyncio.create_subprocess_exec(
                "aplay", "-D", self.play_dev(),
                "-f", "S16_LE", "-r", "44100", "-c", "1", "-t", "raw", "-q",
                stdin=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._cur_aplay = ap
            ap.stdin.write(pcm16.tobytes())
            try:
                ap.stdin.close()
            except Exception:
                pass
            rc = await asyncio.wait_for(ap.wait(), timeout=30.0)
            if rc != 0:
                if gen == self.generation:      # 被打断 kill 的非零码不算设备故障
                    self._playback_dead(rc)
                return False
            return True
        except Exception as exc:
            self._kill(ap)
            self.emit("error", message=f"melo play failed: {exc}")
            return False
        finally:
            await self._reap(ap)
            self._after_playback()

    async def breaker_probe_loop(self) -> None:
        """熔断期间每 60s 探测 edge-tts 是否恢复(只合成不播放)。"""
        while True:
            await asyncio.sleep(BREAKER_PROBE)
            if time.time() >= self.breaker_until:
                continue
            try:
                import edge_tts
                comm = edge_tts.Communicate("好", EDGE_VOICE)
                async for chunk in comm.stream():
                    if chunk.get("type") == "audio" and chunk.get("data"):
                        self.breaker_until = 0.0        # 恢复
                        self.edge_fail_streak = 0
                        break
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 控制动作:listen / stop / interrupt / say
    # ------------------------------------------------------------------ #
    async def do_listen(self, window_s: float | None) -> None:
        self.refresh_deadline(window_s)
        if self.state == IDLE:
            self.set_state(LISTENING)
        await self.start_capture()
        if self.state == IDLE:                     # capture 起不来(无音频)
            self.set_state(LISTENING)

    async def _abort_playback(self) -> None:
        """原子打断:kill aplay/ffmpeg、cancel 轮次/say、清生成号。"""
        self.generation += 1                       # 令所有在途回调过期
        self._kill(self._cur_ffmpeg)
        self._kill(self._cur_aplay)
        await self._reap(self._cur_ffmpeg)
        await self._reap(self._cur_aplay)
        self._cur_ffmpeg = self._cur_aplay = None
        for t in (self.turn_task, self.say_task):
            if t and not t.done():
                t.cancel()
        self.turn_task = self.say_task = None

    async def do_interrupt(self) -> None:
        """播报中按键:立停 → 沉降 200ms → 回 LISTENING(刷新窗口)。"""
        await self._abort_playback()
        await asyncio.sleep(INTERRUPT_SETTLE)
        self.mic_resume_ts = time.time() + HALF_DUPLEX_RESUME
        if self.vad is not None:
            try:
                self.vad.reset()
            except Exception:
                pass
        self.set_state(LISTENING)
        self.refresh_deadline()
        await self.start_capture()

    async def do_stop(self) -> None:
        """任何态强制回 IDLE(先打断)。"""
        await self._abort_playback()
        self.set_state(IDLE)
        await self.stop_capture()

    async def do_say(self, text: str) -> None:
        """调试直接播报,走完整 TTS 通道,可被 interrupt 立停。"""
        gen = self.generation
        prev = self.state
        self.set_state(SPEAKING)
        self.say_task = asyncio.create_task(self._say_run(gen, text, prev))

    async def _say_run(self, gen: int, text: str, prev: str) -> None:
        try:
            acc = SentenceAccumulator()
            sents = list(acc.push(text)) + list(acc.flush())
            if not sents:
                sents = [text]
            for sent in sents:
                if gen != self.generation:
                    return
                await self._synth_and_play(gen, sent)
        except asyncio.CancelledError:
            return
        finally:
            if gen == self.generation:
                # 回落目标看"麦克风窗口现在还开着吗",不是播报前的旧快照:播报
                # 期间按开麦时 do_listen 只刷新 deadline(故意不打断播报),旧
                # 快照会把这次请求丢掉 —— 播完回 IDLE、采集循环随即退出,麦克风
                # 再也不开。这也正是 SPEAKING → LISTENING 的半双工约定。
                live = prev == LISTENING or (self.deadline
                                             and time.time() < self.deadline)
                if live:
                    self.set_state(LISTENING)
                    await self.start_capture()   # 幂等:活着就直接返回
                else:
                    self.set_state(IDLE)

    # ------------------------------------------------------------------ #
    # 窗口到期
    # ------------------------------------------------------------------ #
    async def deadline_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if (self.state == LISTENING and self.deadline
                    and time.time() > self.deadline):
                await self.do_stop()

    # ------------------------------------------------------------------ #
    # 启动时加载模型 + Hermes 会话
    # ------------------------------------------------------------------ #
    async def load_models(self) -> None:
        import sherpa_onnx as so
        loop = asyncio.get_running_loop()

        def _load():
            rec = so.OfflineRecognizer.from_sense_voice(
                model=os.path.join(MODELS, "sense-voice/model.int8.onnx"),
                tokens=os.path.join(MODELS, "sense-voice/tokens.txt"),
                num_threads=4, use_itn=True, language="zh",
            )
            cfg = so.VadModelConfig()
            cfg.silero_vad.model = os.path.join(MODELS, "silero_vad.onnx")
            cfg.silero_vad.threshold = 0.5
            cfg.silero_vad.min_silence_duration = 0.55
            cfg.silero_vad.min_speech_duration = 0.25
            cfg.silero_vad.max_speech_duration = 20
            cfg.sample_rate = 16000
            vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
            tc = so.OfflineTtsConfig()
            base = os.path.join(MODELS, "vits-melo-tts-zh_en")
            tc.model.vits.model = os.path.join(base, "model.onnx")
            tc.model.vits.lexicon = os.path.join(base, "lexicon.txt")
            tc.model.vits.tokens = os.path.join(base, "tokens.txt")
            tc.model.vits.dict_dir = os.path.join(base, "dict")
            tc.model.num_threads = 4
            tc.rule_fsts = ",".join(
                os.path.join(base, f) for f in ("date.fst", "number.fst", "phone.fst")
            )
            tts = so.OfflineTts(tc)
            tts.generate("好", sid=0, speed=1.0)      # 预热
            return rec, vad, tts

        self.rec, self.vad, self.tts = await loop.run_in_executor(None, _load)
        self.asr_loaded = True
        self.tts_local_loaded = True
        print("[voice-daemon] models loaded (ASR+VAD+Melo)", flush=True)

    async def ensure_hermes_session(self) -> None:
        """启动时建 voice 会话;已存在(409/其它)视为成功,GET 确认。"""
        headers = {}
        if HERMES_KEY:
            headers["Authorization"] = f"Bearer {HERMES_KEY}"
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                try:
                    async with sess.post(
                        f"{HERMES_BASE}/api/sessions",
                        json={"id": HERMES_SESSION}, headers=headers,
                    ) as r:
                        _ = await r.text()
                except aiohttp.ClientError:
                    pass
                # GET 确认存在
                async with sess.get(
                    f"{HERMES_BASE}/api/sessions/{HERMES_SESSION}", headers=headers
                ) as r:
                    ok = r.status < 400
            print(f"[voice-daemon] hermes session '{HERMES_SESSION}' ok={ok}", flush=True)
        except Exception as exc:
            print(f"[voice-daemon] hermes session setup failed: {exc}", flush=True)

    def health(self) -> dict:
        return {
            "state": self.state,
            "audio": "ok" if self.audio_ok else "missing",
            "capture_card": self.cap_card,
            "playback_card": self.play_card,
            "asr_loaded": self.asr_loaded,
            "tts_local_loaded": self.tts_local_loaded,
            "edge_breaker": time.time() < self.breaker_until,
            "ffmpeg": bool(FFMPEG),
            "hermes_key": bool(HERMES_KEY),
            "barge_in": BARGE_IN,
            "window_deadline": round(self.deadline, 1) if self.deadline else None,
            "mic_dbfs": round(self.mic_dbfs, 1),
            "mic_peak_dbfs": round(self.mic_peak_dbfs, 1),
            "generation": self.generation,
            "mem_rss_mb": _rss_mb(),
            "last_error": self.last_error,
            "uptime": round(time.time() - START_TS, 1),
        }


DAEMON = Daemon()


# --------------------------------------------------------------------------- #
# HTTP 层(:8092,Bearer)
# --------------------------------------------------------------------------- #
@web.middleware
async def auth_middleware(request: web.Request, handler):
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {TOKEN}"
    if len(auth) != len(expected) or auth != expected:
        return web.json_response(
            {"error": "unauthorized"}, status=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await handler(request)


async def h_health(request: web.Request) -> web.Response:
    return web.json_response(DAEMON.health())


async def h_state(request: web.Request) -> web.Response:
    h = DAEMON.health()
    return web.json_response({
        "state": h["state"], "audio": h["audio"],
        "edge_breaker": h["edge_breaker"], "generation": h["generation"],
        "window_deadline": h["window_deadline"], "mem_rss_mb": h["mem_rss_mb"],
    })


async def _json_body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}


async def h_listen(request: web.Request) -> web.Response:
    body = await _json_body(request)
    window_s = None
    if body.get("window_s") is not None:
        try:
            window_s = float(body["window_s"])
        except (ValueError, TypeError):
            window_s = None
    await DAEMON.do_listen(window_s)
    return web.json_response({"state": DAEMON.state,
                              "window_deadline": round(DAEMON.deadline, 1)})


async def h_stop(request: web.Request) -> web.Response:
    await DAEMON.do_stop()
    return web.json_response({"state": DAEMON.state})


async def h_interrupt(request: web.Request) -> web.Response:
    await DAEMON.do_interrupt()
    return web.json_response({"state": DAEMON.state})


async def h_say(request: web.Request) -> web.Response:
    body = await _json_body(request)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "text required"}, status=400)
    await DAEMON.do_say(text)
    return web.json_response({"ok": True, "state": DAEMON.state})


async def h_simulate(request: web.Request) -> web.Response:
    """调试:把一段文本当作 ASR 定稿直接送 Hermes 走完整一轮(GUI/自测用,
    绕过真实麦克风)。"""
    body = await _json_body(request)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "text required"}, status=400)
    gen = DAEMON.generation
    DAEMON.emit("user_text", text=text)
    DAEMON.turn_task = asyncio.create_task(DAEMON.run_turn(gen, text))
    return web.json_response({"ok": True})


async def h_feed(request: web.Request) -> web.Response:
    """GET /feed?since=<seq> → {events:[...], last_seq:N}。since 缺省=0 返回全部现存。
    GUI 走 Rust 代理无法直连 SSE,以 2-3Hz 轮询增量。"""
    try:
        since = int(request.query.get("since", "0"))
    except ValueError:
        since = 0
    events = [e for e in DAEMON.feed_ring if e["seq"] > since]
    return web.json_response({"events": events, "last_seq": DAEMON.feed_seq})


async def h_events(request: web.Request) -> web.StreamResponse:
    d = DAEMON
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    d.sse_subscribers.add(q)
    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15.0)
                await resp.write(
                    f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8")
                )
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")     # 保活 + 探测断线
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        d.sse_subscribers.discard(q)
    return resp


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/health", h_health)
    app.router.add_get("/state", h_state)
    app.router.add_post("/listen", h_listen)
    app.router.add_post("/stop", h_stop)
    app.router.add_post("/interrupt", h_interrupt)
    app.router.add_post("/say", h_say)
    app.router.add_post("/simulate", h_simulate)
    app.router.add_get("/feed", h_feed)
    app.router.add_get("/events", h_events)
    return app


async def _on_start(app: web.Application) -> None:
    await DAEMON.discover_audio()
    await DAEMON.load_models()
    await DAEMON.ensure_hermes_session()
    app["tasks"] = [
        asyncio.create_task(DAEMON.deadline_loop()),
        asyncio.create_task(DAEMON.audio_watch_loop()),
        asyncio.create_task(DAEMON.breaker_probe_loop()),
    ]


async def _on_cleanup(app: web.Application) -> None:
    await DAEMON._abort_playback()
    await DAEMON.stop_capture()
    for t in app.get("tasks", []):
        t.cancel()


def main() -> None:
    app = make_app()
    app.on_startup.append(_on_start)
    app.on_cleanup.append(_on_cleanup)
    print(
        f"[voice-daemon] listening on {HTTP_HOST}:{HTTP_PORT} "
        f"hermes={HERMES_BASE} session={HERMES_SESSION} "
        f"ffmpeg={FFMPEG} window={WINDOW_S}s",
        flush=True,
    )
    web.run_app(app, host=HTTP_HOST, port=HTTP_PORT, print=None)


if __name__ == "__main__":
    main()

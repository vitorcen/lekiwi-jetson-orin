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
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from aiohttp import web
import aiohttp

import voice_config as vconfig
import voice_switching as vswitch
import voice_engines as vengines
import voice_vad as vvad
import voice_brain as vbrain
import voice_asr_obs as vobs
import voice_http as vhttp
from voice_audio import SentenceAccumulator, SegStore, read_wav_16k

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
HERMES_YAML = os.path.expanduser(
    _env("VOICE_HERMES_YAML", "~/.hermes/profiles/robot/config.yaml")
)
HERMES_UNIT = _env("VOICE_HERMES_UNIT", "hermes-gateway-robot")
HERMES_TURN_TIMEOUT = float(_env("VOICE_HERMES_TIMEOUT", "60"))
# Gateway restart re-spawns the profile's MCP servers (vlm + drive python venvs),
# so readiness can legitimately take >20s on this board — 30s gives margin. This
# is internal to the /brain job (POST returns 202 first), so the Rust 15s cap
# never applies here.
HERMES_READY_TIMEOUT = float(_env("VOICE_HERMES_READY_TIMEOUT", "30"))
HERMES_PROBE_TIMEOUT = float(_env("VOICE_HERMES_PROBE_TIMEOUT", "10"))

# TTS
EDGE_VOICE = _env("VOICE_EDGE_VOICE", "zh-CN-XiaoxiaoNeural")
EDGE_FIRST_TIMEOUT = 2.5        # 首个音频包超时(实测本板网络首包 ~1.4s,规格 1.0s
                                #  太紧会让 edge 每次都被误判失败退回 Melo → 放宽到 2.5s)
EDGE_TOTAL_TIMEOUT = 6.0        # 整句超时(首包之后)
EDGE_SR = 24000                 # ffmpeg 解码目标采样率
BREAKER_FAILS = 3               # 连续失败触发熔断
BREAKER_COOLDOWN = 300.0        # 熔断后直接走 Melo 的时长
BREAKER_PROBE = 60.0            # 熔断期间后台探测间隔

# Vision 播报桥:板端订阅 vlm-daemon caption(:8090,token 与 vlm daemon 同文件)
VLM_BASE = _env("VOICE_VLM_BASE", "http://127.0.0.1:8090").rstrip("/")
VLM_TOKEN_FILE = os.path.expanduser(
    _env("VOICE_VLM_TOKEN_FILE", os.path.join(HERE, "..", "vlm", "token"))
)
# vlm-daemon POST /model is synchronous through a cold model load (90s ready +
# probe); give the proxied call generous margin. POST returns 202 first, so the
# Rust 15s cap never applies (this runs inside the /config vision job).
VLM_MODEL_TIMEOUT = float(_env("VOICE_VLM_MODEL_TIMEOUT", "180"))


def _load_vlm_token() -> str | None:
    try:
        with open(VLM_TOKEN_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


# 事件反馈环形缓冲(/feed 增量拉取用)
FEED_RING = 200

# ASR 段级观测:最近 N 段原始 PCM 存 tmp(16k mono wav,环形覆盖,启动清空),
# 供转写台「▶ 听」回放。只存 /tmp,不入仓库。
SEG_DIR = _env("VOICE_ASR_SEG_DIR", "/tmp/lekiwi_asr_segs")
SEG_KEEP = int(_env("VOICE_ASR_SEG_KEEP", "10"))

# 一键回环自检:已知中文人声测试 wav(edge-tts 合成)直接喂 VAD+ASR 全链,绕过麦克风。
# 把「声学问题」与「模型问题」一键二分(见 .memory/voice-frontend-s2.md 麦克风排查铁律)。
SELFTEST_WAV = os.path.expanduser(
    _env("VOICE_SELFTEST_WAV", os.path.join(HERE, "selftest.wav")))
SELFTEST_TXT = os.path.expanduser(
    _env("VOICE_SELFTEST_TXT", os.path.join(HERE, "selftest.txt")))
SELFTEST_DEFAULT_TEXT = _env("VOICE_SELFTEST_TEXT", "今天天气怎么样")

# 状态常量。SWITCHING=切换执行器持锁期间(拒新轮次);DEBUG=转写台(禁大脑/TTS/barge-in)。
from voice_switching import (IDLE, LISTENING, THINKING,        # noqa: E402
                             SPEAKING, SWITCHING, DEBUG)

START_TS = time.time()

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


def _hermes_env_has(name: str) -> bool:
    """True iff `name` is set to a non-empty value in the profile .env. Reads the
    value only to test emptiness — never returns it, never logs it (§5.5)."""
    try:
        with open(HERMES_ENV, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == name:
                    return bool(v.strip().strip('"').strip("'"))
    except OSError:
        return False
    return False


def hermes_capabilities() -> list[str]:
    """Capability tags for the current profile = the mcp_servers keys in the yaml.
    Capabilities come from the PROFILE, not the model — switching preset never
    changes them (plan §4.1). Read-only, best-effort; [] if the yaml is unreadable."""
    try:
        with open(HERMES_YAML, "r", encoding="utf-8") as fh:
            data = vbrain._yaml().load(fh.read())
        servers = data.get("mcp_servers") if isinstance(data, dict) else None
        if isinstance(servers, dict):
            return sorted(str(k) for k in servers.keys())
    except Exception:                                    # noqa: BLE001
        pass
    return []


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
        self.vad_chunks = 0                       # since-boot 成功喂入的实时块
        self.vad_errors = 0                       # accept_waveform 异常数
        self.vad_last_error: str | None = None
        self._vad_error_ts = 0.0

        # 音频设备(按名发现,失败置 None → /health audio:missing)
        self.cap_card: str | None = None
        self.play_card: str | None = None
        self.audio_ok = False

        # MCP01 摘机 keepalive:采集期间把 USB 话机拉出待机(待机时麦克风增益掉 ~30dB,
        # 跌到 Silero 门限以下)。hidraw 设备按 vid:pid 定位,路径会随拔插漂移故不缓存死。
        self._hidraw: str | None = None
        self._offhook_task: asyncio.Task | None = None

        # 子进程句柄
        self._arecord: asyncio.subprocess.Process | None = None
        self._cap_task: asyncio.Task | None = None
        self._cur_ffmpeg: asyncio.subprocess.Process | None = None
        self._cur_aplay: asyncio.subprocess.Process | None = None
        # 转写台段回放(板上 aplay):独立于对话播放句柄。seg_playing 起时闸 DEBUG 采集,
        # 与对话播报半双工同语义 —— 播完 +HALF_DUPLEX_RESUME 才恢复喂 VAD。
        self._seg_aplay: asyncio.subprocess.Process | None = None
        self.seg_playing = False

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

        # 统一 config(desired state)。缺失/损坏 → 内置默认,照常起(不因 config 起不来)。
        self.config, self.config_source = vconfig.load_config()

        # 引擎宿主(进程内模式,P0a 定谳)。引擎只管模型生命周期 + 同步推理原语;
        # 采集/VAD/generation/子进程/edge->melo 熔断仍归 daemon(§5.1)。
        # ASR 多宿主(数据驱动自 REGISTRY):板上可用 RAM ~1.8G,两引擎不并存,
        # 切换 = 载新→卸旧(见 _apply_asr)。self.asr 始终指向当前运行宿主。
        self.asr_hosts = {n: cls() for n, cls in vengines.REGISTRY["asr"].items()}
        self.asr = self.asr_hosts["sensevoice"]  # 下方按 pair 重定向
        self.melo = vengines.MeloTts()           # 本地 Melo(edge 兜底也用它)
        self.vad = None                          # VadEngine(daemon 所有,可切换)
        # 音频前端(全局,不属 preset pair):当前 VAD 描述 + 数字增益。ephemeral 标记
        # 表示调试态临时改动未落盘 —— 退出 DEBUG 时从 config 还原(与 tts/asr 同语义)。
        self.vad_desc = vconfig.current_vad(self.config)
        self.audio_gain_db = vconfig.current_audio_gain(self.config)
        self._frontend_ephemeral = False
        # DEBUG 流式模式(免VAD,对比验证):按需载的独立 OnlineRecognizer 宿主。
        self.stream_asr = vengines.StreamingAsr()
        self.stream_cfg = vconfig.current_stream(self.config)
        self._stream_last_partial = ""
        self._stream_lock = asyncio.Lock()       # 串行化流式切换(大模型载入排队,latest 在后)
        # 流式解码专用线程(采集循环只入队,永不被解码堵):xlarge 单 320ms 块解码实测可达
        # ~0.3s,内联跑会卡事件循环 → arecord 管道积压 → ALSA overrun 整段丢音,表现就是
        # "识别完一句后好几句被忽略"。worker 随流式引擎载/卸而起/停。
        self._stream_q = None                    # queue.Queue,worker 存活期非 None
        self._stream_thread = None
        self._stream_loop = None                 # worker 甩结果回事件循环用
        self._stream_dropped = 0                 # 积压超上限丢弃的 320ms 块数(病态才会涨)
        self._asr_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")
        self._tts_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="melo")

        # 运行引擎 = 当前 preset pair 的投影(SWITCHING/ephemeral 才偏离,见 switch 执行器)
        _pair = vconfig.current_pair(self.config)
        self.asr_engine = _pair["asr"] if _pair["asr"] in self.asr_hosts else "sensevoice"
        self.asr = self.asr_hosts[self.asr_engine]           # 运行宿主 = 当前 pair
        self.tts_engine = _pair["tts"].get("engine", "edge") # "edge" | "melo"
        self.edge_voice = _pair["tts"].get("voice") or EDGE_VOICE

        # 切换执行器(全局串行) + ephemeral 覆盖(调试态不落盘) + 状态机快照
        self.switcher = vswitch.EngineSwitcher()
        self.override = vswitch.EphemeralOverride()
        self._job_seq = 0
        self._switch_prev_state = IDLE

        # asr 转写台独立增量环(不挤 200 条 feed 环)
        self.debug_tail = vswitch.TailRing(maxlen=200)

        # ASR 段级观测:since-boot 计数器 + 最近段音频回放存储
        self.asr_stats = vobs.AsrStats()
        self.seg_store = SegStore(SEG_DIR, SEG_KEEP)

        # Vision 播报桥(板端后台任务) + caption 去重(限长从 config 读)
        self.vision_task: asyncio.Task | None = None
        self.caption_dedup = vswitch.CaptionDedup(limit=self._vision_limit())

        # 当前轮 id(feed 事件按 turn_id 聚合气泡)
        self.turn_id = 0

        # edge-tts 熔断
        self.edge_fail_streak = 0
        self.breaker_until = 0.0                 # >now 表示熔断中(直接走 Melo)

        # 事件源:SSE 订阅者 + /feed 环形缓冲
        self.sse_subscribers: set[asyncio.Queue] = set()
        self.feed_ring: deque[dict] = deque(maxlen=FEED_RING)
        self.feed_seq = 0

        self.last_error: dict | None = None

    # -- 事件发布(SSE + /feed 环形缓冲同源) ---------------------------- #
    def brain_name(self) -> str:
        return vconfig.current_preset_name(self.config)

    def emit(self, etype: str, **fields) -> None:
        self.feed_seq += 1
        ev = {"type": etype, "seq": self.feed_seq, "ts": round(time.time(), 3),
              "generation": self.generation, "brain": self.brain_name(),
              "turn_id": self.turn_id}
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

    def _vision_limit(self) -> int:
        """Vision 播报朗读字数上限(config vision_speak_limit,Python len)。"""
        try:
            return max(1, int(self.config.get("vision_speak_limit", 300)))
        except (TypeError, ValueError):
            return 300

    # -- ASR 段级观测:每个 VAD 段一条事件 + 计数 + 存音频 ---------------- #
    @staticmethod
    def _seg_meta(samples: np.ndarray) -> tuple[float, float, float]:
        """段的 (dur_s, peak_dbfs, rms_dbfs)。samples 是 float32 归一化 [-1,1]。"""
        n = int(getattr(samples, "size", 0) or 0)
        if n == 0:
            return 0.0, -99.0, -99.0
        peak = float(np.max(np.abs(samples)))
        rms = float(np.sqrt(np.mean(samples * samples)))
        return round(n / 16000.0, 2), vobs.dbfs(peak), vobs.dbfs(rms)

    def _record_seg(self, samples: np.ndarray, outcome: str,
                    text: str | None = None, *, to_debug: bool,
                    emit_feed: bool = True) -> int:
        """一段观测:存 wav → 计数 → 发事件。DEBUG 态发 debug_tail(转写台显示);
        对话态发 feed(type:asr_seg),只在非 accepted 时发(accepted 已有 user_text)。"""
        dur_s, peak, rms = self._seg_meta(samples)
        seg_id = self.seg_store.save(samples)
        self.asr_stats.record(outcome)
        if to_debug:
            ev = self.debug_tail.append(
                "seg", text or "", seg_id=seg_id, outcome=outcome,
                dur_s=dur_s, peak_dbfs=peak, rms_dbfs=rms)
            self.emit("asr_debug", tail_seq=ev["seq"], text=text or "",
                      outcome=outcome, seg_id=seg_id)
        elif emit_feed and outcome != vobs.ACCEPTED:
            fields = {"seg_id": seg_id, "outcome": outcome, "dur_s": dur_s,
                      "peak_dbfs": peak, "rms_dbfs": rms}
            if text:
                fields["text"] = text
            self.emit("asr_seg", **fields)
        return seg_id

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
            # Mic 拧满:MCP01 的 USB Mic Capture Volume 量程 -28dB~0dB 只衰减无增益,
            # 满档(100%)白捡约 4dB。仍不足的部分靠 config audio.gain_db 数字补。
            for args in (["sset", "PCM,0", "90%", "unmute"],
                         ["sset", "PCM,1", "90%"],
                         ["sset", "Mic", "100%", "cap"]):
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
    # -- MCP01 off-hook keepalive -------------------------------------------- #
    MCP01_VID = "17ef"
    MCP01_PID = "a03b"

    @staticmethod
    def _read_sys(path: str) -> str | None:
        try:
            with open(path, "r") as fh:
                return fh.read().strip().lower()
        except OSError:
            return None

    def _find_hidraw(self) -> str | None:
        """Locate the MCP01 hidraw node by vid:pid (never hardcode hidrawN — it drifts on
        replug). idVendor/idProduct sit two levels up from the hidraw's device link."""
        base = "/sys/class/hidraw"
        try:
            names = os.listdir(base)
        except OSError:
            return None
        for name in names:
            vid = self._read_sys(os.path.join(base, name, "device/../../idVendor"))
            pid = self._read_sys(os.path.join(base, name, "device/../../idProduct"))
            if vid == self.MCP01_VID and pid == self.MCP01_PID:
                return f"/dev/{name}"
        return None

    def _write_offhook(self, on: bool) -> bool:
        """Write the off-hook HID report. Silent on failure — a missing device is already
        surfaced as 'audio missing'; don't spam a second error stream."""
        dev = self._hidraw or self._find_hidraw()
        self._hidraw = dev
        if dev is None:
            return False
        try:
            fd = os.open(dev, os.O_WRONLY)
            try:
                os.write(fd, vswitch.offhook_report(on))
            finally:
                os.close(fd)
            return True
        except OSError:
            self._hidraw = None            # path may have drifted; re-find next time
            return False

    async def _offhook_keepalive(self) -> None:
        """Re-assert off-hook every 10s while capturing (the device falls back to idle on
        its own timeout otherwise)."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                await loop.run_in_executor(None, self._write_offhook, True)
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            return

    async def start_capture(self) -> None:
        if self._cap_task and not self._cap_task.done():
            return
        if not self.audio_ok:
            await self.discover_audio()
            if not self.audio_ok:
                return
        self._cap_task = asyncio.create_task(self._capture_loop())
        if self._offhook_task is None or self._offhook_task.done():
            self._offhook_task = asyncio.create_task(self._offhook_keepalive())

    async def stop_capture(self) -> None:
        t = self._cap_task
        self._cap_task = None
        ot = self._offhook_task
        self._offhook_task = None
        if ot and not ot.done():
            ot.cancel()
            try:
                await ot
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, self._write_offhook, False)
        except Exception:                                    # noqa: BLE001
            pass
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

    def _note_level(self, x) -> None:
        """本块 RMS -> dBFS,并保留最近 LEVEL_PEAK_S 秒的最大值。峰值才是有用的
        那个数:说话是断续的,平均会被静音段拉平。x 是已归一化并已加数字增益的 float32
        —— 电平表显示的必须是 VAD/ASR 实际听到的(增益后)幅度,而非原始采集。"""
        if x.size == 0:
            return
        rms = float(np.sqrt(np.mean(x * x)))
        self.mic_dbfs = float(20.0 * np.log10(max(rms, 1e-9)))
        now = time.time()
        if self.mic_dbfs >= self.mic_peak_dbfs or now - self.mic_peak_ts > LEVEL_PEAK_S:
            self.mic_peak_dbfs = self.mic_dbfs
            self.mic_peak_ts = now

    def _handle_chunk(self, data: bytes) -> None:
        """一块 PCM。LISTENING 走正常截句;SPEAKING 开麦做打断检测;其余丢弃。"""
        # 归一化 + 数字增益(gain_db=0 时为恒等,零行为变化)。增益必须在电平表与 VAD
        # 之前统一施加,这样用户看到的电平就是 VAD/ASR 实际听到的幅度。
        samples = vvad.apply_gain(
            np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0,
            self.audio_gain_db)
        # 电平遥测:在闸门之前算,丢弃期也照报——"麦克风到底听没听见我"是排查
        # ASR 不出字的第一个问题,而这台 MCP01 带硬件降噪门(静时输出近似静音,
        # ALSA 增益调了没用),光看波形本底判断不了,必须有说话时的实测值。
        self._note_level(samples)
        listening = (self.state == LISTENING and time.time() >= self.mic_resume_ts)
        barge = (BARGE_IN and self.state == SPEAKING)
        # 转写台:截句 → 转写,不进大脑不触发 TTS。段回放期(seg_playing)及其 250ms 半
        # 双工余量内闸掉采集,免把板上放的段自己截回来。
        debug = (self.state == DEBUG and not self.seg_playing
                 and time.time() >= self.mic_resume_ts)
        if not (listening or barge or debug):
            # 丢弃期间保持 VAD 干净,避免把机器人自己的话截成段
            if self.vad is not None:
                try:
                    self.vad.reset()
                except Exception:
                    pass
            return
        # DEBUG 流式模式(一级=流式):流式引擎自带端点、免VAD,实时出 partial/final。
        # 模式互斥 —— 走流式就不再走 VAD(切模式对比,不并行,免双份推理)。
        if debug and self.stream_cfg.get("enabled"):
            self._feed_stream(samples)
            return
        if self.vad is None:
            return
        try:
            segs = self.vad.feed(samples)
            self.vad_chunks += 1
        except Exception as exc:                              # noqa: BLE001
            # VAD 是语音链路的闸门，这里不能把异常伪装成“没说话”。
            self.vad_errors += 1
            self.vad_last_error = f"{type(exc).__name__}: {exc}"
            now = time.time()
            if now - self._vad_error_ts >= 60.0:
                self._vad_error_ts = now
                self.emit("error", message=f"vad accept failed: {self.vad_last_error}")
            return
        for seg_arr in segs:
            # 只在 LISTENING 起轮;拿到段立刻改 THINKING 停止再截句(单轮保证)
            if self.state == LISTENING and (self.turn_task is None or self.turn_task.done()):
                self.set_state(THINKING)
                gen = self.generation
                self.turn_task = asyncio.create_task(self._asr_then_turn(gen, seg_arr))
            elif (self.state == SPEAKING and barge
                    and (self._barge_task is None or self._barge_task.done())):
                self._barge_task = asyncio.create_task(
                    self._barge_check(self.generation, seg_arr))
            elif self.state == DEBUG:
                asyncio.create_task(self._asr_debug_transcribe(seg_arr))

    # ------------------------------------------------------------------ #
    # ASR → 校验 → 起轮
    # ------------------------------------------------------------------ #
    def _asr_sync(self, samples: np.ndarray) -> str:
        return self.asr.transcribe(samples)

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
        # 段级观测:分类 outcome、计数、存段音频。非 accepted 上 feed(asr_seg),
        # accepted 已有 user_text —— 别刷屏。
        outcome = vobs.classify_segment(text)
        self._record_seg(samples, outcome, text, to_debug=False)
        if outcome != vobs.ACCEPTED:      # 空解码 / 语气词 / 过短 → 不上 LLM
            self.set_state(LISTENING)
            self.refresh_deadline()
            return
        self.turn_id += 1
        self.emit("user_text", text=text)
        await self.run_turn(gen, text)

    async def _asr_debug_transcribe(self, samples: np.ndarray) -> None:
        """转写台:VAD 段 → ASR → 独立增量环(不进大脑、不触发 TTS、不 barge)。
        每段都上转写台,连解码为空(empty_asr)的段也显示 —— 这正是「截到段却没出字」
        的可见化,附 outcome/电平/段音频回放 id。"""
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(self._asr_pool, self._asr_sync, samples)
        except Exception as exc:                                 # noqa: BLE001
            self.emit("error", message=f"asr_debug failed: {exc}")
            return
        if self.state != DEBUG:
            return
        outcome = vobs.classify_segment(text)
        self._record_seg(samples, outcome, text, to_debug=True)

    def _feed_stream(self, samples: np.ndarray) -> None:
        """DEBUG 流式:采集循环侧只入队(微秒级),解码全在专用线程。队列满(积压 ~10s,
        解码远慢于实时的病态)丢新块并计数 —— 有界丢弃可观测,好过 ALSA overrun 静默丢。"""
        q = self._stream_q
        if q is None:
            return
        try:
            q.put_nowait(samples)
        except queue.Full:
            self._stream_dropped += 1

    def _stream_worker(self, q: queue.Queue) -> None:
        """解码线程:取一批(积压时把已到的块合并一次喂,追赶延迟)→ feed → 结果经
        call_soon_threadsafe 甩回事件循环上环。None 哨兵退出。"""
        while True:
            item = q.get()
            if item is None:
                return
            chunks = [item]
            try:
                while True:
                    nxt = q.get_nowait()
                    if nxt is None:
                        return
                    chunks.append(nxt)
            except queue.Empty:
                pass
            samples = chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
            try:
                partial, finals = self.stream_asr.feed(samples)
            except Exception as exc:                             # noqa: BLE001
                msg = f"stream asr failed: {exc}"
                self._stream_loop.call_soon_threadsafe(
                    lambda m=msg: self.emit("error", message=m))
                continue
            self._stream_loop.call_soon_threadsafe(self._stream_emit, partial, finals)

    def _stream_emit(self, partial: str, finals: list) -> None:
        """事件循环侧:端点出 final(合并批可跨多句 → 列表)、合并 partial,进转写台
        增量环(不进大脑/不 TTS)。partial 仅在文本变化时上环去抖。"""
        for final in finals:
            self.debug_tail.append("stream", final, partial=False)
            self._stream_last_partial = ""
        if partial and partial != self._stream_last_partial:
            self.debug_tail.append("stream", partial, partial=True)
            self._stream_last_partial = partial

    def _stream_worker_start(self) -> None:
        if self._stream_thread is not None:
            return
        self._stream_loop = asyncio.get_running_loop()
        self._stream_q = queue.Queue(maxsize=32)          # 32 * 320ms ≈ 10s 积压上限
        self._stream_thread = threading.Thread(
            target=self._stream_worker, args=(self._stream_q,),
            name="stream-decode", daemon=True)
        self._stream_thread.start()

    def _stream_worker_stop(self) -> None:
        """停解码线程并等它退出。会阻塞到当前批解完(积压时可达秒级)——只准在
        executor 里调,别在事件循环上直接调。"""
        t, q = self._stream_thread, self._stream_q
        self._stream_thread = None
        self._stream_q = None                              # 采集侧立即停止入队
        if q is not None:
            while True:                                    # 清积压,保证哨兵放得进去
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            q.put(None)
        if t is not None:
            t.join(timeout=30.0)

    async def _set_stream_runtime(self, want: dict) -> None:
        """把流式引擎载/卸/重载到匹配 want(enabled + endpoint_silence_s;端点规则在 load
        时烘焙,故 silence 变必重载)。只动运行态,不落盘。"""
        loop = asyncio.get_running_loop()
        # 无条件先停解码线程:消除 worker 与 载/卸/reset 之间的一切竞态(要开会再起)。
        await loop.run_in_executor(None, self._stream_worker_stop)
        if want["enabled"]:
            need_reload = (not self.stream_asr.loaded
                           or want["model"] != self.stream_cfg.get("model")
                           or want["endpoint_silence_s"]
                           != self.stream_cfg.get("endpoint_silence_s"))
            if need_reload:
                # 载入提示进转写流(debug_tail):载 700M 要 ~20s,没提示用户以为死了。
                spec = vengines.STREAM_SPECS.get(want["model"]) or {}
                self.debug_tail.append(
                    "stream", f"⏳ 加载流式模型 {want['model']}"
                    f" (~{spec.get('disk_mb', '?')}MB)…", partial=False)
                if self.stream_asr.loaded:
                    await loop.run_in_executor(None, self.stream_asr.unload)
                t0 = time.time()
                try:
                    await loop.run_in_executor(
                        None, self.stream_asr.load, want["model"], want["endpoint_silence_s"])
                except Exception:
                    self.debug_tail.append(
                        "stream", f"❌ 流式模型 {want['model']} 加载失败", partial=False)
                    raise
                self.debug_tail.append(
                    "stream", f"✅ 流式模型 {want['model']} 就绪"
                    f" ({time.time() - t0:.0f}s),可以说话", partial=False)
            else:
                self.stream_asr.reset()
            self._stream_worker_start()
        elif self.stream_asr.loaded:
            await loop.run_in_executor(None, self.stream_asr.unload)
        self.stream_cfg = want
        self._stream_last_partial = ""

    async def _load_stream_bg(self) -> None:
        """进 DEBUG 的流式模型后台载入(/asr_debug 立即返回)。与 config 切换共用
        _stream_lock 串行;拿到锁后重查条件——排队期间用户可能已切走/已载好。"""
        async with self._stream_lock:
            if not self.stream_cfg.get("enabled") or self.stream_asr.loaded:
                return
            try:
                await self._set_stream_runtime(self.stream_cfg)
            except Exception as exc:                             # noqa: BLE001
                self.emit("error", message=f"stream load failed: {exc}")

    async def apply_stream(self, value, ephemeral: bool) -> dict:
        """DEBUG 流式开关 + 端点静音时长。ephemeral=调试态临时(退出 DEBUG 还原);否则
        落盘(存参)。返回实际生效态。"""
        want = vconfig.normalize_stream(value)
        # 先落状态(存参 or ephemeral 标记),快;模型载入在锁内做(可能 700M,慢)。
        if ephemeral:
            self._frontend_ephemeral = True
        else:
            self.config = vconfig.apply_axis(self.config, "stream", want)
            try:
                vconfig.save_config(self.config)
            except OSError as exc:
                self.emit("error", message=f"config save failed: {exc}")
        async with self._stream_lock:                            # 串行化,rapid 切换排队
            try:
                await self._set_stream_runtime(want)
            except Exception as exc:                             # noqa: BLE001
                self.emit("error", message=f"stream mode failed: {exc}")
                return {"error": str(exc), "status": 500}
        self.emit("stream", model=want["model"], enabled=want["enabled"],
                  loaded=self.stream_asr.loaded)                 # 载完通知
        return {"enabled": want["enabled"], "model": want["model"],
                "endpoint_silence_s": want["endpoint_silence_s"],
                "loaded": self.stream_asr.loaded}

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
        # 能量/长度门在 ASR 之前丢弃的段计 gate(存音频可回放,但 SPEAKING 期不上
        # feed 免刷屏)。
        if len(samples) < int(BARGE_MIN_S * 16000):
            self._record_seg(samples, vobs.GATE, to_debug=False, emit_feed=False)
            return
        rms = float(np.sqrt(np.mean(samples * samples)))
        if rms < BARGE_MIN_RMS:
            self._record_seg(samples, vobs.GATE, to_debug=False, emit_feed=False)
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
        self.turn_id += 1
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
        """极简 SSE 解析:产出 (event, data_dict)。data 非 JSON 时包成 {'raw':...}。
        自行按 \\n 切行,不用 aiohttp 行迭代器:那条路有 read_bufsize 上限(512KB),
        assistant.completed 把全文塞进一行 data: 会超限抛错、整轮对话中断(实锅)。"""
        event = "message"
        buf = b""
        async for chunk in resp.content.iter_chunked(65536):
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = buf[:nl].decode("utf-8", "replace").rstrip("\r")
                buf = buf[nl + 1:]
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
        # tts_engine=="melo" → 纯本地 Melo(单独引擎);"edge" → edge 含 melo 兜底+熔断。
        use_edge = self.tts_engine == "edge"
        if use_edge and not breaker_open and FFMPEG:
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

    async def retranscribe_seg(self, seg_id: int) -> dict:
        """用当前 ASR 宿主对已存段重新识别 —— 同段同 PCM(增益已烘焙),只换引擎,
        供切模型后并排对比。段不存在 404;切换中 409;引擎未载 409;识别异常 500。"""
        path = self.seg_store.path(seg_id)
        if path is None:
            return {"error": "segment not found", "id": seg_id, "status": 404}
        if self.switcher.busy:
            return {"error": "switch in progress", "id": seg_id, "status": 409}
        if not self.asr.loaded:
            return {"error": "asr engine not loaded", "id": seg_id, "status": 409}
        engine = self.asr_engine
        samples = read_wav_16k(path)
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(self._asr_pool, self.asr.transcribe,
                                              samples)
        except Exception as exc:                             # noqa: BLE001
            return {"error": f"asr failed: {exc}", "id": seg_id,
                    "engine": engine, "status": 500}
        return {"id": seg_id, "engine": engine, "text": text or "", "status": 200}

    async def play_seg(self, seg_id: int) -> dict:
        """转写台段回放:板上(MCP01 音响)aplay 放段 wav(16k mono s16le)。段不存在
        → 404;音响不可用 → 409;aplay 非零 → 500(非零必报错,不装成功)。latest-wins:
        新请求先 kill 在放的旧 aplay。播放期 seg_playing 起闸 DEBUG 采集,播完设半双工
        余量恢复。返回 dict 带可选 status 供 HTTP 层取用。"""
        path = self.seg_store.path(seg_id)
        if path is None:
            return {"error": "segment not found", "id": seg_id, "status": 404}
        if not (self.audio_ok and self.play_card):
            return {"error": "音响不可用", "id": seg_id, "status": 409}
        # latest-wins:前一段还在放就打断(被 kill 的旧任务在其 finally 里不动共享态)
        if self._seg_aplay is not None and self._seg_aplay.returncode is None:
            self._kill(self._seg_aplay)
            await self._reap(self._seg_aplay)
        self._seg_aplay = None
        self.seg_playing = True
        ap = None
        try:
            ap = await asyncio.create_subprocess_exec(
                "aplay", "-D", self.play_dev(),
                "-r", "16000", "-f", "S16_LE", "-c", "1", "-q", path,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._seg_aplay = ap
            rc = await asyncio.wait_for(ap.wait(), timeout=30.0)
            if rc == 0:
                return {"ok": True, "id": seg_id}
            if rc < 0:            # 负码 = 被 latest-wins/关停 SIGKILL,不是设备故障
                return {"ok": False, "superseded": True, "id": seg_id}
            self._playback_dead(rc)   # 正码 = aplay 自身失败(设备失效),不装成功
            return {"error": f"aplay exited rc={rc}", "id": seg_id, "status": 500}
        except Exception as exc:                     # noqa: BLE001
            self._kill(ap)
            self.emit("error", message=f"seg play failed: {exc}")
            return {"error": str(exc), "id": seg_id, "status": 500}
        finally:
            await self._reap(ap)
            # 只有仍持有本句柄(未被 latest-wins 接管)才复位闸门,否则会误放行正在放的新段
            if self._seg_aplay is ap:
                self._seg_aplay = None
                self.seg_playing = False
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

            comm = edge_tts.Communicate(text, self.edge_voice)
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
        return self.melo.synth(text)

    async def _melo_play(self, gen: int, text: str) -> bool:
        """本地 Melo:float32@44100 → int16 → aplay -r 44100。合成丢 executor。"""
        if not self.melo.loaded or not self.audio_ok:
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
                comm = edge_tts.Communicate("好", self.edge_voice)
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
        """调试直接播报,走完整 TTS 通道,可被 interrupt 立停。
        latest-wins:新 say 先取消在途 say(kill aplay/ffmpeg + gen++ 令旧回调过期),
        修此前连发 /say 两路 aplay 叠播的缺陷。"""
        if self.say_task and not self.say_task.done():
            prev = self.state
            await self._abort_playback()          # gen++,停旧播放,cancel 旧 say/turn
            gen = self.generation
            self.set_state(SPEAKING)
            self.say_task = asyncio.create_task(self._say_run(gen, text, prev))
            return
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
        loop = asyncio.get_running_loop()

        desc = self.vad_desc

        def _load():
            self.asr.load()                          # SenseVoice(引擎宿主)
            self.melo.load()                         # Melo(含预热)
            # 当前 config 选中的 VAD 引擎。默认 silero + 默认参数 ⇒ 与 P2.7 前逐字节一致。
            return vvad.make_vad(desc["engine"], desc, MODELS)

        self.vad = await loop.run_in_executor(None, _load)
        print(f"[voice-daemon] models loaded (ASR+VAD:{desc['engine']}+Melo)", flush=True)

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

    # ------------------------------------------------------------------ #
    # 切换执行器(全局串行,单轴,报真实 applied,不承诺原子回滚)§5.2
    # ------------------------------------------------------------------ #
    def new_job_id(self) -> str:
        self._job_seq += 1
        return f"job-{self._job_seq}"

    def applied_engines(self) -> dict:
        """实际加载/运行的引擎(applied 状态)。"""
        return {"asr": self.asr_engine, "tts_engine": self.tts_engine,
                "edge_voice": self.edge_voice if self.tts_engine == "edge" else None}

    def brain_drift(self) -> dict | None:
        """desired 大脑(config.json 当前 preset 的 provider/model)vs applied(实际
        config.yaml 的 model.provider/default)。人手改了 yaml 就在这里现形(§5.5)。
        yaml 读不了 → None(不报假漂移)。key 值不涉及,只比 provider/model 名。"""
        try:
            with open(HERMES_YAML, "r", encoding="utf-8") as fh:
                data = vbrain._yaml().load(fh.read())
            model = data.get("model") if isinstance(data, dict) else None
            applied_provider = (model or {}).get("provider")
            applied_model = (model or {}).get("default")
        except Exception:                                    # noqa: BLE001
            return None
        want_provider = vbrain.PROVIDER_PREFIX + vconfig.current_preset_name(self.config)
        want_model = vconfig.current_preset(self.config).get("model")
        if applied_provider != want_provider or applied_model != want_model:
            return {"desired": {"provider": want_provider, "model": want_model},
                    "applied": {"provider": applied_provider, "model": applied_model}}
        return None

    def drift(self) -> dict:
        """desired(config pair)与 applied(实际运行)的差异;ephemeral 覆盖也算漂移。"""
        d = vconfig.compute_drift(vconfig.current_pair(self.config),
                                  self.applied_engines())
        if self.override.active():
            d["ephemeral"] = self.override.get()
        bd = self.brain_drift()
        if bd:
            d["brain"] = bd
        return d

    def _vad_enums(self) -> list:
        """vad 引擎表 + 本板实测可用性(sherpa 是否带 ten_vad / webrtcvad 是否装了 /
        模型是否在盘)。不可用的引擎 GUI 置灰并拒绝切换。"""
        avail = vvad.availability(MODELS)
        out = []
        for e in vconfig.enums()["vad"]:
            e = dict(e)
            e["available"] = bool(avail.get(e["id"], False))
            out.append(e)
        return out

    def config_view(self) -> dict:
        """GET /config:全量 desired + applied + drift + 各轴枚举(含 edge 音色表 + vad
        引擎可用性)+ capabilities(profile mcp_servers 键名)。"""
        enums = vconfig.enums()
        enums["vad"] = self._vad_enums()
        return {"desired": self.config, "applied": self.applied_engines(),
                "drift": self.drift(), "enums": enums,
                "capabilities": hermes_capabilities(),
                "source": self.config_source}

    async def _drain_pools(self) -> None:
        """等推理池在途任务跑完(单 worker → no-op 排在其后),防推理中卸载 use-after-free。"""
        loop = asyncio.get_running_loop()
        for pool in (self._asr_pool, self._tts_pool):
            try:
                await loop.run_in_executor(pool, lambda: None)
            except Exception:
                pass

    async def _apply_tts(self, tts_value) -> bool:
        """切运行 TTS 引擎。edge 兜底与 melo 单独都需要 Melo 模型常驻。"""
        loop = asyncio.get_running_loop()
        engine = tts_value.get("engine") if isinstance(tts_value, dict) else tts_value
        if engine not in vconfig.TTS_ENGINES:
            raise ValueError(f"unknown tts engine: {engine}")
        if not self.melo.loaded:
            await loop.run_in_executor(None, self.melo.load)
        self.tts_engine = engine
        if engine == "edge" and isinstance(tts_value, dict) and tts_value.get("voice"):
            self.edge_voice = tts_value["voice"]
        return True

    async def _apply_asr(self, asr_value) -> bool:
        """切运行 ASR 宿主。载新→卸旧(板上可用 RAM ~1.8G,两引擎不并存;先载新,
        失败则旧引擎原样保留,由 switch_engine 判 degraded)。调用方须已 drain 推理池。"""
        loop = asyncio.get_running_loop()
        name = asr_value.get("asr") if isinstance(asr_value, dict) else asr_value
        if name not in vconfig.ASR_ENGINES or name not in self.asr_hosts:
            raise ValueError(f"unknown asr engine: {name}")
        target = self.asr_hosts[name]
        old = self.asr
        # 大宿主(unload_first,如 funasr 子进程 ~1.6GB 峰值)与旧引擎并存会击穿 RAM →
        # swap 抖动全板假死(2026-07-21 实锅)。这类引擎先卸旧再载新;载失败即 degraded
        # (诚实降级,switch_engine 已有此语义),不装能回滚。
        unload_first = getattr(target, "unload_first", False)
        if unload_first and old is not target and old.loaded:
            await loop.run_in_executor(None, old.unload)
        if not target.loaded:
            await loop.run_in_executor(None, target.load)    # 默认先载新(小引擎可并存)
        self.asr = target
        self.asr_engine = name
        if not unload_first and old is not target and old.loaded:
            await loop.run_in_executor(None, old.unload)      # 再卸旧,回收 RSS
        return True

    async def _apply_config_pair_runtime(self) -> None:
        """把运行引擎拉回 config pair(退出 DEBUG / 清 ephemeral 覆盖时)。ASR 可能被临时
        切成别的宿主 → 先 drain 再真正卸调试引擎、载回 config 引擎。"""
        pair = vconfig.current_pair(self.config)
        if pair["asr"] != self.asr_engine:
            await self._drain_pools()
            try:
                await self._apply_asr(pair["asr"])
            except Exception as exc:                          # noqa: BLE001
                self.emit("error", message=f"restore asr failed: {exc}")
        self.tts_engine = pair["tts"].get("engine", "edge")
        if pair["tts"].get("voice"):
            self.edge_voice = pair["tts"]["voice"]

    async def switch_engine(self, axis: str, value, ephemeral: bool,
                            job_id: str) -> None:
        """唯一切换路径(HTTP 层已 try_begin 抢到锁)。冻结→drain→卸旧/载新→
        成功落盘(或 ephemeral 只切运行)→报真实 applied(可能 degraded)。"""
        prev_pair = vconfig.effective_pair(self.config, self.override.get())
        prev_engine = prev_pair["tts"]["engine"] if axis == "tts" else prev_pair["asr"]
        target = value.get("engine") if isinstance(value, dict) else value
        self._switch_prev_state = self.state
        self.emit("job", job_id=job_id, phase="start", axis=axis, target=target,
                  ephemeral=bool(ephemeral))
        new_loaded = False
        old_reloaded = False
        try:
            # 冻结:中断在途轮次/播放(gen++、interrupt),再 drain 推理池
            await self._abort_playback()
            self.set_state(SWITCHING)
            await self._drain_pools()
            try:
                if axis == "tts":
                    new_loaded = await self._apply_tts(value)
                elif axis == "asr":
                    new_loaded = await self._apply_asr(value)
                else:
                    raise ValueError(f"axis {axis} not switchable")
            except Exception as exc:                             # noqa: BLE001
                self.emit("error", message=f"switch {axis} failed: {exc}")
                new_loaded = False
                try:                                             # 尽力重载旧引擎
                    if axis == "tts":
                        old_reloaded = await self._apply_tts(prev_pair["tts"])
                    elif axis == "asr":
                        old_reloaded = await self._apply_asr(prev_pair["asr"])
                except Exception:                                # noqa: BLE001
                    old_reloaded = False
            result = vswitch.resolve_switch(prev_engine, target, new_loaded,
                                            old_reloaded)
            if result["persist"] and not ephemeral:
                self.config = vconfig.apply_axis(self.config, axis, value)
                try:
                    vconfig.save_config(self.config)
                except OSError as exc:
                    self.emit("error", message=f"config save failed: {exc}")
                self.override.clear()      # 落盘后 config 即真相,清调试覆盖
            elif result["status"] == "ok" and ephemeral:
                self.override.set(axis, value)  # 调试态:只切运行引擎不落盘
            self.emit("job", job_id=job_id, phase="done", axis=axis,
                      status=result["status"], applied=result["applied"],
                      drift=self.drift())
        finally:
            # 恢复状态机:调试态回 DEBUG(续采集),否则回 IDLE(切换已中断对话)
            if self._switch_prev_state == DEBUG:
                self.set_state(DEBUG)
                await self.start_capture()
            else:
                self.set_state(IDLE)
                await self.stop_capture()
            self.switcher.end()

    # ------------------------------------------------------------------ #
    # VAD 切换:先卸后载(MB 级,不做 malloc_trim 大动作),复用切换执行器序。§5.2
    # HTTP 层已校验引擎可用 + try_begin 抢锁。失败保旧 VAD,报真实 applied。
    # ------------------------------------------------------------------ #
    async def switch_vad(self, value, ephemeral: bool, job_id: str) -> None:
        target = value.get("engine")
        self._switch_prev_state = self.state
        self.emit("job", job_id=job_id, phase="start", axis="vad", target=target,
                  ephemeral=bool(ephemeral))
        prev_vad, prev_desc = self.vad, self.vad_desc
        loop = asyncio.get_running_loop()
        status = "degraded"
        applied = None
        try:
            await self._abort_playback()
            self.set_state(SWITCHING)
            try:
                new_vad = await loop.run_in_executor(
                    None, vvad.make_vad, target, value, MODELS)
                self.vad = new_vad
                self.vad_desc = vconfig.normalize_vad(value)
                status, applied = "ok", target
                self._dispose_vad(prev_vad)              # fsmn 常驻子进程要收尸
            except Exception as exc:                          # noqa: BLE001
                self.emit("error", message=f"switch vad failed: {exc}")
                self.vad, self.vad_desc = prev_vad, prev_desc  # 保旧 VAD,通路不断
                status, applied = "reverted", prev_desc.get("engine")
            if status == "ok" and not ephemeral:
                self.config = vconfig.apply_axis(self.config, "vad", value)
                try:
                    vconfig.save_config(self.config)
                except OSError as exc:
                    self.emit("error", message=f"config save failed: {exc}")
                self._frontend_ephemeral = False
            elif status == "ok" and ephemeral:
                self._frontend_ephemeral = True
            self.emit("job", job_id=job_id, phase="done", axis="vad",
                      status=status, applied=applied, drift=self.drift())
        finally:
            if self._switch_prev_state == DEBUG:
                self.set_state(DEBUG)
                await self.start_capture()
            else:
                self.set_state(IDLE)
                await self.stop_capture()
            self.switcher.end()

    def apply_audio_gain(self, gain_db, ephemeral: bool) -> float:
        """数字增益即时生效(无需重建 VAD/推理池,不走切换执行器)。ephemeral=调试态不落盘,
        退出 DEBUG 还原;否则落盘。返回实际生效值。"""
        gain = vconfig.clamp_gain(gain_db)
        self.audio_gain_db = gain
        if ephemeral:
            self._frontend_ephemeral = True
        else:
            self.config = vconfig.apply_axis(self.config, "audio", {"gain_db": gain})
            try:
                vconfig.save_config(self.config)
            except OSError as exc:
                self.emit("error", message=f"config save failed: {exc}")
        return gain

    async def restore_frontend(self) -> None:
        """退出 DEBUG:把音频前端(VAD + 增益)从 ephemeral 改动还原回 config。VAD 引擎/
        参数变了才重建。与 tts/asr 的 _apply_config_pair_runtime 同语义。"""
        if not self._frontend_ephemeral:
            return
        self._frontend_ephemeral = False
        self.audio_gain_db = vconfig.current_audio_gain(self.config)
        # 流式模式的 ephemeral 改动也还原回 config
        want_stream = vconfig.current_stream(self.config)
        if want_stream != self.stream_cfg:
            try:
                await self._set_stream_runtime(want_stream)
            except Exception as exc:                              # noqa: BLE001
                self.emit("error", message=f"restore stream failed: {exc}")
        want = vconfig.current_vad(self.config)
        if want == self.vad_desc:
            return
        loop = asyncio.get_running_loop()
        try:
            old_vad = self.vad
            self.vad = await loop.run_in_executor(
                None, vvad.make_vad, want["engine"], want, MODELS)
            self.vad_desc = want
            self._dispose_vad(old_vad)
        except Exception as exc:                              # noqa: BLE001
            self.emit("error", message=f"restore vad failed: {exc}")

    @staticmethod
    def _dispose_vad(vad) -> None:
        """丢弃被换下的 VAD。fsmn 持有常驻子进程,靠 GC 不可靠 —— close() 显式收尸;
        其余引擎没有 close,no-op。"""
        close = getattr(vad, "close", None)
        if close is not None:
            try:
                close()
            except Exception:                                 # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # 大脑下发:Hermes yaml 补丁事务(§5.5)。HTTP 层已做前置校验并 try_begin。
    # 流程:备份 → 补丁 → restart → 就绪轮询 → session 重建 → 真实探针 →
    # 过则落盘+应用 pair;败则按备份还原、重启、再探针旧模型,全程 feed 回报。
    # ------------------------------------------------------------------ #
    @staticmethod
    def _atomic_write(path: str, text: str) -> None:
        d = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".cfg.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    @staticmethod
    async def _systemctl_restart(unit: str) -> int:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", unit,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.communicate()
        return proc.returncode if proc.returncode is not None else -1

    async def _hermes_wait_ready(self, timeout: float) -> bool:
        """轮询网关 /health 直到 <400 或超时(重启后端口就绪 ≠ 模型可用,后者靠探针)。"""
        headers = {}
        if HERMES_KEY:
            headers["Authorization"] = f"Bearer {HERMES_KEY}"
        deadline = time.time() + timeout
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=3)) as sess:
            while time.time() < deadline:
                try:
                    async with sess.get(f"{HERMES_BASE}/health",
                                        headers=headers) as r:
                        if r.status < 400:
                            return True
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.5)
        return False

    async def _brain_probe(self, timeout: float) -> tuple[bool, str]:
        """真实 1-token completion 探针:临时 session、极短提示,打到当前 provider。
        任一 assistant token/completed 即通过;error/HTTP/超时即失败(带原因)。
        端口 200 但错 key/错模型名会在这里被抓住(§5.5 验收门核心)。"""
        sess_id = f"probe-{int(time.time() * 1000)}"
        jhead = {}
        if HERMES_KEY:
            jhead["Authorization"] = f"Bearer {HERMES_KEY}"
        shead = dict(jhead)
        shead["Accept"] = "text/event-stream"
        timeout_cfg = aiohttp.ClientTimeout(total=timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout_cfg) as sess:
                try:
                    async with sess.post(f"{HERMES_BASE}/api/sessions",
                                         json={"id": sess_id}, headers=jhead) as r:
                        await r.text()
                except aiohttp.ClientError:
                    pass
                ok, reason = False, "no completion"
                try:
                    url = f"{HERMES_BASE}/api/sessions/{sess_id}/chat/stream"
                    # 关键:Hermes 网关把 provider 的 HTTP 4xx(错模型名/错 key)伪装成
                    # assistant.completed,content 是 "HTTP 400: ..." 错误串,零 delta、
                    # 零 output token,且不发 error 事件。所以「过」必须要求真实产出:
                    # 收到过 assistant.delta,或 completed 且 usage.output_tokens>0。
                    saw_delta = False
                    completed_content = None
                    completed_seen = False
                    out_tokens = None
                    async with sess.post(url, json={"message": "回复:好"},
                                         headers=shead) as resp:
                        if resp.status >= 400:
                            body = (await resp.text())[:160]
                            return False, f"HTTP {resp.status}: {body}"
                        async for evt, data in self._iter_sse(resp):
                            if evt == "assistant.delta" and data.get("delta"):
                                saw_delta = True
                            elif evt == "assistant.completed":
                                completed_seen = True
                                completed_content = data.get("content")
                            elif evt == "run.completed":
                                completed_seen = True
                                out_tokens = (data.get("usage") or {}).get(
                                    "output_tokens")
                                break
                            elif evt == "error":
                                return False, str(data.get("message",
                                                           "provider error"))[:160]
                            elif evt == "done":
                                break
                    if saw_delta or (completed_seen and (out_tokens or 0) > 0):
                        ok, reason = True, "ok"
                    else:
                        # 伪装失败:把 completed 里的错误串原样报出(通常带 HTTP 4xx)
                        ok = False
                        reason = str(completed_content
                                     or "empty completion (0 output tokens)")[:160]
                finally:
                    try:
                        async with sess.delete(
                                f"{HERMES_BASE}/api/sessions/{sess_id}",
                                headers=jhead) as r:
                            await r.text()
                    except aiohttp.ClientError:
                        pass
                return ok, reason
        except asyncio.TimeoutError:
            return False, f"timeout >{timeout:g}s"
        except Exception as exc:                             # noqa: BLE001
            return False, f"probe error: {exc}"

    async def _revert_brain(self, orig_text: str, prev_name: str,
                            job_id: str, reason: str) -> None:
        """按备份还原 yaml、重启网关、重建 session、再探针旧模型,feed 报出原因。"""
        try:
            self._atomic_write(HERMES_YAML, orig_text)
        except OSError as exc:
            self.emit("error", message=f"yaml restore failed: {exc}")
        await self._systemctl_restart(HERMES_UNIT)
        await self._hermes_wait_ready(HERMES_READY_TIMEOUT)
        await self.ensure_hermes_session()
        ok, preason = await self._brain_probe(HERMES_PROBE_TIMEOUT)
        self.emit("job", job_id=job_id, phase="reverted", axis="brain",
                  status="reverted", preset=prev_name, reason=reason,
                  old_probe=("ok" if ok else preason), drift=self.drift())

    async def switch_brain(self, preset_name: str, job_id: str) -> None:
        presets = self.config.get("presets") or {}
        preset = presets.get(preset_name)
        prev_name = vconfig.current_preset_name(self.config)
        self._switch_prev_state = self.state
        self.emit("job", job_id=job_id, phase="start", axis="brain",
                  target=preset_name)
        orig_text = None
        try:
            await self._abort_playback()
            self.set_state(SWITCHING)
            self.emit("job", job_id=job_id, phase="precheck", axis="brain",
                      target=preset_name)
            with open(HERMES_YAML, "r", encoding="utf-8") as fh:
                orig_text = fh.read()
            h8 = hashlib.sha256(orig_text.encode("utf-8")).hexdigest()[:8]
            bak_path = f"{HERMES_YAML}.{h8}.bak"
            try:
                if not os.path.exists(bak_path):
                    self._atomic_write(bak_path, orig_text)
            except OSError as exc:
                self.emit("error", message=f"backup failed: {exc}")
            try:
                new_text = vbrain.plan_yaml_patch(orig_text, preset_name, preset)
            except vbrain.BrainError as exc:
                self.emit("job", job_id=job_id, phase="reverted", axis="brain",
                          status="error", reason=f"patch refused: {exc}")
                return
            self.emit("job", job_id=job_id, phase="patch", axis="brain",
                      target=preset_name)
            self._atomic_write(HERMES_YAML, new_text)
            self.emit("job", job_id=job_id, phase="restart", axis="brain",
                      target=preset_name)
            rc = await self._systemctl_restart(HERMES_UNIT)
            if not await self._hermes_wait_ready(HERMES_READY_TIMEOUT):
                await self._revert_brain(
                    orig_text, prev_name, job_id,
                    f"gateway not ready in {HERMES_READY_TIMEOUT:g}s (rc={rc})")
                return
            await self.ensure_hermes_session()
            self.emit("job", job_id=job_id, phase="probe", axis="brain",
                      target=preset_name)
            ok, reason = await self._brain_probe(HERMES_PROBE_TIMEOUT)
            if not ok:
                await self._revert_brain(orig_text, prev_name, job_id,
                                         f"probe failed: {reason}")
                return
            self.config = vconfig.apply_axis(
                self.config, "brain", {"kind": "hermes", "preset": preset_name})
            try:
                vconfig.save_config(self.config)
            except OSError as exc:
                self.emit("error", message=f"config save failed: {exc}")
            await self._apply_config_pair_runtime()
            self.override.clear()
            self.emit("job", job_id=job_id, phase="done", axis="brain",
                      status="ok", preset=preset_name, drift=self.drift(),
                      capabilities=hermes_capabilities())
        except Exception as exc:                             # noqa: BLE001
            if orig_text is not None:
                await self._revert_brain(orig_text, prev_name, job_id,
                                         f"unexpected: {exc}")
            else:
                self.emit("job", job_id=job_id, phase="reverted", axis="brain",
                          status="error", reason=f"unexpected: {exc}")
        finally:
            if self._switch_prev_state == DEBUG:
                self.set_state(DEBUG)
                await self.start_capture()
            else:
                self.set_state(IDLE)
                await self.stop_capture()
            self.switcher.end()

    # ------------------------------------------------------------------ #
    # Vision 模型切换(P2.6):代理 vlm-daemon POST /model,进度/结局转成 job feed。
    # 切模型只重启 llama-server(与对话用的 hermes 无关),故不冻结状态机/不打断对话。
    # switcher 锁串行化(HTTP 层已 try_begin);vlm 侧另有 busy 标志防并发。
    # ------------------------------------------------------------------ #
    async def switch_vision(self, model_id: str, job_id: str) -> None:
        self.emit("job", job_id=job_id, phase="start", axis="vision",
                  target=model_id)
        token = _load_vlm_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            self.emit("job", job_id=job_id, phase="restart", axis="vision",
                      target=model_id)
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=VLM_MODEL_TIMEOUT)) as sess:
                async with sess.post(f"{VLM_BASE}/model",
                                     json={"id": model_id}, headers=headers) as r:
                    try:
                        data = await r.json()
                    except (aiohttp.ContentTypeError, ValueError):
                        data = {"status": "error",
                                "error": f"vlm HTTP {r.status}: {(await r.text())[:160]}"}
            status = data.get("status")
            if status == "ok":
                self.config = vconfig.apply_axis(
                    self.config, "vision", {"model": model_id})
                try:
                    vconfig.save_config(self.config)
                except OSError as exc:
                    self.emit("error", message=f"config save failed: {exc}")
                self.emit("job", job_id=job_id, phase="done", axis="vision",
                          status="ok", active=data.get("active"),
                          load_s=data.get("load_s"))
            else:
                # reverted / degraded / error — surface the real vlm outcome
                self.emit("job", job_id=job_id, phase="reverted", axis="vision",
                          status=status or "error",
                          reason=data.get("error") or data.get("reason"),
                          active=data.get("active"),
                          old_probe=data.get("old_probe"))
        except Exception as exc:                             # noqa: BLE001
            self.emit("job", job_id=job_id, phase="reverted", axis="vision",
                      status="error", reason=f"vlm unreachable: {exc}")
        finally:
            self.switcher.end()

    # ------------------------------------------------------------------ #
    # 转写台 DEBUG 态开关 §5.4
    # ------------------------------------------------------------------ #
    async def set_debug(self, on: bool) -> None:
        if on:
            if self.state in (LISTENING, THINKING, SPEAKING):
                # 与对话互斥:先内部停对话并向 feed 广播,再进 DEBUG
                await self.do_stop()
                self.emit("debug", status="took_over", message="对话被转写台终止")
            self.debug_tail.clear()
            self.set_state(DEBUG)
            # 流式模式已开则后台载流式引擎(免VAD 走它);仅 DEBUG 期常驻,退出即卸。
            # 不能内联 await:xlarge 700M 载 ~20s,会把 /asr_debug 拖到 GUI HTTP 超时
            # ("转写开关失败: timed out")。进度提示由 _set_stream_runtime 发进转写流。
            if self.stream_cfg.get("enabled") and not self.stream_asr.loaded:
                asyncio.create_task(self._load_stream_bg())
            await self.start_capture()
        else:
            if self.state == DEBUG:
                self.set_state(IDLE)
                await self.stop_capture()
            # 退出 DEBUG:ephemeral 引擎改动自动还原为 config pair;音频前端(VAD+增益)同理
            await self._apply_config_pair_runtime()
            self.override.clear()
            await self.restore_frontend()
            # 流式引擎只在 DEBUG 期有意义 → 退出即卸,释放 ~190MB(下次进 DEBUG 再载)。
            if self.stream_asr.loaded:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._stream_worker_stop)
                await loop.run_in_executor(None, self.stream_asr.unload)

    # ------------------------------------------------------------------ #
    # Vision 播报桥(板端后台任务)§5.6
    # ------------------------------------------------------------------ #
    def _vision_can_speak(self) -> bool:
        if self.state in (DEBUG, SWITCHING, THINKING):
            return False
        if self.turn_task and not self.turn_task.done():   # 对话进行中,对话优先
            return False
        if self.say_task and not self.say_task.done():     # 正在播上一条,丢弃新帧不排队
            return False
        return True

    async def set_vision_speak(self, on: bool) -> None:
        if on and (self.vision_task is None or self.vision_task.done()):
            self.caption_dedup = vswitch.CaptionDedup(limit=self._vision_limit())
            self.vision_task = asyncio.create_task(self._vision_bridge_loop())
            self.emit("vision", status="on")
        elif not on and self.vision_task and not self.vision_task.done():
            self.vision_task.cancel()
            self.vision_task = None
            self.emit("vision", status="off")

    async def _vision_bridge_loop(self) -> None:
        """轮询 vlm caption(先 POST watch 保活,1.5s 间隔),seq/frame_ts 去重 + >120 截断,
        经 say 走 latest-wins;对话/DEBUG 时暂停;vlm 不可达静默重试不刷错误。"""
        token = _load_vlm_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        last_watch = 0.0
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=8)) as sess:
                while True:
                    try:
                        now = time.time()
                        if now - last_watch > 30.0:      # 保活:周期 promote watch
                            try:
                                async with sess.post(
                                        f"{VLM_BASE}/state",
                                        json={"state": "watch"}, headers=headers) as r:
                                    await r.read()
                                last_watch = now
                            except aiohttp.ClientError:
                                pass
                        async with sess.get(f"{VLM_BASE}/caption",
                                            headers=headers) as r:
                            cap = await r.json() if r.status < 400 else None
                        text = self.caption_dedup.accept(cap) if cap else None
                        if text and self._vision_can_speak():
                            self.emit("vision_caption", text=text)
                            await self.do_say(text)
                    except asyncio.CancelledError:
                        raise
                    except Exception:                    # noqa: BLE001
                        pass                             # vlm 不可达:静默重试
                    await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------ #
    # 一键回环自检:已知人声 wav 直接喂 VAD+ASR 全链(不用麦克风)§P2.5
    # ------------------------------------------------------------------ #
    def _selftest_sync(self) -> tuple[int, str, bool]:
        """在 asr_pool 里跑:读测试 wav → (数字增益) → 独立 VAD 截段 → 逐段 ASR → 拼接。
        走当前选中的 VAD 引擎 + 当前增益(与在线通路一致),独立实例绝不碰 daemon 的在线
        VAD 状态。无段则整段兜底解码 —— 这是「验证 VAD 效果」的一键途径。
        逐块采样 vad.active,回报是否真实翻转过(证明 in-speech 状态对该引擎生效)。"""
        samples = vvad.apply_gain(read_wav_16k(SELFTEST_WAV), self.audio_gain_db)
        vad = vvad.make_vad(self.vad_desc["engine"], self.vad_desc, MODELS)
        texts: list[str] = []
        n = 0
        active_seen = False
        step = int(0.32 * 16000)

        def _run(segs) -> None:
            nonlocal n
            for seg in segs:
                n += 1
                texts.append(self.asr.transcribe(seg))

        try:
            for i in range(0, len(samples), step):
                _run(vad.feed(samples[i:i + step]))
                active_seen = active_seen or vad.active
            _run(vad.flush())
        finally:
            self._dispose_vad(vad)
        asr_text = "".join(t for t in texts if t).strip()
        if not asr_text:                                     # 没截出段 → 整段兜底
            asr_text = self.asr.transcribe(samples)
        return n, asr_text, active_seen

    async def run_selftest(self) -> dict:
        if not os.path.exists(SELFTEST_WAV):
            return {"error": f"selftest wav missing: {SELFTEST_WAV}", "pass": False}
        if not self.asr.loaded:
            return {"error": "asr engine not loaded", "pass": False}
        expected = SELFTEST_DEFAULT_TEXT
        try:
            with open(SELFTEST_TXT, "r", encoding="utf-8") as fh:
                t = fh.read().strip()
                if t:
                    expected = t
        except OSError:
            pass
        loop = asyncio.get_running_loop()
        try:
            segs, asr_text, active_seen = await loop.run_in_executor(
                self._asr_pool, self._selftest_sync)
        except Exception as exc:                             # noqa: BLE001
            self.emit("error", message=f"selftest failed: {exc}")
            return {"error": f"selftest failed: {exc}", "pass": False}
        ratio = round(vobs.similarity(asr_text, expected), 3)
        ok = ratio >= 0.5
        result = {"vad_segments": segs, "asr_text": asr_text,
                  "expected": expected, "ratio": ratio, "pass": ok,
                  "vad_engine": self.vad_desc.get("engine"),
                  "vad_active_seen": bool(active_seen),
                  "gain_db": round(self.audio_gain_db, 1)}
        self.emit("selftest", **result)
        return result

    def health(self) -> dict:
        return {
            "state": self.state,
            "audio": "ok" if self.audio_ok else "missing",
            "capture_card": self.cap_card,
            "playback_card": self.play_card,
            "asr_loaded": self.asr.loaded,
            "tts_local_loaded": self.melo.loaded,
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
            "turn_id": self.turn_id,
            # ASR 段级统计(daemon 启动起累计):VAD 截段总数 + 各结局分布
            "asr_stats": self.asr_stats.snapshot(),
            "vad_stats": {"chunks": self.vad_chunks, "errors": self.vad_errors,
                          "last_error": self.vad_last_error},
            # VAD 圆点:当前是否处于开段中(在听)。GUI 据此点绿灯。
            "vad_active": bool(self.vad.active) if self.vad is not None else False,
            "vad_engine": self.vad_desc.get("engine"),
            "audio_gain_db": round(self.audio_gain_db, 1),
            # 流式模式运行态(可能是 ephemeral,与 desired.stream 不同):GUI 据此反映实际
            "stream": {"enabled": bool(self.stream_cfg.get("enabled")),
                       "model": self.stream_cfg.get("model"),
                       "endpoint_silence_s": self.stream_cfg.get("endpoint_silence_s"),
                       "loaded": self.stream_asr.loaded,
                       # 解码积压秒数(采集入队 - worker 消费差):持续>0 说明解码跟不上
                       # 实时;dropped 涨 = 积压破 10s 上限在丢块,该换小模型了。
                       "backlog_s": round((_sq.qsize() if (_sq := self._stream_q)
                                           else 0) * 0.32, 2),
                       "dropped": self._stream_dropped},
            # 统一 config 三态 + 引擎状态(desired/applied/drift)
            "desired": {"brain": self.config.get("brain"),
                        "vision_speak": bool(self.config.get("vision_speak")),
                        "vision_speak_limit": self._vision_limit(),
                        "pair": vconfig.current_pair(self.config)},
            "applied": self.applied_engines(),
            "drift": self.drift(),
            "engines": {"asr_loaded": self.asr.loaded, "melo_loaded": self.melo.loaded,
                        "tts_engine": self.tts_engine, "edge_voice": self.edge_voice},
            "vision_speak": bool(self.vision_task and not self.vision_task.done()),
        }


DAEMON = Daemon()


async def _on_start(app: web.Application) -> None:
    await DAEMON.discover_audio()
    await DAEMON.load_models()
    await DAEMON.ensure_hermes_session()
    # 启动恢复:config.json 大脑 preset 与 config.yaml 模型不一致(人手改/崩溃残局)
    # → 只报 drift 不自动选边(§5.5)。
    _bd = DAEMON.brain_drift()
    if _bd:
        DAEMON.emit("drift", axis="brain", desired=_bd["desired"],
                    applied=_bd["applied"],
                    message="config.json 大脑与 config.yaml 模型不一致,待人处置")
        print(f"[voice-daemon] brain drift at startup: {_bd}", flush=True)
    app["tasks"] = [
        asyncio.create_task(DAEMON.deadline_loop()),
        asyncio.create_task(DAEMON.audio_watch_loop()),
        asyncio.create_task(DAEMON.breaker_probe_loop()),
    ]
    # config 里 vision_speak=true 则起板端播报桥(不依赖 GUI 哪个 Tab 可见)
    if DAEMON.config.get("vision_speak"):
        await DAEMON.set_vision_speak(True)


async def _on_cleanup(app: web.Application) -> None:
    await DAEMON._abort_playback()
    await DAEMON.stop_capture()
    await DAEMON.set_vision_speak(False)
    for t in app.get("tasks", []):
        t.cancel()


def main() -> None:
    app = vhttp.make_app(DAEMON, TOKEN, MODELS, HERMES_ENV, _hermes_env_has)
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

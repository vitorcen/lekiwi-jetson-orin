"""Client for the LAN omni brain (Qwen3-Omni on a Mac).

The omni brain replaces the whole hermes turn: the user's raw audio goes to the
Mac, which returns either tool calls to execute here, or 24 kHz speech to play.
No ASR text is sent as the decision input and no TTS runs — the local ASR only
feeds the GUI transcript.

Two things this module deliberately does NOT do:

  * It does not import the MCP servers. `voice/setup.sh` installs neither `mcp`
    nor `pyzmq`, so `import drive.mcp_server` fails outright in this venv; and the
    per-process `_motion_lock` inside it would not be the same lock the hermes MCP
    child holds anyway. Cross-process motion arbitration is P3 work.
  * It therefore exposes READ-ONLY tools only. Nothing here can move the robot.

See docs/omni-brain-lan.html.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import uuid
import wave
from pathlib import Path

import aiohttp
import numpy as np


OMNI_TIMEOUT = float(os.environ.get("OMNI_TURN_TIMEOUT", "60"))
OMNI_CONNECT_TIMEOUT = 5.0
MAX_TOOL_ROUNDS = 4
MAX_TOOL_RESULT_CHARS = 4000

VLM_URL = os.environ.get("VLM_DAEMON_URL", "http://127.0.0.1:8090")
VLM_TOKEN_FILE = Path(__file__).resolve().parent.parent / "vlm" / "token"

# Spoken replies are heard, not read. Without this the model answers in bulleted
# markdown, and P0 measured first-audio at 12.8 s for those replies versus 2.6 s
# for short ones — reply length is a latency knob, not just a style preference.
SYSTEM = (
    "你是一个轮式机器人的大脑。用户对你说话时，如果需要查看画面或读取机器人状态，"
    "就调用相应的工具；否则直接用中文口语回答。\n"
    "回答必须简短：最多两句话，四十字以内。禁止列表、禁止分点、禁止markdown。"
    "不要复述用户的话。每次都只针对当前这句话回答，绝不重复你上一轮说过的内容。\n"
    # A robot that announces "没听清" on every noise burst is as annoying as one
    # that invents a story; either way it talks when nobody addressed it. The
    # sentinel lets it choose silence, and the two-stage split makes that free.
    "如果这段声音不是在对你说话——背景噪音、别人的对话、你自己的回声、听不清的片段——"
    "就只输出 <ignore> 这五个字符，不要输出任何其他内容，也不要解释。"
)


class OmniError(Exception):
    pass


class Ignored(Exception):
    """The model judged this audio was not addressed to the robot."""


# --------------------------------------------------------------------------- #
# Read-only tools
# --------------------------------------------------------------------------- #

def _vlm_token() -> str:
    try:
        return VLM_TOKEN_FILE.read_text().strip()
    except Exception:
        return ""


TOOLS = [
    {"type": "function", "function": {
        "name": "vlm_look",
        "description": "看一眼摄像头，返回画面内容的文字描述。想知道面前有什么就用它。",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "想问画面什么，可省略"},
            "camera": {"type": "string", "enum": ["front", "wrist"],
                       "description": "front=前置摄像头(默认)，wrist=手腕摄像头"}}}}},
    {"type": "function", "function": {
        "name": "vlm_last_caption",
        "description": "读取最近一次画面描述，不重新拍摄。",
        "parameters": {"type": "object", "properties": {}}}},
]


async def _vlm_call(session: aiohttp.ClientSession, path: str,
                    payload: dict | None) -> dict:
    headers = {}
    tok = _vlm_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    url = f"{VLM_URL}{path}"
    timeout = aiohttp.ClientTimeout(total=20)
    if payload is None:
        async with session.get(url, headers=headers, timeout=timeout) as r:
            return await r.json()
    async with session.post(url, json=payload, headers=headers, timeout=timeout) as r:
        return await r.json()


# Fields worth showing a language model. Everything else in a vlm-daemon reply is
# either telemetry or, in the case of `frame_b64`, an entire base64 JPEG — dumping
# the raw JSON fed the model thousands of characters of truncated base64, which it
# read as noise and answered by calling the tool again. Observed in the field as
# vlm_look → vlm_last_caption → vlm_look → … until the tool-round cap tripped.
_VLM_KEEP = ("text", "age_seconds", "notice", "disclaimer", "error", "detail", "hint")


def _project(payload) -> dict | str:
    if not isinstance(payload, dict):
        return payload
    return {k: v for k, v in payload.items() if k in _VLM_KEEP}


async def execute_tool(session: aiohttp.ClientSession, name: str, args: dict) -> dict:
    """Run one read-only tool. Always returns {ok, content} — a raised exception
    here would strand the model waiting for a result that never arrives."""
    try:
        if name == "vlm_look":
            payload = {"prompt": args.get("prompt") or "画面里有什么？",
                       "max_age_s": args.get("max_age_s", 5.0)}
            if args.get("camera"):
                payload["camera"] = args["camera"]
            return {"ok": True,
                    "content": _project(await _vlm_call(session, "/look", payload))}
        if name == "vlm_last_caption":
            return {"ok": True,
                    "content": _project(await _vlm_call(session, "/caption", None))}
    except asyncio.TimeoutError:
        return {"ok": False, "content": f"{name} timed out"}
    except Exception as exc:
        return {"ok": False, "content": f"{name} failed: {exc}"}
    # Unknown tool is a distinct failure from a tool that ran and errored: the
    # model needs to learn the name does not exist, not retry it.
    return {"ok": False, "content": f"unknown tool {name!r}"}


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #

def wav_b64(samples: np.ndarray, rate: int = 16000) -> str:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return base64.b64encode(buf.getvalue()).decode()


def wav_to_pcm(b64: str) -> tuple[bytes, int]:
    """-> (raw s16le frames, sample rate). aplay wants raw, not a container."""
    with wave.open(io.BytesIO(base64.b64decode(b64)), "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate()


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #

async def _iter_sse(resp):
    """Split on \\n by hand rather than using aiohttp's line iterator.

    Same reason as daemon._iter_sse: that iterator has a 512 KB read_bufsize cap
    and raises when one `data:` line exceeds it, killing the turn. An omni audio
    chunk is far bigger than a hermes text delta, so this is not hypothetical.
    """
    event, buf = "message", b""
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
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    yield event, json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue


# --------------------------------------------------------------------------- #
# One turn
# --------------------------------------------------------------------------- #

class OmniTurn:
    """Drives one user turn to completion, including any tool rounds.

    `on_audio(pcm_bytes, rate)` is awaited per chunk so playback starts on the
    first chunk instead of after the whole reply — that is the difference P0
    measured between 20.9 s and 2.6 s to first sound.
    `alive()` must return False once the turn is superseded (barge-in), so a
    stale turn stops both playing and issuing tool calls.
    """

    def __init__(self, url: str, token: str, speaker: str = "ethan",
                 conversation_id: str = "voice"):
        self.url = url.rstrip("/")
        self.token = token
        self.speaker = speaker
        self.conversation_id = conversation_id
        self.request_ids: list[str] = []

    def _headers(self) -> dict:
        h = {"Accept": "text/event-stream"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _post_sse(self, session, path: str, payload: dict, on_audio, alive,
                        on_text=None):
        rid = payload["request_id"]
        self.request_ids.append(rid)
        head: dict = {}
        async with session.post(f"{self.url}{path}", json=payload,
                                headers=self._headers()) as resp:
            if resp.status == 503:
                raise OmniError("omni busy")
            if resp.status >= 400:
                body = (await resp.text())[:200]
                raise OmniError(f"omni HTTP {resp.status}: {body}")
            async for event, data in _iter_sse(resp):
                if not alive():
                    # Superseded (barge-in). Stopping the local loop is not enough:
                    # the Mac keeps generating until it is told to stop, and the
                    # next turn would then bounce off a 503.
                    await self.cancel()
                    return head
                if event == "thinker.text":
                    head = data
                    if on_text and data.get("assistant_text"):
                        on_text(data["assistant_text"])
                elif event == "audio.chunk":
                    pcm, rate = wav_to_pcm(data["b64"])
                    await on_audio(pcm, rate)
                elif event == "error":
                    raise OmniError(f"{data.get('code')}: {data.get('message')}")
        return head

    async def run(self, samples, user_text: str, on_audio, alive,
                  on_tool=None, on_text=None) -> str:
        """-> the assistant text finally spoken (or "" if nothing was).

        `samples` may be None: the GUI's text box (POST /simulate) produces a turn
        with no audio at all, and refusing it would make the omni brain the only
        one you cannot type at.

        `on_text` fires the moment the reply text is known — BEFORE its audio
        plays. Barge-in echo detection compares the mic against recently spoken
        text, so registering it after playback (the obvious place) is too late:
        the robot hears itself, decides it was interrupted, and starts a turn that
        collides with the one still running.
        """
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=OMNI_CONNECT_TIMEOUT,
                                        sock_read=OMNI_TIMEOUT)
        payload = {
            "request_id": str(uuid.uuid4()),
            # The server keeps the transcript for this id. Holding it there rather
            # than resending it means a daemon restart cannot desync the history.
            "conversation_id": self.conversation_id,
            "system": SYSTEM,
            "user_text": user_text or None,     # history/log only, never model input
            "tools": TOOLS,
            "speaker": self.speaker,
        }
        if samples is not None:
            payload["audio"] = {"b64": wav_b64(samples)}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            head = await self._post_sse(session, "/v1/turn", payload,
                                        on_audio, alive, on_text)

            rounds = 0
            seen: set[str] = set()
            while head.get("status") == "tool_calls" and alive():
                rounds += 1
                if rounds > MAX_TOOL_ROUNDS:
                    raise OmniError(f"exceeded {MAX_TOOL_ROUNDS} tool rounds")
                calls = head.get("tool_calls") or []
                if on_tool:
                    for c in calls:
                        on_tool(c.get("name", "?"))
                results = []
                for c in calls:
                    # Re-running an identical call cannot produce a new answer, so
                    # tell the model that instead of burning a round on it. Without
                    # this, a reply the model finds unsatisfying turns into a loop
                    # that ends in the round cap and a "抱歉出错了" to the user.
                    sig = f"{c.get('name')}:{json.dumps(c.get('arguments') or {}, sort_keys=True)}"
                    if sig in seen:
                        results.append({"call_id": c.get("call_id"), "ok": False,
                                        "content": "重复调用，结果与上次相同，"
                                                   "请基于已有结果回答，不要再调用工具。"})
                        continue
                    seen.add(sig)
                    r = await execute_tool(session, c.get("name", ""),
                                           c.get("arguments") or {})
                    content = r["content"]
                    if not isinstance(content, str):
                        content = json.dumps(content, ensure_ascii=False)
                    if len(content) > MAX_TOOL_RESULT_CHARS:
                        content = content[:MAX_TOOL_RESULT_CHARS] + " …[truncated]"
                    results.append({"call_id": c.get("call_id"), "ok": r["ok"],
                                    "content": content})
                head = await self._post_sse(
                    session, f"/v1/turn/{head['turn_id']}/tool-results",
                    {"request_id": str(uuid.uuid4()), "results": results},
                    on_audio, alive, on_text)

            if head.get("status") == "ignored":
                # Deliberate silence, not a failure: no audio, no transcript row,
                # nothing committed to history. The caller counts it so the
                # operator can still see it happened.
                raise Ignored()
            if head.get("status") == "bad_tool_call":
                # Never spoken: reading a broken JSON blob aloud is the worst
                # possible failure here. The caller plays a fixed phrase instead.
                raise OmniError(f"bad_tool_call: {head.get('error')}")
            return head.get("assistant_text") or ""

    async def cancel(self) -> None:
        """Withdraw every request this turn issued. Cancelling the coroutine is
        NOT enough — MLX generation on the Mac is a sync loop that only stops when
        the server sees this."""
        if not self.request_ids:
            return
        timeout = aiohttp.ClientTimeout(total=3)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for rid in self.request_ids:
                    try:
                        await session.delete(f"{self.url}/v1/requests/{rid}",
                                             headers=self._headers())
                    except Exception:
                        pass
        except Exception:
            pass


async def health(url: str, token: str) -> dict:
    timeout = aiohttp.ClientTimeout(total=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(f"{url.rstrip('/')}/health", headers=headers) as r:
            return await r.json()


async def reset_conversation(url: str, token: str, conversation_id: str = "voice") -> dict:
    """Make the brain forget. Pairs with the GUI's 「新对话」: clearing the local
    transcript alone leaves the model still carrying the old context."""
    timeout = aiohttp.ClientTimeout(total=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(
                f"{url.rstrip('/')}/v1/conversations/{conversation_id}/reset",
                headers=headers) as r:
            return await r.json()

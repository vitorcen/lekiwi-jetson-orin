#!/usr/bin/env python3
"""LeKiwi VLM daemon — camera + llama.cpp caption, three-state power model.

HTTP API on 0.0.0.0:8090 (Bearer token). Camera frames grabbed on demand via
ffmpeg subprocess (no persistent handle -> device closed when idle). Captions
produced by a resident llama.cpp server (OpenAI-compatible) so first response is
fast. See README.md for the full contract.

Design rules baked in here (from docs/hermes-lekiwi-voice-agent-plan.html):
  - camera is CLOSED when idle (per-grab ffmpeg, power saving)
  - llama-server stays resident (idle keeps VRAM warm, zero GPU work otherwise)
  - every caption carries frame_ts (capture wall time) so stale obs is detectable
  - the caption is an UNTRUSTED observation; this daemon only produces/serves it
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from collections import deque

from aiohttp import web
import aiohttp

import vlm_models

# --------------------------------------------------------------------------- #
# Config (all overridable via env; sane defaults for this board)
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


HTTP_HOST = _env("VLM_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(_env("VLM_HTTP_PORT", "8090"))
LLAMA_URL = _env("VLM_LLAMA_URL", "http://127.0.0.1:8091").rstrip("/")
CAMERA_DEV = _env(
    "VLM_CAMERA",
    "/dev/v4l/by-id/usb-CN02KX4NLG0004ABK00_USB_Camera_"
    "CN02KX4NLG0004ABK00-video-index0",
)
FFMPEG = os.path.expanduser(_env("VLM_FFMPEG", "~/.local/bin/ffmpeg"))
VIDEO_SIZE = _env("VLM_VIDEO_SIZE", "1280x720")
# Watch cadence: PERIOD between caption starts, inference time INCLUDED — a
# 10 s period with 4 s inference sleeps 6 s, so the rate the user picks is the
# rate they get instead of drifting with model latency. Runtime-settable via
# POST /state; this is only the boot default.
WATCH_INTERVAL = float(_env("VLM_WATCH_INTERVAL", "10.0"))
WATCH_INTERVAL_MIN = 1.0
WATCH_INTERVAL_MAX = 300.0
DEMOTE_SECONDS = float(_env("VLM_DEMOTE_SECONDS", "90"))
LLAMA_TIMEOUT = float(_env("VLM_LLAMA_TIMEOUT", "30"))
GRAB_TIMEOUT = float(_env("VLM_GRAB_TIMEOUT", "10"))
CAPTURE_IDLE_STOP = float(_env("VLM_CAPTURE_IDLE_STOP", "10"))  # stop ffmpeg N s after last /frame.jpg poll
FPS_WINDOW = float(_env("VLM_FPS_WINDOW", "2.0"))               # rolling window for measured fps
THUMB_WIDTH = int(_env("VLM_THUMB_WIDTH", "320"))              # caption thumbnail width
TOKEN_FILE = os.path.expanduser(_env("VLM_TOKEN_FILE", os.path.join(HERE, "token")))
DEFAULT_PROMPT = _env(
    "VLM_DEFAULT_PROMPT", "用一句不超过40字的中文直接描述画面关键内容,不要开场白"
)
MODEL_NAME = _env("VLM_MODEL", "qwen3-vl")
MAX_TOKENS = int(_env("VLM_MAX_TOKENS", "80"))
INFER_WIDTH = int(_env("VLM_INFER_WIDTH", "640"))  # frame width sent to the VLM
LOOK_MAX_AGE = float(_env("VLM_LOOK_MAX_AGE", "5.0"))  # /look get-or-refresh default
CAPTION_RING = int(_env("VLM_CAPTION_RING", "16"))    # shared-slot ring buffer size

# Model switch: llama-server reads model paths from this EnvironmentFile; the
# daemon swaps a model by rewriting it (atomic) + restarting the unit, then
# polling llama /health and running a real-inference probe before committing.
MODELS_DIR = os.path.expanduser(_env("VLM_MODELS_DIR", "~/models/vlm"))
LLAMA_ENV_FILE = os.path.expanduser(
    _env("VLM_LLAMA_ENV", "~/.config/lekiwi/llama-model.env"))
LLAMA_UNIT = _env("VLM_LLAMA_UNIT", "llama-server")
LLAMA_READY_TIMEOUT = float(_env("VLM_LLAMA_READY_TIMEOUT", "90"))  # cold 3.4GB load is slow
MODEL_PROBE_TIMEOUT = float(_env("VLM_MODEL_PROBE_TIMEOUT", "30"))

START_TS = time.time()


def _load_token() -> str:
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    except FileNotFoundError:
        pass
    raise SystemExit(
        f"[vlm-daemon] token file missing/empty: {TOKEN_FILE}. "
        "Run install.sh (or: openssl rand -hex 24 > token && chmod 600 token)."
    )


TOKEN = _load_token()


# --------------------------------------------------------------------------- #
# Core daemon state
# --------------------------------------------------------------------------- #
class Daemon:
    def __init__(self) -> None:
        self.state = "idle"                 # "idle" | "watch"
        self.last_frame: bytes | None = None
        self.last_frame_ts: float = 0.0
        self.last_grab_ok: bool = False
        self.last_caption: dict | None = None   # SHARED slot: DEFAULT_PROMPT scene captions only
        self.last_answer: dict | None = None    # VQA slot: /describe + custom-prompt /look
        self.seq = 0
        # ring buffer of last N shared-slot captions (seq, frame_ts, text; no thumbs)
        self.captions: deque[dict] = deque(maxlen=CAPTION_RING)

        # fault bookkeeping for stale_reason diagnosis (read-time computed)
        self.last_capture_ok_ts: float = 0.0
        self.last_llama_ok_ts: float = 0.0
        self.last_camera_error: dict | None = None   # {detail, ts}
        self.last_llama_error: dict | None = None    # {detail, ts}

        # continuous capture (persistent ffmpeg MJPEG reader, latest frame only)
        self.capture_on = False
        self.capture_task: asyncio.Task | None = None
        self.capture_proc: asyncio.subprocess.Process | None = None
        self.last_frame_poll = 0.0          # wall time of last /frame.jpg poll
        self.frame_times: deque[float] = deque()  # recent frame timestamps (fps)

        # activity / demotion bookkeeping
        self.last_activity = time.time()
        self.sse_subscribers: set[asyncio.Queue] = set()

        # llama single-flight (never hit the server concurrently)
        self._llama_lock = asyncio.Lock()

        # model switch: reject concurrent /model calls (simple busy flag -> 409)
        self.model_switch_busy = False

        # coalescing: two independent latest-wins batches by sink, so a VQA
        # ("answer") request can never collapse into a shared scene-caption
        # ("shared") request. Each: one running + one queued batch.
        self._batches: dict[str, dict | None] = {"shared": None, "answer": None}
        self._running: dict[str, bool] = {"shared": False, "answer": False}

        # watch loop wakeup
        self._watch_wakeup = asyncio.Event()
        self.watch_interval = WATCH_INTERVAL   # live, settable via POST /state

    # -- activity -------------------------------------------------------- #
    def touch(self) -> None:
        """Register client activity (poll / describe / sse) -> defer demote."""
        self.last_activity = time.time()

    def promote_watch(self) -> None:
        if self.state != "watch":
            self.state = "watch"
        self.touch()
        self._watch_wakeup.set()

    def demote_idle(self) -> None:
        if self.state != "idle":
            self.state = "idle"

    def set_watch_interval(self, secs: float) -> float:
        """Clamp and apply a new watch period. Waking the loop makes it take
        effect on the current sleep, not only after the next caption."""
        self.watch_interval = max(WATCH_INTERVAL_MIN,
                                  min(WATCH_INTERVAL_MAX, float(secs)))
        self._watch_wakeup.set()
        return self.watch_interval

    # -- fault bookkeeping + staleness diagnosis ------------------------- #
    def _note_camera_error(self, detail: str) -> None:
        self.last_camera_error = {"detail": str(detail)[:300], "ts": time.time()}

    def _note_llama_error(self, detail: str) -> None:
        self.last_llama_error = {"detail": str(detail)[:300], "ts": time.time()}

    def stale_reason(self) -> str | None:
        """Diagnose why the shared caption may be stale, computed at read time.
        Precedence: camera-error / llama-error (error newer than last success)
        > watch-stalled > idle. Returns None when fresh or benign.
          - "camera-error": last capture attempt failed
          - "llama-error":  last inference attempt errored
          - "watch-stalled": state=watch but newest caption older than 3xINTERVAL
          - "idle": state=idle and caption older than 2xINTERVAL (normal, by design)
        """
        now = time.time()
        cam = self.last_camera_error
        if cam and cam["ts"] > self.last_capture_ok_ts:
            return "camera-error"
        lla = self.last_llama_error
        if lla and lla["ts"] > self.last_llama_ok_ts:
            return "llama-error"
        cap = self.last_caption
        cap_ts = cap.get("frame_ts") if cap else None
        age = (now - cap_ts) if isinstance(cap_ts, (int, float)) else None
        if self.state == "watch":
            if age is not None and age > 3 * self.watch_interval:
                return "watch-stalled"
            return None
        # idle
        if age is not None and age > 2 * self.watch_interval:
            return "idle"
        return None

    def last_error(self) -> dict | None:
        """Most-recent camera/llama fault as {kind, detail, ts}, else None."""
        errs = []
        if self.last_camera_error:
            errs.append(("camera", self.last_camera_error))
        if self.last_llama_error:
            errs.append(("llama", self.last_llama_error))
        if not errs:
            return None
        kind, e = max(errs, key=lambda kv: kv[1]["ts"])
        return {"kind": kind, "detail": e["detail"], "ts": round(e["ts"], 3)}

    # -- camera: one-shot grab (idle / capture-off fallback) ------------- #
    async def grab_frame(self) -> bytes:
        """Grab a single JPEG via ffmpeg (opens+closes the device). Raises on
        failure. Updates the frame cache on success. Used only when the
        continuous capture is off (no device double-open)."""
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", VIDEO_SIZE, "-i", CAMERA_DEV,
            "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=GRAB_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            self.last_grab_ok = False
            self._note_camera_error(f"camera grab timed out after {GRAB_TIMEOUT}s")
            raise RuntimeError(f"camera grab timed out after {GRAB_TIMEOUT}s")

        if proc.returncode != 0 or not out[:2] == b"\xff\xd8":
            self.last_grab_ok = False
            msg = err.decode("utf-8", "replace").strip()[:400]
            detail = f"ffmpeg grab failed rc={proc.returncode}: {msg or 'no jpeg data'}"
            self._note_camera_error(detail)
            raise RuntimeError(detail)

        self.last_frame = out
        self.last_frame_ts = time.time()
        self.last_grab_ok = True
        self.last_capture_ok_ts = self.last_frame_ts
        return out

    # -- camera: continuous capture (persistent ffmpeg MJPEG reader) ----- #
    def note_frame_poll(self) -> None:
        """A /frame.jpg poll refreshes the capture activity clock and starts the
        persistent capture task if it isn't running. Does NOT touch activity
        (no promote to watch) — frame polling is pure CPU, never GPU."""
        self.last_frame_poll = time.time()
        if self.capture_task is None or self.capture_task.done():
            self.capture_on = True
            self.capture_task = asyncio.create_task(self._capture_loop())

    def _on_capture_frame(self, frame: bytes) -> None:
        now = time.time()
        self.last_frame = frame
        self.last_frame_ts = now
        self.last_grab_ok = True
        self.last_capture_ok_ts = now
        self.frame_times.append(now)
        while self.frame_times and now - self.frame_times[0] > FPS_WINDOW:
            self.frame_times.popleft()

    def current_fps(self) -> float:
        """Measured capture fps over the last FPS_WINDOW seconds; 0 when off."""
        if not self.capture_on:
            return 0.0
        now = time.time()
        while self.frame_times and now - self.frame_times[0] > FPS_WINDOW:
            self.frame_times.popleft()
        return round(len(self.frame_times) / FPS_WINDOW, 1)

    async def _capture_loop(self) -> None:
        """Persistent ffmpeg reading MJPEG straight through (-c:v copy) to a
        pipe; split on JPEG SOI/EOI (FFD8..FFD9), keep only the latest frame.
        Stops (device closed, CPU freed) after CAPTURE_IDLE_STOP s with no
        /frame.jpg poll."""
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", VIDEO_SIZE, "-i", CAMERA_DEV,
            "-c:v", "copy", "-f", "image2pipe", "-",
        ]
        proc = None
        buf = bytearray()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.capture_proc = proc
            while True:
                if time.time() - self.last_frame_poll > CAPTURE_IDLE_STOP:
                    break
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(65536), timeout=2.0
                    )
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    break  # ffmpeg died / EOF
                buf += chunk
                # extract every complete JPEG present, keep only the last one
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        buf.clear()
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start > 0:
                            del buf[:start]
                        break
                    frame = bytes(buf[start:end + 2])
                    del buf[:end + 2]
                    self._on_capture_frame(frame)
        except Exception as exc:
            self.last_grab_ok = False
            self._note_camera_error(f"capture loop error: {type(exc).__name__}: {exc}")
        finally:
            self.capture_on = False
            self.last_frame = None       # force a fresh frame on next restart
            self.frame_times.clear()
            self.capture_proc = None
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass

    async def get_frame_for_serving(self) -> bytes:
        """/frame.jpg: prefer the live captured frame (waiting briefly for the
        first one after capture starts); fall back to a one-shot grab if capture
        is off/failed. Never promotes state."""
        if self.capture_on:
            deadline = time.time() + 5.0
            while (
                self.last_frame is None
                and self.capture_on
                and time.time() < deadline
            ):
                await asyncio.sleep(0.05)
            if self.last_frame is not None:
                return self.last_frame
        return await self.grab_frame()

    # -- JPEG downscale via ffmpeg (no new deps) ------------------------- #
    async def _scale_jpeg(self, jpeg: bytes, width: int) -> bytes | None:
        """Downscale a JPEG to `width` px wide, return JPEG bytes (or None)."""
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "error",
            "-f", "image2pipe", "-i", "pipe:0",
            "-vf", f"scale={width}:-2",
            "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(
                proc.communicate(jpeg), timeout=GRAB_TIMEOUT
            )
        except Exception:
            return None
        if proc.returncode == 0 and out[:2] == b"\xff\xd8":
            return out
        return None

    async def _thumbnail(self, jpeg: bytes) -> str | None:
        """Downscale to THUMB_WIDTH and return base64 (or None)."""
        out = await self._scale_jpeg(jpeg, THUMB_WIDTH)
        return base64.b64encode(out).decode() if out else None

    # -- llama client ---------------------------------------------------- #
    async def _llama_caption(self, jpeg: bytes, prompt: str) -> dict:
        """One VLM call. Returns {text} or {error, detail}."""
        # Vision-token count scales with input resolution; INFER_WIDTH px is
        # plenty for scene captioning and much faster to encode than 720p.
        scaled = await self._scale_jpeg(jpeg, INFER_WIDTH)
        data_uri = "data:image/jpeg;base64," + base64.b64encode(scaled or jpeg).decode()
        payload = {
            "model": MODEL_NAME,
            "max_tokens": MAX_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": data_uri}},
                    ],
                }
            ],
        }
        url = LLAMA_URL + "/v1/chat/completions"
        timeout = aiohttp.ClientTimeout(total=LLAMA_TIMEOUT)
        async with self._llama_lock:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.post(url, json=payload) as resp:
                        body = await resp.text()
                        if resp.status != 200:
                            return {
                                "error": "llama-server error",
                                "detail": f"HTTP {resp.status}: {body[:300]}",
                            }
                        data = json.loads(body)
                        text = (
                            data["choices"][0]["message"]["content"] or ""
                        ).strip()
                        return {"text": text}
            except aiohttp.ClientError as exc:
                return {
                    "error": "llama-server unreachable",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            except asyncio.TimeoutError:
                return {
                    "error": "llama-server timeout",
                    "detail": f"no response within {LLAMA_TIMEOUT}s",
                }
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                return {
                    "error": "llama-server bad response",
                    "detail": f"{type(exc).__name__}: {exc}",
                }

    async def caption_once(self, prompt: str) -> dict:
        """Caption a frame. Reuses the latest continuously-captured frame when
        capture is live (fresher + faster, no device reopen); one-shot grabs it
        otherwise. Attaches a downscaled thumbnail of the EXACT interpreted frame
        as base64 `frame_b64`. Always returns a structured dict carrying
        frame_ts + latency_ms; never raises."""
        t0 = time.time()
        try:
            if self.capture_on and self.last_frame is not None:
                jpeg = self.last_frame
                frame_ts = self.last_frame_ts
            else:
                jpeg = await self.grab_frame()
                frame_ts = self.last_frame_ts
        except Exception as exc:  # camera failure -> structured, no crash
            return {
                "error": "camera grab failed",
                "detail": str(exc),
                "frame_ts": None,
                "latency_ms": int((time.time() - t0) * 1000),
                "frame_b64": None,
            }

        res = await self._llama_caption(jpeg, prompt)
        if res.get("error"):
            self._note_llama_error(f"{res['error']}: {res.get('detail', '')}")
        else:
            self.last_llama_ok_ts = time.time()
        latency_ms = int((time.time() - t0) * 1000)
        res["frame_ts"] = frame_ts
        res["latency_ms"] = latency_ms
        res["frame_b64"] = await self._thumbnail(jpeg)
        return res

    # -- coalescing (bounded latest-wins per sink, never stacks) --------- #
    async def _coalesced(self, sink: str, prompt: str) -> dict:
        """Run caption_once(prompt) coalesced within `sink` ("shared"|"answer").
        In-flight + one queued batch; a newer request supersedes the queued
        prompt and rides the same result. GPU is serialized by _llama_lock."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        batch = self._batches[sink]
        if batch is None:
            self._batches[sink] = {"prompt": prompt, "futures": [fut]}
        else:
            batch["prompt"] = prompt
            batch["futures"].append(fut)
        if not self._running[sink]:
            asyncio.create_task(self._batch_worker(sink))
        return await fut

    async def _batch_worker(self, sink: str) -> None:
        if self._running[sink]:
            return
        self._running[sink] = True
        try:
            while self._batches[sink] is not None:
                batch = self._batches[sink]
                self._batches[sink] = None
                res = await self.caption_once(batch["prompt"])
                for f in batch["futures"]:
                    if not f.done():
                        f.set_result(res)
        finally:
            self._running[sink] = False

    async def refresh_shared(self) -> dict:
        """Run a DEFAULT_PROMPT scene caption and commit it to the SHARED slot
        (ring + SSE). Used by /look cache-miss; returns the stored event."""
        res = await self._coalesced("shared", DEFAULT_PROMPT)
        return self._store_caption(res)

    async def answer(self, prompt: str) -> dict:
        """Run a VQA / custom-prompt caption into the ISOLATED answer slot.
        Never touches the shared watch slot, ring, or SSE feed."""
        res = await self._coalesced("answer", prompt)
        self.last_answer = res
        return res

    async def look(self, max_age_s: float, prompt: str | None) -> dict:
        """get-or-refresh. Custom prompt -> isolated fresh answer (never cached).
        Default prompt -> return the shared caption if age <= max_age_s
        (cached:true, zero GPU), else refresh the shared slot (cached:false)."""
        if prompt:
            res = dict(await self.answer(prompt))
            res["cached"] = False
            return res
        cap = self.last_caption
        cap_ts = cap.get("frame_ts") if cap else None
        if (cap and cap.get("text")
                and isinstance(cap_ts, (int, float))
                and (time.time() - cap_ts) <= max_age_s):
            out = dict(cap)
            out["cached"] = True
            return out
        out = dict(await self.refresh_shared())
        out["cached"] = False
        return out

    # -- caption store + SSE fan-out (SHARED slot only) ------------------ #
    def _store_caption(self, res: dict) -> dict:
        """Commit a DEFAULT_PROMPT scene caption to the shared slot + ring + SSE.
        Only the watch loop and /look refresh call this; VQA never does."""
        self.seq += 1
        event = {
            "text": res.get("text"),
            "error": res.get("error"),
            "detail": res.get("detail"),
            "frame_ts": res.get("frame_ts"),
            "latency_ms": res.get("latency_ms"),
            "frame_b64": res.get("frame_b64"),
            "seq": self.seq,
        }
        # drop None keys for a clean payload but keep text/seq/frame_ts always
        event = {k: v for k, v in event.items()
                 if v is not None or k in ("text", "seq", "frame_ts")}
        self.last_caption = event
        if event.get("text"):  # ring holds successful captions only (no thumbs)
            self.captions.append({
                "seq": event["seq"],
                "frame_ts": event.get("frame_ts"),
                "text": event["text"],
            })
        for q in list(self.sse_subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    # -- background loops ------------------------------------------------ #
    async def watch_loop(self) -> None:
        while True:
            if self.state != "watch":
                self._watch_wakeup.clear()
                await self._watch_wakeup.wait()
                continue
            t0 = time.monotonic()
            res = await self.caption_once(DEFAULT_PROMPT)
            self._store_caption(res)
            # PERIOD, not gap: subtract the inference time just spent, so the
            # configured interval is the caption-to-caption rate. Inference
            # slower than the period -> back-to-back, never negative sleep.
            while True:
                left = self.watch_interval - (time.monotonic() - t0)
                if left <= 0:
                    break
                try:
                    # Wake early on state change; also on an interval change,
                    # which re-enters and re-measures against the NEW period
                    # (shortening it can end the sleep immediately).
                    await asyncio.wait_for(self._watch_wakeup.wait(), timeout=left)
                    self._watch_wakeup.clear()
                    if self.state != "watch":
                        break
                except asyncio.TimeoutError:
                    break

    async def demote_loop(self) -> None:
        while True:
            await asyncio.sleep(min(5.0, DEMOTE_SECONDS / 3 or 1))
            if self.state != "watch":
                continue
            if self.sse_subscribers:
                # an active subscriber keeps us warm
                self.touch()
                continue
            if time.time() - self.last_activity > DEMOTE_SECONDS:
                self.demote_idle()

    async def llama_up(self) -> bool:
        url = LLAMA_URL + "/health"
        timeout = aiohttp.ClientTimeout(total=1.5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url) as resp:
                    return resp.status == 200
        except Exception:
            return False

    # -- model switch: env-file rewrite + unit restart + real probe + revert -- #
    def active_model_file(self) -> str | None:
        """VLM_MODEL path named by the current llama env file, or None."""
        try:
            with open(LLAMA_ENV_FILE, "r", encoding="utf-8") as fh:
                return vlm_models.parse_env(fh.read()).get("VLM_MODEL")
        except OSError:
            return None

    def active_model_id(self) -> str | None:
        return vlm_models.active_model_id(self.active_model_file()) or MODEL_NAME

    @staticmethod
    def _atomic_write(path: str, text: str) -> None:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".llama-model.", suffix=".tmp")
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
    async def _restart_llama() -> int:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", LLAMA_UNIT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await proc.communicate()
        return proc.returncode if proc.returncode is not None else -1

    async def _llama_wait_ready(self, timeout: float) -> bool:
        """Poll llama /health until 200 or timeout (cold model load is slow)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if await self.llama_up():
                return True
            await asyncio.sleep(1.0)
        return False

    async def _text_probe(self, timeout: float) -> tuple[bool, str]:
        """1-token text completion straight to llama — proves the model loaded and
        infers even when no camera frame is available."""
        payload = {"model": MODEL_NAME, "max_tokens": 1,
                   "messages": [{"role": "user", "content": "回复:好"}]}
        url = LLAMA_URL + "/v1/chat/completions"
        cfg = aiohttp.ClientTimeout(total=timeout)
        try:
            async with self._llama_lock:
                async with aiohttp.ClientSession(timeout=cfg) as sess:
                    async with sess.post(url, json=payload) as resp:
                        body = await resp.text()
                        if resp.status != 200:
                            return False, f"HTTP {resp.status}: {body[:160]}"
                        data = json.loads(body)
                        data["choices"][0]["message"]  # raises if malformed
                        return True, "ok (text)"
        except asyncio.TimeoutError:
            return False, f"timeout >{timeout:g}s"
        except Exception as exc:                             # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    async def _model_probe(self, timeout: float) -> tuple[bool, str]:
        """Real inference on the freshly loaded model: prefer a live/one-shot
        camera frame (true vision path), fall back to a 1-token text probe when
        there is no frame. Passes only on real produced output."""
        jpeg = None
        try:
            if self.capture_on and self.last_frame is not None:
                jpeg = self.last_frame
            else:
                jpeg = await self.grab_frame()
        except Exception:                                    # noqa: BLE001
            jpeg = None
        if jpeg is not None:
            res = await self._llama_caption(jpeg, "用一句话描述画面")
            if res.get("error"):
                return False, f"{res['error']}: {res.get('detail', '')}"[:160]
            if res.get("text"):
                return True, "ok (vision)"
            return False, "empty caption"
        return await self._text_probe(timeout)

    async def switch_model(self, model_id: str) -> dict:
        """Swap the llama model: validate -> write env -> restart -> wait ready ->
        real probe. On failure restore the previous env, restart, re-probe the old
        model and report the REAL outcome (never leave the board sightless).
        Returns a dict; `http` (popped by the handler) carries the status code."""
        models = vlm_models.list_models(MODELS_DIR, self.active_model_file())
        target = next((m for m in models if m["id"] == model_id), None)
        if target is None:
            return {"status": "error", "error": f"unknown model: {model_id}",
                    "http": 404}
        if not target["usable"]:
            return {"status": "error",
                    "error": f"model has no paired mmproj: {model_id}", "http": 400}
        prev_env = None
        try:
            with open(LLAMA_ENV_FILE, "r", encoding="utf-8") as fh:
                prev_env = fh.read()
        except OSError:
            prev_env = None
        # the model we'd fall back to — captured BEFORE the env is overwritten
        prev_model_id = vlm_models.active_model_id(
            vlm_models.parse_env(prev_env or "").get("VLM_MODEL"))

        t0 = time.time()
        self._atomic_write(LLAMA_ENV_FILE,
                           vlm_models.build_env(target["file"], target["mmproj"]))
        rc = await self._restart_llama()
        ready = await self._llama_wait_ready(LLAMA_READY_TIMEOUT)
        load_s = round(time.time() - t0, 1)
        if ready:
            ok, reason = await self._model_probe(MODEL_PROBE_TIMEOUT)
        else:
            ok, reason = False, f"llama not ready in {LLAMA_READY_TIMEOUT:g}s (rc={rc})"
        if ok:
            print(f"[vlm-daemon] switched model -> {model_id} in {load_s}s ({reason})",
                  flush=True)
            return {"status": "ok", "active": model_id, "load_s": load_s,
                    "probe": reason, "http": 200}

        # failure: restore old env + restart + re-probe the old model
        old_id = prev_model_id or MODEL_NAME
        restored = False
        if prev_env is not None:
            try:
                self._atomic_write(LLAMA_ENV_FILE, prev_env)
                restored = True
            except OSError:
                restored = False
        await self._restart_llama()
        old_ready = await self._llama_wait_ready(LLAMA_READY_TIMEOUT)
        old_ok, old_reason = (await self._model_probe(MODEL_PROBE_TIMEOUT)
                              if old_ready else (False, "old model not ready"))
        status = "reverted" if (restored and old_ok) else "degraded"
        print(f"[vlm-daemon] model switch to {model_id} FAILED ({reason}); "
              f"{status}, old={old_id} probe={'ok' if old_ok else old_reason}",
              flush=True)
        return {"status": status, "error": reason, "active": old_id,
                "old_probe": ("ok" if old_ok else old_reason), "http": 200}


DAEMON = Daemon()


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
@web.middleware
async def auth_middleware(request: web.Request, handler):
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {TOKEN}"
    # constant-ish comparison; token is not a secret-of-secrets but be tidy
    if len(auth) != len(expected) or auth != expected:
        return web.json_response(
            {"error": "unauthorized"}, status=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await handler(request)


async def h_health(request: web.Request) -> web.Response:
    d = DAEMON
    return web.json_response({
        "state": d.state,
        "watch_interval": d.watch_interval,
        "llama_up": await d.llama_up(),
        "model": d.active_model_id(),
        "model_switch_busy": d.model_switch_busy,
        "camera": {"device": CAMERA_DEV, "last_ok": d.last_grab_ok},
        "camera_fps": d.current_fps(),      # measured; 0 when capture off
        "capture_on": d.capture_on,
        "last_caption_ts": (
            d.last_caption.get("frame_ts") if d.last_caption else None
        ),
        "stale_reason": d.stale_reason(),
        "last_error": d.last_error(),
        "uptime": round(time.time() - START_TS, 1),
    })


async def h_frame(request: web.Request) -> web.Response:
    # NB: /frame.jpg does NOT count as activity and must NOT promote to watch.
    # It DOES turn the continuous capture on and refresh its activity clock.
    DAEMON.note_frame_poll()
    try:
        jpeg = await DAEMON.get_frame_for_serving()
    except Exception as exc:
        return web.json_response(
            {"error": "camera grab failed", "detail": str(exc)}, status=503
        )
    return web.Response(
        body=jpeg,
        content_type="image/jpeg",
        headers={
            "X-Frame-Ts": f"{DAEMON.last_frame_ts:.3f}",
            "X-Fps": f"{DAEMON.current_fps():.1f}",
        },
    )


async def h_caption(request: web.Request) -> web.Response:
    DAEMON.touch()  # polling counts as activity (keeps watch warm)
    if DAEMON.last_caption is None:
        return web.json_response(
            {"error": "no caption yet", "stale_reason": DAEMON.stale_reason()},
            status=404,
        )
    out = dict(DAEMON.last_caption)
    out["stale_reason"] = DAEMON.stale_reason()
    return web.json_response(out)


async def h_captions(request: web.Request) -> web.Response:
    """Newest-first list of the last shared-slot captions (no thumbnails)."""
    DAEMON.touch()
    try:
        n = int(request.query.get("n", "8"))
    except ValueError:
        n = 8
    n = max(1, min(n, CAPTION_RING))
    now = time.time()
    items = list(DAEMON.captions)[-n:][::-1]  # newest first
    out = []
    for it in items:
        e = dict(it)
        ft = it.get("frame_ts")
        e["age_seconds"] = round(now - ft, 1) if isinstance(ft, (int, float)) else None
        out.append(e)
    return web.json_response({
        "captions": out,
        "stale_reason": DAEMON.stale_reason(),
    })


async def h_look(request: web.Request) -> web.Response:
    """get-or-refresh: body {prompt?, max_age_s?}. Default prompt returns the
    shared caption when fresh (cached:true, zero GPU) else refreshes it;
    a custom prompt always runs a fresh, isolated answer (cached:false)."""
    DAEMON.touch()
    prompt = None
    max_age_s = LOOK_MAX_AGE
    if request.can_read_body:
        try:
            body = await request.json()
            if isinstance(body, dict):
                if body.get("prompt"):
                    prompt = str(body["prompt"])
                if body.get("max_age_s") is not None:
                    max_age_s = float(body["max_age_s"])
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    res = await DAEMON.look(max_age_s, prompt)
    res["stale_reason"] = DAEMON.stale_reason()
    return web.json_response(res)


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

    q: asyncio.Queue = asyncio.Queue(maxsize=32)
    d.sse_subscribers.add(q)
    # NB: an SSE subscriber NO LONGER auto-promotes to watch. The GUI button
    # (POST /state) is now the only normal promoter; the no-activity auto-demote
    # (demote_loop) still applies as a safety net.
    try:
        # replay the latest caption immediately so a new client isn't blank
        if d.last_caption is not None:
            await resp.write(
                f"data: {json.dumps(d.last_caption, ensure_ascii=False)}\n\n"
                .encode("utf-8")
            )
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=15.0)
                await resp.write(
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    .encode("utf-8")
                )
            except asyncio.TimeoutError:
                # keepalive comment; also detects dead peer
                await resp.write(b": keepalive\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        d.sse_subscribers.discard(q)
        if not d.sse_subscribers:
            # last subscriber gone -> start the 90s demote countdown
            d.touch()
    return resp


async def h_describe(request: web.Request) -> web.Response:
    DAEMON.touch()
    prompt = DEFAULT_PROMPT
    if request.can_read_body:
        try:
            body = await request.json()
            if isinstance(body, dict) and body.get("prompt"):
                prompt = str(body["prompt"])
        except (json.JSONDecodeError, ValueError):
            pass
    # VQA burst -> isolated answer slot. Does NOT overwrite the shared watch
    # caption slot / ring / SSE feed (the GUI ask box reads this response
    # directly). The shared slot only ever holds DEFAULT_PROMPT scene captions.
    res = dict(await DAEMON.answer(prompt))
    res["stale_reason"] = DAEMON.stale_reason()
    return web.json_response(res)


async def h_models(request: web.Request) -> web.Response:
    """List models under MODELS_DIR (mmproj paired, size measured, active marked)."""
    active = DAEMON.active_model_file()
    return web.json_response({
        "models": vlm_models.list_models(MODELS_DIR, active),
        "active_file": active,
        "busy": DAEMON.model_switch_busy,
    })


async def h_model_post(request: web.Request) -> web.Response:
    """Switch the llama model to {id}. Synchronous (cold load can take 90s+): the
    caller gets the real outcome. Rejects concurrent switches with 409."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid json"}, status=400)
    model_id = body.get("id")
    if not model_id or not isinstance(model_id, str):
        return web.json_response({"error": "id (model id) required"}, status=400)
    if DAEMON.model_switch_busy:
        return web.json_response({"error": "model switch in progress"}, status=409)
    DAEMON.model_switch_busy = True
    try:
        res = await DAEMON.switch_model(model_id)
    finally:
        DAEMON.model_switch_busy = False
    status = res.pop("http", 200)
    return web.json_response(res, status=status)


async def h_state(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "invalid json"}, status=400)
    want = body.get("state")
    if want is not None and want not in ("idle", "watch"):
        return web.json_response(
            {"error": "state must be 'idle' or 'watch'"}, status=400
        )
    # "interval" may come alone (retune while watching) or with a state change.
    iv = body.get("interval")
    if iv is not None:
        try:
            DAEMON.set_watch_interval(float(iv))
        except (TypeError, ValueError):
            return web.json_response({"error": "interval must be a number"},
                                     status=400)
    if want == "watch":
        DAEMON.promote_watch()
    elif want == "idle":
        DAEMON.demote_idle()
    return web.json_response({"state": DAEMON.state,
                              "interval": DAEMON.watch_interval})


def make_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/health", h_health)
    app.router.add_get("/frame.jpg", h_frame)
    app.router.add_get("/caption", h_caption)
    app.router.add_get("/captions", h_captions)
    app.router.add_get("/events", h_events)
    app.router.add_post("/describe", h_describe)
    app.router.add_post("/look", h_look)
    app.router.add_post("/state", h_state)
    app.router.add_get("/models", h_models)
    app.router.add_post("/model", h_model_post)
    return app


async def _on_start(app: web.Application) -> None:
    app["watch_task"] = asyncio.create_task(DAEMON.watch_loop())
    app["demote_task"] = asyncio.create_task(DAEMON.demote_loop())


async def _on_cleanup(app: web.Application) -> None:
    for key in ("watch_task", "demote_task"):
        task = app.get(key)
        if task:
            task.cancel()


def main() -> None:
    app = make_app()
    app.on_startup.append(_on_start)
    app.on_cleanup.append(_on_cleanup)
    print(
        f"[vlm-daemon] listening on {HTTP_HOST}:{HTTP_PORT} "
        f"llama={LLAMA_URL} camera={CAMERA_DEV} "
        f"watch_interval={DAEMON.watch_interval}s demote={DEMOTE_SECONDS}s",
        flush=True,
    )
    web.run_app(app, host=HTTP_HOST, port=HTTP_PORT, print=None)


if __name__ == "__main__":
    main()

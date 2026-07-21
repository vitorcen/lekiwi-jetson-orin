"""HTTP layer (:8092, Bearer), extracted from daemon.py (pure movement). Handlers
are thin adapters over the Daemon public API. daemon.py is the entry script
(__main__), so this module must NEVER import daemon — all daemon-side references
are injected once via make_app(); module globals below hold them."""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

import voice_brain as vbrain
import voice_config as vconfig
import voice_vad as vvad
from voice_switching import IDLE, DEBUG

D = None            # Daemon instance, set by make_app()
TOKEN = ""
MODELS = ""         # models dir (vad availability check)
HERMES_ENV = ""     # .env path (error message only; key values never leave the board)
_hermes_env_has = lambda name: False    # noqa: E731  (injected)


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
    return web.json_response(D.health())


async def h_state(request: web.Request) -> web.Response:
    h = D.health()
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
    await D.do_listen(window_s)
    return web.json_response({"state": D.state,
                              "window_deadline": round(D.deadline, 1)})


async def h_stop(request: web.Request) -> web.Response:
    await D.do_stop()
    return web.json_response({"state": D.state})


async def h_interrupt(request: web.Request) -> web.Response:
    await D.do_interrupt()
    return web.json_response({"state": D.state})


async def h_say(request: web.Request) -> web.Response:
    body = await _json_body(request)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "text required"}, status=400)
    await D.do_say(text)
    return web.json_response({"ok": True, "state": D.state})


async def h_simulate(request: web.Request) -> web.Response:
    """调试:把一段文本当作 ASR 定稿直接送 Hermes 走完整一轮(GUI/自测用,
    绕过真实麦克风)。"""
    body = await _json_body(request)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "text required"}, status=400)
    gen = D.generation
    D.turn_id += 1
    D.emit("user_text", text=text)
    D.turn_task = asyncio.create_task(D.run_turn(gen, text))
    return web.json_response({"ok": True})


async def h_config_get(request: web.Request) -> web.Response:
    """GET /config → 全量 desired + applied + drift + 各轴枚举(含 edge 音色表)。"""
    return web.json_response(D.config_view())


async def h_config_post(request: web.Request) -> web.Response:
    """POST /config:按轴整体替换。engine 轴(asr/tts)→ 202+job(进度走 feed);
    vision_speak → 立即落盘 + 起停桥;brain → P2(400)。"""
    body = await _json_body(request)
    axis = body.get("axis")
    value = body.get("value")
    ephemeral = bool(body.get("ephemeral"))
    if axis == "brain":
        return web.json_response({"error": "brain switch uses POST /brain"},
                                 status=400)
    if axis == "vision_speak":
        D.config = vconfig.apply_axis(D.config, "vision_speak", value)
        try:
            vconfig.save_config(D.config)
        except OSError as exc:
            return web.json_response({"error": f"config save failed: {exc}"},
                                     status=500)
        await D.set_vision_speak(bool(value))
        return web.json_response({"ok": True, "vision_speak": bool(value)})
    if axis == "stream":
        # DEBUG 流式模式开关 + 模型 + 端点静音(ephemeral 不落盘 / 否则存参)。模型可能
        # 700M,同步载入会超 GUI HTTP 超时 → 后台加载、立即返回,GUI 轮询 /health.stream.loaded。
        want = vconfig.normalize_stream(value)
        asyncio.create_task(D.apply_stream(value, ephemeral))
        return web.json_response({"enabled": want["enabled"], "model": want["model"],
                                  "endpoint_silence_s": want["endpoint_silence_s"],
                                  "state": "loading"})
    if axis == "vision_speak_limit":
        D.config = vconfig.apply_axis(D.config, "vision_speak_limit", value)
        try:
            vconfig.save_config(D.config)
        except OSError as exc:
            return web.json_response({"error": f"config save failed: {exc}"},
                                     status=500)
        D.caption_dedup.limit = D._vision_limit()   # 即时对在跑的桥生效
        return web.json_response({"ok": True,
                                  "vision_speak_limit": D._vision_limit()})
    if axis == "vision":
        model_id = value.get("model") if isinstance(value, dict) else value
        if not model_id or not isinstance(model_id, str):
            return web.json_response(
                {"error": "vision axis needs value {model:<id>}"}, status=400)
        job_id = D.new_job_id()
        if not D.switcher.try_begin(job_id):
            return web.json_response(
                {"error": "switch in progress", "job_id": D.switcher.job_id},
                status=409)
        asyncio.create_task(D.switch_vision(model_id, job_id))
        return web.json_response({"job_id": job_id, "state": "switching"}, status=202)
    if axis == "audio":
        gain = value.get("gain_db") if isinstance(value, dict) else value
        applied = D.apply_audio_gain(gain, ephemeral)
        return web.json_response({"ok": True, "gain_db": applied,
                                  "ephemeral": ephemeral})
    if axis == "vad":
        engine = value.get("engine") if isinstance(value, dict) else value
        if engine not in vconfig.VAD_ENGINES:
            return web.json_response({"error": f"unknown vad engine: {engine}"},
                                     status=400)
        # 不可用引擎明确拒绝,不静默换别的(硬纪律)。
        if not vvad.availability(MODELS).get(engine):
            return web.json_response(
                {"error": f"vad engine unavailable on this board: {engine}"},
                status=400)
        job_id = D.new_job_id()
        if not D.switcher.try_begin(job_id):
            return web.json_response(
                {"error": "switch in progress", "job_id": D.switcher.job_id},
                status=409)
        asyncio.create_task(D.switch_vad(value, ephemeral, job_id))
        return web.json_response({"job_id": job_id, "state": "switching"}, status=202)
    if axis not in ("asr", "tts"):
        return web.json_response({"error": f"unknown axis: {axis}"}, status=400)
    job_id = D.new_job_id()
    if not D.switcher.try_begin(job_id):
        return web.json_response(
            {"error": "switch in progress", "job_id": D.switcher.job_id},
            status=409)
    asyncio.create_task(D.switch_engine(axis, value, ephemeral, job_id))
    return web.json_response({"job_id": job_id, "state": "switching"}, status=202)


async def h_brain(request: web.Request) -> web.Response:
    """POST /brain {preset}:大脑 preset 切换 → 202+job(流程见 §5.5,进度走 feed)。
    前置(同步、拒则不建 job):preset 存在、state∈{IDLE,DEBUG}、preset 校验过、
    key_env 在 .env 存在非空。任一不过 → 400/409 明确原因,key 值永不出现。"""
    body = await _json_body(request)
    preset_name = body.get("preset")
    presets = D.config.get("presets") or {}
    if preset_name not in presets:
        return web.json_response({"error": f"unknown preset: {preset_name}"},
                                 status=400)
    if D.state not in (IDLE, DEBUG):
        return web.json_response(
            {"error": "先停对话再切大脑", "state": D.state}, status=409)
    preset = presets[preset_name]
    try:
        vbrain.validate_preset(preset_name, preset)
    except vbrain.BrainError as exc:
        return web.json_response({"error": f"preset invalid: {exc}"}, status=400)
    key_env = preset.get("key_env")
    if not _hermes_env_has(key_env):
        return web.json_response(
            {"error": f"{key_env} 未在 {HERMES_ENV} 配置或为空 —— 先手工写入 key 再切"},
            status=409)
    job_id = D.new_job_id()
    if not D.switcher.try_begin(job_id):
        return web.json_response(
            {"error": "switch in progress", "job_id": D.switcher.job_id},
            status=409)
    asyncio.create_task(D.switch_brain(preset_name, job_id))
    return web.json_response({"job_id": job_id, "state": "switching"}, status=202)


async def h_asr_debug(request: web.Request) -> web.Response:
    """POST /asr_debug {on:1|0}:进/出 DEBUG 转写台(与对话互斥)。"""
    body = await _json_body(request)
    on = body.get("on")
    on = str(on).lower() not in ("0", "false", "no", "none", "") if on is not None else True
    await D.set_debug(on)
    return web.json_response({"state": D.state, "debug": D.state == DEBUG})


async def h_asr_debug_tail(request: web.Request) -> web.Response:
    """GET /asr_debug/tail?since=<seq> → 转写增量(独立环,不挤 200 条 feed 环)。"""
    try:
        since = int(request.query.get("since", "0"))
    except ValueError:
        since = 0
    return web.json_response(D.debug_tail.since(since))


async def h_asr_debug_seg(request: web.Request) -> web.Response:
    """GET /asr_debug/seg?id=<seg_id> → {wav_b64}(16k mono wav base64)。段已被环形
    覆盖/不存在 → 404。供转写台段行「▶ 听」回放。"""
    try:
        sid = int(request.query.get("id", "0"))
    except ValueError:
        sid = 0
    b64 = D.seg_store.read_b64(sid)
    if b64 is None:
        return web.json_response({"error": "segment not found", "id": sid},
                                 status=404)
    return web.json_response({"wav_b64": b64})


async def h_asr_debug_seg_asr(request: web.Request) -> web.Response:
    """POST /asr_debug/seg_asr {id} → 用当前 ASR 宿主重识别已存段,返回
    {id, engine, text}。切模型后对同一段重跑,并排比引擎效果。段不存在 404。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        sid = int(body.get("id", 0))
    except (ValueError, TypeError):
        sid = 0
    result = await D.retranscribe_seg(sid)
    status = result.pop("status", 200)
    return web.json_response(result, status=status)


async def h_asr_debug_seg_play(request: web.Request) -> web.Response:
    """POST /asr_debug/seg_play {id} → 板上 aplay 放段(机器人音响),播放期闸采集防回录。
    段不存在 404;音响不可用 409;aplay 非零 500。供转写台段行「▶ 听」板上播。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        sid = int(body.get("id", 0))
    except (ValueError, TypeError):
        sid = 0
    result = await D.play_seg(sid)
    status = result.pop("status", 200)
    return web.json_response(result, status=status)


async def h_selftest(request: web.Request) -> web.Response:
    """POST /selftest → 已知人声 wav 喂 VAD+ASR 全链(绕过麦克风),返回
    {vad_segments, asr_text, expected, ratio, pass}。MCP01 不在也能跑。"""
    return web.json_response(await D.run_selftest())


async def h_feed(request: web.Request) -> web.Response:
    """GET /feed?since=<seq> → {events:[...], last_seq:N, oldest_seq:N}。since 缺省=0
    返回全部现存;oldest_seq 让 GUI 检测事件丢失(环被覆盖)。GUI 走 Rust 代理无法
    直连 SSE,以 2-3Hz 轮询增量。"""
    try:
        since = int(request.query.get("since", "0"))
    except ValueError:
        since = 0
    events = [e for e in D.feed_ring if e["seq"] > since]
    oldest = D.feed_ring[0]["seq"] if D.feed_ring else 0
    return web.json_response({"events": events, "last_seq": D.feed_seq,
                              "oldest_seq": oldest})


async def h_events(request: web.Request) -> web.StreamResponse:
    d = D
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


def make_app(daemon, token: str, models: str, hermes_env: str,
             hermes_env_has) -> web.Application:
    """Build the aiohttp app. Injects the daemon + board facts into module globals
    (single writer, set once before the loop serves)."""
    global D, TOKEN, MODELS, HERMES_ENV, _hermes_env_has
    D = daemon
    TOKEN = token
    MODELS = models
    HERMES_ENV = hermes_env
    _hermes_env_has = hermes_env_has
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/health", h_health)
    app.router.add_get("/state", h_state)
    app.router.add_post("/listen", h_listen)
    app.router.add_post("/stop", h_stop)
    app.router.add_post("/interrupt", h_interrupt)
    app.router.add_post("/say", h_say)
    app.router.add_post("/simulate", h_simulate)
    app.router.add_get("/config", h_config_get)
    app.router.add_post("/config", h_config_post)
    app.router.add_post("/brain", h_brain)
    app.router.add_post("/asr_debug", h_asr_debug)
    app.router.add_get("/asr_debug/tail", h_asr_debug_tail)
    app.router.add_get("/asr_debug/seg", h_asr_debug_seg)
    app.router.add_post("/asr_debug/seg_play", h_asr_debug_seg_play)
    app.router.add_post("/asr_debug/seg_asr", h_asr_debug_seg_asr)
    app.router.add_post("/selftest", h_selftest)
    app.router.add_get("/feed", h_feed)
    app.router.add_get("/events", h_events)
    return app

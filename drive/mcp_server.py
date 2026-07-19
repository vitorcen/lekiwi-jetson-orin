#!/usr/bin/env python3
"""LeKiwi DRIVE — stdio MCP server (CONSTRAINED base control) for Hermes.

Exposes a small, hard-clamped base-driving skill that speaks the base_host ZMQ
wire contract directly:

  base_host (systemd `base_host.service`) binds  PULL  tcp://*:5555
  each command is one JSON string: {"x.vel": m/s, "y.vel": m/s, "theta.vel": deg/s}
    x = forward+ , y = left+ , theta = counter-clockwise+
  base_host has a 0.5 s watchdog: no command for 0.5 s -> auto-brake. So any
  sustained motion MUST be re-sent at >=10 Hz and end with a zero frame.

SAFETY MODEL — this file is LAYER 2 of 4: SCHEMA HARD CLAMP.
  The tool inputSchema advertises the limits AND the code re-clamps every value
  before it ever reaches the wire, so a mis-behaving Agent (or prompt injection)
  cannot exceed them. Limits: |vx|,|vy| <= 0.15 m/s ; |omega| <= 30 deg/s ;
  0.1 <= duration <= 2.0 s. On top of that: a process-wide motion mutex (no
  queueing — a second concurrent call gets `busy`), a >=0.3 s cooldown between
  moves (anti chain-call distance farming), and a stop event so drive_stop can
  pre-empt an in-flight drive_move.

  NOTE: this server is NOT mounted on Hermes by default. Validate it via the
  GUI / CLI first; mounting into the Hermes robot profile is a separate,
  deliberate mainline decision.

Tool results mimic vlm/mcp_server.py: clear JSON text, and drive_move/drive_stop
carry an arm-state reminder (if the follower arm is `holding`, it is torque-
locked and must not be assumed limp).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import zmq
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ---- wire / endpoint ------------------------------------------------------
ZMQ_TARGET = os.environ.get("LEKIWI_BASE_ADDR", "tcp://127.0.0.1:5555")
ARM_FILE = os.environ.get("LEKIWI_ARM_FILE", "/tmp/lekiwi_arm")
BATT_FILE = os.environ.get("LEKIWI_BATT_FILE", "/tmp/lekiwi_batt")

# ---- LAYER-2 hard clamps (also advertised in the schema) ------------------
VX_MAX = 0.15          # m/s, |vx| and |vy|
VY_MAX = 0.15          # m/s
OMEGA_MAX = 30.0       # deg/s
DUR_MIN, DUR_MAX = 0.1, 2.0   # seconds

SEND_HZ = 20.0                 # re-send rate during a move (> 10 Hz watchdog)
SEND_PERIOD = 1.0 / SEND_HZ    # 0.05 s
BRAKE_FRAMES, BRAKE_GAP = 3, 0.03   # zero frames after a move
STOP_FRAMES, STOP_GAP = 5, 0.02     # zero frames on drive_stop
COOLDOWN_S = 0.3               # forced gap between drive_move calls

# ---- shared state ---------------------------------------------------------
_ctx = zmq.Context.instance()
_sock = _ctx.socket(zmq.PUSH)
_sock.setsockopt(zmq.LINGER, 0)
_sock.setsockopt(zmq.SNDHWM, 10)      # PUSH w/o peer buffers up to 10, then EAGAIN
_sock.setsockopt(zmq.SNDTIMEO, 0)     # non-blocking sends (belt-and-braces w/ NOBLOCK)
_sock.connect(ZMQ_TARGET)

_send_lock = threading.Lock()         # zmq sockets are NOT thread-safe
_motion_lock = threading.Lock()       # motion mutex — non-blocking acquire => busy
_stop_event = threading.Event()       # set by drive_stop to pre-empt a move
_last_move_end = 0.0                  # monotonic ts of last drive_move completion


def _send(vx: float, vy: float, om: float) -> bool:
    """Send one base frame. Returns False if it couldn't be queued (unreachable)."""
    # src tag feeds base_host's priority mux: pad > gui > mcp (we are lowest).
    msg = json.dumps({"src": "mcp", "x.vel": vx, "y.vel": vy, "theta.vel": om})
    with _send_lock:
        try:
            _sock.send_string(msg, flags=zmq.NOBLOCK)
            return True
        except zmq.Again:
            return False


def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return "unknown"


def _arm_notice() -> tuple[str, str | None]:
    """(arm_state, human reminder or None)."""
    arm = _read_file(ARM_FILE) or "unknown"
    if arm == "holding":
        return arm, "臂处于 holding(伺服上锁保持姿态),不可假设其松弛;底盘移动前确认臂不会碰撞"
    if arm == "limp":
        return arm, "臂 limp(掉力,可手动搬动)"
    if arm == "none":
        return arm, "未检测到臂"
    return arm, None


def _run_move(vx: float, vy: float, om: float, dur: float) -> dict:
    """Blocking send loop (runs in a worker thread). 20 Hz re-send for `dur`,
    then BRAKE_FRAMES zero frames. Exits early if _stop_event is set."""
    n = max(1, round(dur / SEND_PERIOD))
    sent_ok = 0
    unreachable = False
    interrupted = False
    for _ in range(n):
        if _stop_event.is_set():
            interrupted = True
            break
        if _send(vx, vy, om):
            sent_ok += 1
        else:
            unreachable = True
        time.sleep(SEND_PERIOD)
    # Always brake, even on interruption / unreachable.
    for _ in range(BRAKE_FRAMES):
        _send(0.0, 0.0, 0.0)
        time.sleep(BRAKE_GAP)
    return {
        "frames_sent": sent_ok,
        "frames_requested": n,
        "interrupted": interrupted,
        "unreachable": unreachable,
    }


# ---- MCP server -----------------------------------------------------------
server = Server("lekiwi-drive")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="drive_move",
            description=(
                "Drive the LeKiwi base for a short, HARD-CLAMPED burst. "
                "Values are clamped server-side to: |vx_mps|<=0.15, "
                "|vy_mps|<=0.15 (x=forward+, y=left+), |omega_dps|<=30 "
                "(counter-clockwise+), duration_s in [0.1, 2.0]. Blocks until "
                "the burst finishes (re-sends at 20 Hz, then brakes). Only one "
                "move at a time: a concurrent call returns `busy`. A >=0.3 s "
                "cooldown is enforced between moves (returns `cooldown` if too "
                "soon). Returns the actually-executed (post-clamp) parameters "
                "plus an arm-state reminder."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "vx_mps": {
                        "type": "number",
                        "description": "Forward(+)/back(-) velocity, m/s. CLAMPED to [-0.15, 0.15].",
                    },
                    "vy_mps": {
                        "type": "number",
                        "description": "Left(+)/right(-) velocity, m/s. CLAMPED to [-0.15, 0.15].",
                    },
                    "omega_dps": {
                        "type": "number",
                        "description": "Yaw rate, deg/s, counter-clockwise+. CLAMPED to [-30, 30].",
                    },
                    "duration_s": {
                        "type": "number",
                        "description": "Burst duration, seconds. CLAMPED to [0.1, 2.0].",
                    },
                },
                "required": ["vx_mps", "vy_mps", "omega_dps", "duration_s"],
            },
        ),
        Tool(
            name="drive_stop",
            description=(
                "Immediately stop the base: pre-empts any in-flight drive_move "
                "and sends 5 zero frames. Safe to call any time."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="drive_status",
            description=(
                "Report drive skill status: arm torque state (/tmp/lekiwi_arm), "
                "servo-pack voltage (/tmp/lekiwi_batt), the ZMQ target address, "
                "and whether a move is currently in progress. Read-only."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


def _text(payload: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global _last_move_end
    arguments = arguments or {}

    if name == "drive_status":
        moving = _motion_lock.locked()
        arm_state, arm_msg = _arm_notice()
        return _text({
            "arm": arm_state,
            "battery_v": _read_file(BATT_FILE),
            "zmq_target": ZMQ_TARGET,
            "moving": moving,
            "arm_notice": arm_msg,
        })

    if name == "drive_stop":
        _stop_event.set()
        # send STOP_FRAMES zero frames off the event loop
        def _do_stop() -> int:
            ok = 0
            for _ in range(STOP_FRAMES):
                if _send(0.0, 0.0, 0.0):
                    ok += 1
                time.sleep(STOP_GAP)
            return ok
        sent = await asyncio.to_thread(_do_stop)
        arm_state, arm_msg = _arm_notice()
        return _text({
            "stopped": True,
            "zero_frames_sent": sent,
            "base_reachable": sent > 0,
            "arm": arm_state,
            "arm_notice": arm_msg,
        })

    if name == "drive_move":
        # required params
        try:
            req_vx = float(arguments["vx_mps"])
            req_vy = float(arguments["vy_mps"])
            req_om = float(arguments["omega_dps"])
            req_dur = float(arguments["duration_s"])
        except (KeyError, TypeError, ValueError) as exc:
            return _text({"error": "bad arguments", "detail": str(exc)})

        # cooldown gate (before taking the lock)
        now = time.monotonic()
        since = now - _last_move_end
        if since < COOLDOWN_S:
            return _text({
                "error": "cooldown",
                "detail": f"两次 drive_move 需间隔 >= {COOLDOWN_S}s;还需等待 "
                          f"{round(COOLDOWN_S - since, 3)}s",
                "retry_after_s": round(COOLDOWN_S - since, 3),
            })

        # motion mutex — no queueing
        if not _motion_lock.acquire(blocking=False):
            return _text({
                "error": "busy",
                "detail": "已有 drive_move 在执行;不排队,请稍后重试或先 drive_stop",
            })
        try:
            # LAYER-2 hard clamp
            vx = _clamp(req_vx, -VX_MAX, VX_MAX)
            vy = _clamp(req_vy, -VY_MAX, VY_MAX)
            om = _clamp(req_om, -OMEGA_MAX, OMEGA_MAX)
            dur = _clamp(req_dur, DUR_MIN, DUR_MAX)
            clamped = {
                "vx_mps": vx != req_vx,
                "vy_mps": vy != req_vy,
                "omega_dps": om != req_om,
                "duration_s": dur != req_dur,
            }
            _stop_event.clear()
            result = await asyncio.to_thread(_run_move, vx, vy, om, dur)
            _last_move_end = time.monotonic()
        finally:
            _motion_lock.release()

        arm_state, arm_msg = _arm_notice()
        payload = {
            "executed": {
                "vx_mps": vx, "vy_mps": vy, "omega_dps": om, "duration_s": dur,
            },
            "requested": {
                "vx_mps": req_vx, "vy_mps": req_vy,
                "omega_dps": req_om, "duration_s": req_dur,
            },
            "clamped": clamped,
            "any_clamped": any(clamped.values()),
            "frames_sent": result["frames_sent"],
            "frames_requested": result["frames_requested"],
            "interrupted": result["interrupted"],
            "base_reachable": result["frames_sent"] > 0,
            "arm": arm_state,
            "arm_notice": arm_msg,
        }
        if result["unreachable"]:
            payload["warning"] = (
                "部分/全部帧无法送达 base_host(PUSH 缓冲已满,SNDHWM=10):"
                "base_host 可能未运行,轮子可能未动"
            )
        return _text(payload)

    return _text({"error": f"unknown tool: {name}"})


async def _amain() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()

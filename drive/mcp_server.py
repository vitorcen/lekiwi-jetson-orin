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

Also carries the read-only `imu_read` tool: a one-shot snapshot of the 10-DOF
IMU via rosbridge (:9090) — attitude/heading, gyro/accel, raw mag counts,
temperature, pressure, ISA altitude. imu_10dof.py owns the serial port, so the
/imu/* topics are the only sanctioned read path.
"""

from __future__ import annotations

import asyncio
import json
import math
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
ROSBRIDGE_URL = os.environ.get("LEKIWI_ROSBRIDGE", "ws://127.0.0.1:9090")
MOTION_ADDR = os.environ.get("LEKIWI_MOTION_ADDR", "tcp://127.0.0.1:5560")
ACK_ADDR = os.environ.get("LEKIWI_BASE_FEEDBACK", "tcp://127.0.0.1:5557")

# /imu/* topics published by ros2/imu_10dof.py (temp/pressure capped at 2 Hz,
# so the snapshot timeout must comfortably exceed 0.5 s).
IMU_TOPICS = {
    "imu": ("/imu/data", "sensor_msgs/msg/Imu"),
    "mag": ("/imu/mag", "sensor_msgs/msg/MagneticField"),
    "temp": ("/imu/temp", "sensor_msgs/msg/Temperature"),
    "pressure": ("/imu/pressure", "sensor_msgs/msg/FluidPressure"),
}
IMU_SNAPSHOT_TIMEOUT_S = 3.0

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

# base_host feedback (:5557): per-applied-frame acks + 2 Hz state heartbeat.
# This is the truth channel — "sent to ZMQ" is NOT "wheels moved". Only read
# under _motion_lock (single mover at a time), so no extra locking.
_ack = _ctx.socket(zmq.SUB)
_ack.setsockopt_string(zmq.SUBSCRIBE, "")
_ack.setsockopt(zmq.RCVHWM, 64)
_ack.connect(ACK_ADDR)
_seq_lock = threading.Lock()
_seq_n = 0

_send_lock = threading.Lock()         # zmq sockets are NOT thread-safe
_motion_lock = threading.Lock()       # motion mutex — non-blocking acquire => busy
_stop_event = threading.Event()       # set by drive_stop to pre-empt a move
_last_move_end = 0.0                  # monotonic ts of last drive_move completion


def _send(vx: float, vy: float, om: float) -> bool:
    """Send one base frame. Returns False if it couldn't be queued (unreachable)."""
    global _seq_n
    with _seq_lock:
        _seq_n += 1
        seq = _seq_n
    # src tag feeds base_host's priority mux: pad > gui > ros > mcp (lowest).
    # ts+ttl let base_host drop this frame instead of replaying it stale.
    msg = json.dumps({"src": "mcp", "seq": seq, "ts": time.time(),
                      "ttl_s": 0.25, "x.vel": vx, "y.vel": vy, "theta.vel": om})
    with _send_lock:
        try:
            _sock.send_string(msg, flags=zmq.NOBLOCK)
            return True
        except zmq.Again:
            return False


def _drain_acks() -> list[dict]:
    out = []
    while True:
        try:
            out.append(json.loads(_ack.recv_string(zmq.NOBLOCK)))
        except zmq.Again:
            return out
        except ValueError:
            continue


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
    then BRAKE_FRAMES zero frames. Exits early if _stop_event is set.
    Counts base_host acks: applied == wheels actually driven, sent != applied
    when muted (pad/gui hold, motion switch off) or base_host is down."""
    _drain_acks()                       # flush stale feedback
    t0 = time.time()
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
    time.sleep(0.1)                     # let the last acks arrive
    acks = _drain_acks()
    applied = sum(1 for a in acks if a.get("type") == "ack"
                  and a.get("owner") == "mcp" and a.get("ts", 0) >= t0)
    motion_on = next((a.get("motion_on") for a in reversed(acks)
                      if a.get("type") == "state"), None)
    return {
        "frames_sent": sent_ok,
        "frames_requested": n,
        "frames_applied": applied,
        "motion_on": motion_on,
        "interrupted": interrupted,
        "unreachable": unreachable,
    }


# ---- motion controller (yaw closed loop) ----------------------------------
# ros2/motion_controller.py owns the feedback loop; this side only submits
# goals and polls status over a local ZMQ REQ. Fresh socket per call so a
# dead controller can never wedge REQ state.

def _motion_req(obj: dict, timeout_ms: int = 800) -> dict:
    s = _ctx.socket(zmq.REQ)
    s.setsockopt(zmq.LINGER, 0)
    s.setsockopt(zmq.RCVTIMEO, timeout_ms)
    s.setsockopt(zmq.SNDTIMEO, timeout_ms)
    try:
        s.connect(MOTION_ADDR)
        s.send_string(json.dumps(obj))
        return json.loads(s.recv_string())
    except (zmq.ZMQError, ValueError):
        return {"error": "motion_controller unreachable",
                "hint": "闭环转向不可用;可显式用 drive_move 开环(误差按 v×t 估计)"}
    finally:
        s.close()


async def _run_turn(angle: float) -> dict:
    sub = await asyncio.to_thread(_motion_req, {"op": "turn_by", "angle_deg": angle})
    if "error" in sub:
        return sub
    deadline = time.monotonic() + max(3.0, abs(angle) / 25.0 * 2 + 2.0) + 2.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        st = await asyncio.to_thread(_motion_req, {"op": "status"})
        if "error" in st:
            return st
        if not st.get("active"):
            return st.get("last_result") or {"error": "no result"}
    await asyncio.to_thread(_motion_req, {"op": "stop"}, 300)
    return {"error": "turn poll timeout", "detail": "controller 未在预期时间内结束"}


# ---- IMU read (rosbridge one-shot) ----------------------------------------
# imu_10dof.py owns the serial port; the only sanctioned read path is the four
# /imu/* ROS topics via rosbridge (:9090). One websocket, subscribe all four,
# wait until each arrived once (or timeout), unsubscribe, close.

def imu_euler_deg(w: float, x: float, y: float, z: float) -> dict:
    """Quaternion -> ZYX euler in degrees (REP-103, same math as the GUI)."""
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(_clamp(2 * (w * y - z * x), -1.0, 1.0))
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return {"roll_deg": math.degrees(roll), "pitch_deg": math.degrees(pitch),
            "yaw_deg": math.degrees(yaw)}


def imu_heading_deg(yaw_deg: float) -> float:
    """REP-103 CCW yaw -> compass-style heading (0..360, clockwise)."""
    return ((-yaw_deg) % 360 + 360) % 360


def isa_altitude_m(pressure_pa: float) -> float:
    """Barometric altitude, ISA standard atmosphere (same formula as the GUI)."""
    return 44330.0 * (1 - (pressure_pa / 101325.0) ** (1 / 5.255))


def imu_payload(msgs: dict) -> dict:
    """Build the imu_read result from raw topic messages (pure, testable).
    `msgs` maps the IMU_TOPICS keys to rosbridge `msg` dicts; missing keys mean
    that topic never arrived within the timeout."""
    out: dict = {"missing": sorted(set(IMU_TOPICS) - set(msgs))}
    imu = msgs.get("imu")
    if imu:
        q = imu["orientation"]
        e = imu_euler_deg(q["w"], q["x"], q["y"], q["z"])
        gv, av = imu["angular_velocity"], imu["linear_acceleration"]
        out["orientation"] = {
            **{k: round(v, 1) for k, v in e.items()},
            "heading_deg": round(imu_heading_deg(e["yaw_deg"]), 1),
        }
        out["angular_velocity_dps"] = {
            k: round(math.degrees(gv[k]), 2) for k in ("x", "y", "z")}
        out["linear_acceleration_mps2"] = {
            k: round(av[k], 3) for k in ("x", "y", "z")}
        out["heading_note"] = (
            "heading 来自 IMU 融合四元数,0° 为上电参考方向,未标定为绝对地磁北;"
            "磁力计为原始计数(量纲未标定),仅可看比值/变化趋势"
        )
    mag = msgs.get("mag")
    if mag:
        mf = mag["magnetic_field"]
        out["magnetic_raw"] = {k: round(mf[k], 0) for k in ("x", "y", "z")}
    temp = msgs.get("temp")
    if temp:
        out["temperature_c"] = round(temp["temperature"], 1)
    press = msgs.get("pressure")
    if press:
        pa = press["fluid_pressure"]
        out["pressure_hpa"] = round(pa / 100.0, 1)
        out["altitude_m_isa"] = round(isa_altitude_m(pa), 1)
    return out


async def _imu_snapshot(timeout_s: float = IMU_SNAPSHOT_TIMEOUT_S) -> dict:
    """Collect one message per /imu/* topic over rosbridge. Returns the
    imu_payload dict, or {"error": ...} if rosbridge is unreachable."""
    import aiohttp  # only needed for imu_read; vlm/.venv ships it

    by_topic = {t: k for k, (t, _) in IMU_TOPICS.items()}
    msgs: dict = {}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(ROSBRIDGE_URL, timeout=aiohttp.ClientWSTimeout(ws_close=5)) as ws:
                for key, (topic, typ) in IMU_TOPICS.items():
                    await ws.send_json({"op": "subscribe", "topic": topic,
                                        "type": typ, "queue_length": 1})
                deadline = time.monotonic() + timeout_s
                while len(msgs) < len(IMU_TOPICS):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    try:
                        m = await ws.receive_json(timeout=left)
                    except (asyncio.TimeoutError, TypeError, ValueError):
                        break
                    key = by_topic.get(m.get("topic"))
                    if key and m.get("op") == "publish":
                        msgs.setdefault(key, m["msg"])
                for topic, _ in IMU_TOPICS.values():
                    await ws.send_json({"op": "unsubscribe", "topic": topic})
    except (aiohttp.ClientError, OSError) as exc:
        return {"error": "rosbridge unreachable",
                "detail": f"{ROSBRIDGE_URL}: {exc}",
                "hint": "板上 rosbridge / imu-10dof user 服务可能未运行"}
    if not msgs:
        return {"error": "no imu data",
                "detail": f"rosbridge 已连接但 {timeout_s}s 内无 /imu/* 消息",
                "hint": "imu-10dof 服务可能未运行或 IMU 未接"}
    return imu_payload(msgs)


# ---- MCP server -----------------------------------------------------------
server = Server("lekiwi-drive")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="drive_move",
            description=(
                "LOW-LEVEL OPEN-LOOP (velocity x time) base burst — for "
                "rotation prefer turn_by (IMU closed loop, far more "
                "accurate); use this for short translations or as explicit "
                "degraded mode when the motion controller / IMU is down. "
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
            name="turn_by",
            description=(
                "Rotate the base IN PLACE by a relative angle with IMU "
                "closed-loop control — MUCH more accurate than drive_move for "
                "turning (repeatability a few degrees). angle_deg > 0 turns "
                "LEFT (counter-clockwise), < 0 turns RIGHT; clamped to ±180 "
                "per call. Blocks until the turn settles (typically 2-8 s) "
                "and returns the measured rotation and final error. Fails "
                "honestly (sensor_stale / preempted_by_human / no_progress / "
                "...) instead of silently falling back to open-loop; only "
                "then consider drive_move."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "angle_deg": {
                        "type": "number",
                        "description": "Relative rotation, degrees. >0 = left/CCW, <0 = right/CW. CLAMPED to [-180, 180].",
                    },
                },
                "required": ["angle_deg"],
            },
        ),
        Tool(
            name="motion_status",
            description=(
                "Read the closed-loop motion controller state: active goal, "
                "last goal result, and IMU feedback freshness (imu_ok). "
                "Read-only; use it to decide between closed-loop turn_by and "
                "open-loop drive_move."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="imu_read",
            description=(
                "Read the robot's 10-DOF IMU snapshot (read-only): attitude "
                "roll/pitch/yaw and compass-style heading (degrees, from the "
                "fused quaternion — 0° is the power-on reference, NOT true "
                "magnetic north), gyro (deg/s), accelerometer (m/s²), raw "
                "magnetometer counts, temperature (°C), barometric pressure "
                "(hPa) and ISA-derived altitude (m). Takes up to ~3 s (waits "
                "for the slow 2 Hz baro topic)."
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

    if name == "imu_read":
        return _text(await _imu_snapshot())

    if name == "motion_status":
        return _text(await asyncio.to_thread(_motion_req, {"op": "status"}))

    if name == "turn_by":
        try:
            angle = float(arguments["angle_deg"])
        except (KeyError, TypeError, ValueError) as exc:
            return _text({"error": "bad arguments", "detail": str(exc)})
        angle = _clamp(angle, -180.0, 180.0)
        if not _motion_lock.acquire(blocking=False):
            return _text({"error": "busy",
                          "detail": "已有运动在执行;不排队,可先 drive_stop"})
        try:
            result = await _run_turn(angle)
        finally:
            _motion_lock.release()
        arm_state, arm_msg = _arm_notice()
        result["arm"] = arm_state
        if arm_msg:
            result["arm_notice"] = arm_msg
        return _text(result)

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
        # Also cancel any closed-loop goal (best effort, controller may be down).
        await asyncio.to_thread(_motion_req, {"op": "stop"}, 300)
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
            "frames_applied": result["frames_applied"],
            "wheels_driven": result["frames_applied"] > 0,
            "interrupted": result["interrupted"],
            "arm": arm_state,
            "arm_notice": arm_msg,
        }
        if result["frames_applied"] == 0:
            if result["motion_on"] is False:
                payload["warning"] = (
                    "轮子没有动:安全开关(motion)关闭 — 需在 GUI/手柄打开"
                    "运动开关;重试任何 move 都不会动"
                )
            else:
                payload["warning"] = (
                    "轮子可能没有动:命令未被 base_host 应用(被手柄/GUI 抢占,"
                    "或 base_host 未运行)"
                )
        elif result["unreachable"]:
            payload["warning"] = (
                "部分帧无法送达 base_host(PUSH 缓冲已满):执行可能不完整"
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

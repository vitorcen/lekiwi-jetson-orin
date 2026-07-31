#!/usr/bin/env python3
"""LeKiwi MOTION ACTIONS — stdio MCP server for Hermes.

FOUR fixed tools, forever: list / play / stop / status. Actions are DATA, not
API — saving a new recording makes it appear in the next `motion_action_list`
with no YAML edit, no MCP reload, no daemon restart. There is deliberately no
tool per action (a tool table the client may or may not refresh must never be
a correctness precondition) and deliberately NO delete tool, so no prompt
injection can destroy a recording.

This process is a THIN CLIENT. It never opens a serial port, never streams
frames, and holds no scheduler: `play` is one blocking request on base_host's
local Unix socket, and if this process dies the socket closes and base_host
aborts the action. Safety — leases, calibration limits, the master motor
switch, the base speed ceiling — lives entirely in base_host.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motion_actions import control, store

server = Server("lekiwi-motion-actions")


def _text(payload):
    return [TextContent(type="text",
                        text=json.dumps(payload, ensure_ascii=False, indent=2))]


def _call(request, timeout):
    try:
        return control.call(request, timeout=timeout)
    except control.ControlError as exc:
        return {"ok": False, "error": "host_unreachable", "detail": str(exc),
                "hint": "base_host is not running; no motion is possible"}


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="motion_action_list",
            description=(
                "List the robot's recorded motion actions. Read-only, and the "
                "ONLY way to learn which actions exist — always call this "
                "before motion_action_play instead of guessing an id. Each "
                "entry carries action_id, scope (arm / base / full), label, "
                "description and duration_ms; files that fail validation are "
                "reported separately under `invalid` and are never playable."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="motion_action_play",
            description=(
                "Play one recorded action by id and BLOCK until it finishes. "
                "No speed factor, no repeat, no joint override: it replays "
                "exactly as recorded. The action ranks below every human and "
                "ROS input — touching the gamepad, keyboard, leader arm or a "
                "ROS motion goal pre-empts the WHOLE action, stops the base "
                "and does not resume. Returns result: succeeded | preempted | "
                "stopped | safety_cut | aborted, with `by` naming who took "
                "over. Errors: unknown_action, busy, recording, safety_off, "
                "arm_unavailable, calibrating. Never report 'done' on an "
                "error or a preemption."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "action_id": {
                        "type": "string",
                        "description": "Exact id from motion_action_list.",
                    },
                },
                "required": ["action_id"],
            },
        ),
        Tool(
            name="motion_action_stop",
            description=(
                "Stop the running action immediately; the base is forced to "
                "zero speed and the arm holds its current pose. Idempotent — "
                "safe to call when nothing is playing."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="motion_action_status",
            description=(
                "Read-only: what is recording or playing, progress, who "
                "currently owns the base and the arm, the master motor switch, "
                "arm readiness and the last action result."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name, arguments):
    arguments = arguments or {}

    if name == "motion_action_list":
        root = store.actions_root()
        return _text({"ok": True, **store.list_actions(root)})

    if name == "motion_action_play":
        action_id = arguments.get("action_id")
        if not store.valid_action_id(action_id):
            return _text({"ok": False, "error": "invalid_action_id",
                          "detail": "call motion_action_list for valid ids"})
        payload = await asyncio.to_thread(
            _call, {"op": "play", "action_id": action_id}, control.PLAY_TIMEOUT_S)
        return _text(payload)

    if name == "motion_action_stop":
        return _text(await asyncio.to_thread(
            _call, {"op": "stop", "by": "mcp"}, control.DEFAULT_TIMEOUT_S))

    if name == "motion_action_status":
        return _text(await asyncio.to_thread(
            _call, {"op": "status"}, control.DEFAULT_TIMEOUT_S))

    return _text({"ok": False, "error": "unknown_tool", "detail": str(name)})


async def _amain():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    asyncio.run(_amain())


if __name__ == "__main__":
    main()

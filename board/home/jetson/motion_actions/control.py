"""Client end of base_host's local control socket.

A Unix SOCK_STREAM socket in systemd's `RuntimeDirectory=lekiwi` — filesystem
permissions are the whole auth story, so there is no unauthenticated TCP port
on the LAN. One JSON request line in, one JSON reply line out, then close.

`play` is deliberately a LONG request: the reply only arrives when playback
finishes or is pre-empted, and **closing the connection aborts the action**.
That is what makes "the MCP server died" mean "the robot stops" without any
TTL bookkeeping on the wire.
"""
from __future__ import annotations

import json
import os
import socket

SOCKET_PATH = os.environ.get("LEKIWI_CONTROL_SOCK", "/run/lekiwi/base_host.sock")

# The timeout LADDER, in one place because only the ordering matters:
#   longest legitimate job (15 s action + glide)  <  JOB_MAX_WAIT_S
#                                                 <  PLAY_TIMEOUT_S
# The server must give up FIRST, so a wedged main loop answers a structured
# `control_timeout` instead of letting the client's socket expire and report
# "host_unreachable" — which would be a lie, and the wrong thing to go fix.
PLAY_TIMEOUT_S = 40.0
JOB_MAX_WAIT_S = 30.0
DEFAULT_TIMEOUT_S = 5.0


class ControlError(Exception):
    """base_host is unreachable. Never downgraded into a fake success."""


def call(request, timeout=DEFAULT_TIMEOUT_S, path=SOCKET_PATH):
    """Send one request, wait for the single reply object."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as exc:                                  # pragma: no cover
        raise ControlError(str(exc)) from None
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(path)
        except OSError as exc:
            raise ControlError(
                f"cannot reach base_host control socket {path}: {exc}") from None
        try:
            sock.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
            chunks = []
            while b"\n" not in b"".join(chunks):
                part = sock.recv(65536)
                if not part:
                    break
                chunks.append(part)
        except OSError as exc:
            raise ControlError(f"control socket i/o failed: {exc}") from None
        line = b"".join(chunks).split(b"\n", 1)[0]
        if not line:
            raise ControlError("base_host closed the control socket without replying")
        try:
            return json.loads(line.decode("utf-8"))
        except ValueError as exc:
            raise ControlError(f"bad reply from base_host: {exc}") from None
    finally:
        sock.close()

"""Server end of base_host's local control socket.

Threads here do exactly one thing: carry bytes. Every decision — leases,
recording, playback, file writes — happens on base_host's single main-loop
thread, which drains `take()` once per tick. That is what makes "atomically
acquire both the base and the arm lease" a plain `if` instead of a
distributed-locking problem.
"""
from __future__ import annotations

import json
import os
import select
import socket
import threading
import time

from .control import JOB_MAX_WAIT_S      # the one timeout ladder; see control.py

MAX_REQUEST_BYTES = 64 * 1024
ACCEPT_BACKLOG = 8
POLL_S = 0.1


class Job:
    """One control request awaiting a decision from the main loop."""

    def __init__(self, request):
        self.request = request
        self.op = request.get("op")
        self.result = None
        self.cancelled = False          # client hung up: abort whatever it started
        self._done = threading.Event()

    def finish(self, payload):
        """Complete the job. Idempotent — a late finish after abort is ignored."""
        if self._done.is_set():
            return False
        self.result = payload
        self._done.set()
        return True

    @property
    def finished(self):
        return self._done.is_set()

    def wait(self, timeout):
        return self._done.wait(timeout)


def _peer_gone(conn):
    """True once the client closed its end (MCP process died, ssh dropped)."""
    try:
        readable, _, _ = select.select([conn], [], [], 0)
        if not readable:
            return False
        return conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except BlockingIOError:
        return False
    except OSError:
        return True


class ControlServer:
    """Accepts local clients and queues their requests for the main loop."""

    def __init__(self, path):
        self.path = path
        self._pending = []
        self._lock = threading.Lock()
        self._sock = None
        self._live = 0                  # connection threads still holding a client

    def start(self):
        """Bind and listen. Raises OSError — the caller decides how loud to be."""
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(self.path):
            os.unlink(self.path)                 # stale socket from a crash
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(self.path)
        os.chmod(self.path, 0o600)               # owner-only: this is the auth
        sock.listen(ACCEPT_BACKLOG)
        self._sock = sock
        threading.Thread(target=self._accept_loop, name="control-accept",
                         daemon=True).start()
        return self

    def close(self, drain_s=0.0):
        """Stop accepting, then let already-decided replies reach their clients.

        The connection threads are daemons, so without the drain a shutdown
        reply loses the race with interpreter exit and the client sees an EOF
        instead of the result base_host just computed for it.
        """
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        # Nothing will drain the queue again: refuse what never got a decision
        # rather than leaving those clients to time out.
        for job in self.take():
            job.finish({"ok": False, "error": "host_stopped",
                        "detail": "base_host shut down before this request ran"})
        deadline = time.monotonic() + drain_s
        while self._live > 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def take(self):
        """Hand every queued job to the main loop and clear the queue."""
        with self._lock:
            jobs, self._pending = self._pending, []
        return jobs

    # ---- internals --------------------------------------------------------

    def _accept_loop(self):
        while self._sock is not None:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                self._live += 1
            threading.Thread(target=self._serve, args=(conn,),
                             name="control-conn", daemon=True).start()

    def _serve(self, conn):
        try:
            request = self._read_request(conn)
            if request is None:
                return
            job = Job(request)
            with self._lock:
                self._pending.append(job)
            waited = 0.0
            while not job.wait(POLL_S):
                waited += POLL_S
                if _peer_gone(conn):
                    job.cancelled = True
                if waited >= JOB_MAX_WAIT_S:
                    job.cancelled = True
                    job.finish({"ok": False, "error": "control_timeout",
                                "detail": "base_host did not answer in time"})
            self._send(conn, job.result)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                self._live -= 1

    def _read_request(self, conn):
        conn.settimeout(POLL_S * 50)
        buf = b""
        while b"\n" not in buf:
            try:
                part = conn.recv(4096)
            except OSError:
                return None
            if not part:
                return None
            buf += part
            if len(buf) > MAX_REQUEST_BYTES:
                self._send(conn, {"ok": False, "error": "request_too_large"})
                return None
        conn.settimeout(None)
        try:
            request = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(conn, {"ok": False, "error": "bad_request",
                              "detail": str(exc)})
            return None
        if not isinstance(request, dict) or not isinstance(request.get("op"), str):
            self._send(conn, {"ok": False, "error": "bad_request",
                              "detail": "request needs a string `op`"})
            return None
        return request

    @staticmethod
    def _send(conn, payload):
        try:
            conn.sendall(
                (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass

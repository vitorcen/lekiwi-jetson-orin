#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mlx-whisper>=0.4.0", "numpy"]
# ///
"""Mac-side ASR for the board's `remote` engine (voice/voice_engines.py RemoteAsr).

Why this exists: the Orin fits a 0.6B recognizer; this Mac fits a 1.5B one and has
no thermal budget problem doing it. The board ships each VAD segment here and gets
text back, so the ASR axis becomes "which machine", not just "which small model".

Wire shape is OpenAI's, deliberately:

    POST /v1/audio/transcriptions   multipart/form-data, field `file` = 16k mono wav
                                    optional fields: model, language
      -> 200 {"text": "...", "model": "...", "ms": 123}
    GET  /health                    -> {"ok": true, "model": "...", "loaded": bool}

so whisper.cpp server / LM Studio / vLLM can replace it without touching the board.

Run it (uv fetches mlx-whisper into an ephemeral env — no venv to maintain):

    uv run scripts/mac_asr_server.py --port 8094

Weights land in the normal Hugging Face cache (~/.cache/huggingface/hub, or
$HF_HOME if set) — the same place every other model on this Mac already lives.

Deliberately no auth: it is a LAN transcription box, it stores nothing, and adding
a token here would mean putting that token in the board's config file. Bind it to
an interface you trust (--host) if that is not good enough.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

DEFAULT_MODEL = "mlx-community/whisper-large-v3-mlx"
MAX_BODY = 32 * 1024 * 1024          # 32MB — a VAD segment is measured in KB

# MLX has one GPU queue; two concurrent transcribes just interleave badly.
_LOCK = threading.Lock()
_ARGS = None


def wav_to_float32(blob: bytes) -> np.ndarray:
    """16-bit PCM wav -> float32 [-1,1] mono. No ffmpeg dependency: the board is
    the only client and it always sends 16k mono s16le."""
    with wave.open(io.BytesIO(blob), "rb") as w:
        frames = w.readframes(w.getnframes())
        channels = w.getnchannels()
        width = w.getsampwidth()
    if width != 2:
        raise ValueError(f"expected 16-bit pcm, got {width * 8}-bit")
    arr = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        arr = arr.reshape(-1, channels).mean(axis=1)
    return arr


def parse_multipart(ctype: str, body: bytes) -> tuple[dict, bytes | None]:
    """Minimal multipart/form-data reader: returns (text fields, file bytes).

    Not a general implementation — it assumes the file part carries `filename=`,
    which every OpenAI-shaped client sends. Anything else raises rather than
    silently transcribing a truncated buffer.
    """
    m = re.search(r'boundary="?([^";]+)"?', ctype or "")
    if not m:
        raise ValueError("no multipart boundary")
    sep = b"--" + m.group(1).encode()
    fields, blob = {}, None
    for part in body.split(sep):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        head, _, payload = part.partition(b"\r\n\r\n")
        if not _:
            continue
        payload = payload.rstrip(b"\r\n")
        disp = head.decode("utf-8", "replace")
        name = re.search(r'name="([^"]*)"', disp)
        if not name:
            continue
        if "filename=" in disp:
            blob = payload
        else:
            fields[name.group(1)] = payload.decode("utf-8", "replace").strip()
    return fields, blob


def transcribe(samples: np.ndarray, model: str, language: str) -> str:
    import mlx_whisper
    with _LOCK:
        out = mlx_whisper.transcribe(samples, path_or_hf_repo=model,
                                     language=language or None,
                                     condition_on_previous_text=False)
    return (out.get("text") or "").strip()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                    # noqa: N802
        if self.path.split("?")[0] in ("/health", "/"):
            self._send(200, {"ok": True, "model": _ARGS.model,
                             "busy": _LOCK.locked()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):                                   # noqa: N802
        if self.path.split("?")[0] != "/v1/audio/transcriptions":
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            self._send(400, {"error": f"bad content-length: {n}"})
            return
        body = self.rfile.read(n)
        t0 = time.time()
        try:
            fields, blob = parse_multipart(self.headers.get("Content-Type", ""), body)
            if not blob:
                raise ValueError("no file part")
            samples = wav_to_float32(blob)
            # The board picks the model per request only if it was told one; the
            # server's --model stays the default so a board typo cannot make this
            # process download a 3GB repo.
            text = transcribe(samples, _ARGS.model, fields.get("language", "zh"))
        except Exception as exc:                          # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._send(200, {"text": text, "model": _ARGS.model,
                         "ms": int((time.time() - t0) * 1000)})

    def log_message(self, fmt, *args):                    # quieter default log
        print("[mac-asr] " + fmt % args, flush=True)


def main() -> None:
    global _ARGS
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8094)
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="Hugging Face repo id of an MLX whisper conversion")
    ap.add_argument("--warm", action="store_true",
                    help="load the model at startup (first real request is then fast)")
    _ARGS = ap.parse_args()
    if _ARGS.warm:
        print(f"[mac-asr] warming {_ARGS.model} …", flush=True)
        transcribe(np.zeros(16000, dtype=np.float32), _ARGS.model, "zh")
    print(f"[mac-asr] listening on {_ARGS.host}:{_ARGS.port} model={_ARGS.model}",
          flush=True)
    ThreadingHTTPServer((_ARGS.host, _ARGS.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

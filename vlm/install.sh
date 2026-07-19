#!/usr/bin/env bash
# Idempotent installer for the LeKiwi VLM stack (vlm-daemon + MCP + units).
# Safe to re-run. Does NOT enable/start llama-server unless its binary exists.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UV="${UV:-$HOME/.local/bin/uv}"
export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
LLAMA_BIN="$HOME/work/llama.cpp/build/bin/llama-server"
UNIT_DIR="$HOME/.config/systemd/user"

echo "[install] vlm dir: $HERE"

# 1. venv (Python 3.11 via uv) --------------------------------------------- #
if [ ! -x "$HERE/.venv/bin/python" ]; then
    echo "[install] creating uv venv (python 3.11) ..."
    "$UV" venv --python 3.11 "$HERE/.venv"
else
    echo "[install] venv already exists"
fi

# 2. deps ------------------------------------------------------------------ #
echo "[install] installing deps (aiohttp, mcp) ..."
"$UV" pip install --python "$HERE/.venv/bin/python" aiohttp "mcp>=1.2"

# 3. token ----------------------------------------------------------------- #
if [ ! -s "$HERE/token" ]; then
    echo "[install] generating API token -> token (chmod 600)"
    openssl rand -hex 24 > "$HERE/token"
    chmod 600 "$HERE/token"
else
    echo "[install] token already present"
fi

# 4. systemd user units ---------------------------------------------------- #
mkdir -p "$UNIT_DIR"
cp -f "$HERE/systemd/vlm-daemon.service" "$UNIT_DIR/vlm-daemon.service"
cp -f "$HERE/systemd/llama-server.service" "$UNIT_DIR/llama-server.service"
echo "[install] installed units to $UNIT_DIR"

systemctl --user daemon-reload

# 5. enable ---------------------------------------------------------------- #
# vlm-daemon always: it runs fine with llama down.
systemctl --user enable vlm-daemon.service
echo "[install] enabled vlm-daemon.service"

if [ -x "$LLAMA_BIN" ]; then
    systemctl --user enable llama-server.service
    echo "[install] enabled llama-server.service (binary found)"
else
    echo "[install] SKIP enabling llama-server.service — binary not found at:"
    echo "          $LLAMA_BIN"
    echo "          Build llama.cpp (CUDA) then: systemctl --user enable --now llama-server.service"
fi

echo
echo "[install] done. Start with:"
echo "  systemctl --user start vlm-daemon.service"
echo "  systemctl --user status vlm-daemon.service"
echo "(user services survive logout only with: loginctl enable-linger $USER)"

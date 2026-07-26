#!/usr/bin/env bash
# LAN local-LLM brain for the robot: one mlx_lm OpenAI-compatible server on this
# Mac, serving BOTH local models. Pairs with the `local-35b` / `local-9b` presets
# in the board's ~/.config/lekiwi/config.json.
#
# ## Why one server and not one per model
#
# mlx_lm.server loads whatever the request's `model` field names and drops the
# previous one (`ModelProvider.load` → `_load`, which clears self.model first).
# That is exactly the shape the preset system already has: a preset is an api +
# a model name, and switching brains rewrites `model.default` in the gateway's
# config.yaml. So "switch to the 9B" needs no new service, no second port, and
# no coordination — it falls out of the model string.
#
# The cost is real and you will feel it: switching models re-reads the weights
# (37.7 GB for the 35B at 8bit). The board's switch job probes right after it
# restarts the gateway, so that read lands inside the probe budget — see
# VOICE_HERMES_PROBE_TIMEOUT in voice/daemon.py, and expect to measure it rather
# than assume it fits.
#
# ## No authentication, on purpose-ish
#
# mlx_lm.server has no token option — there is no --api-key and no Authorization
# check anywhere in it. So anything on the LAN can use this endpoint. That is a
# genuine difference from the omni server (which does have a bearer token), and
# the reason the board's preset validator only allows http:// for PRIVATE
# addresses. Do not port-forward this.
set -euo pipefail

HOST="${LLM_HOST:-0.0.0.0}"
PORT="${LLM_PORT:-8094}"                 # 8093 is the omni server; do not collide
MODEL="${LLM_MODEL:-mlx-community/Qwen3.6-35B-A3B-8bit}"

# The system prompt hermes sends is ~12k tokens and byte-identical every turn, so
# the prompt cache hits on every request after the first — the opposite of the
# vision server's case, where each image made a different prefix and its 8 GiB
# cache never earned its memory (see vlm/systemd/llama-server.service). Defaults
# are 10 distinct caches, unbounded bytes; bounded here because this box also
# holds the model.
# Thinking OFF, and this is not a preference — with it on the robot says nothing.
# Qwen3.5/3.6 default to thinking when the flag is absent, and the monologue is
# long: asked "又下暴雨了。" the 9B spent all 300 tokens on an English "Here's a
# thinking process that leads to the suggested response: 1. Analyze the Input…"
# and returned an EMPTY reply. Spoken output has no room for that — the operator
# hears silence, then nothing. Tool calls are unaffected either way (the model
# emits those without deliberating). Flip it back here if you ever want to trade
# latency for reasoning depth on a text-only bench.
exec uv run --python 3.12 --with 'mlx-lm==0.31.3' \
  python -m mlx_lm server \
  --host "$HOST" --port "$PORT" \
  --model "$MODEL" \
  --chat-template-args '{"enable_thinking":false}' \
  --prompt-cache-size 4 \
  --prompt-cache-bytes 8G \
  "$@"

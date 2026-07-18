#!/usr/bin/env bash
# Start the LeKiwi base-only ZMQ host in the background. Log: ~/base_host.log
# Usage: ./start_base_host.sh [/dev/ttyACM0]
set -uo pipefail
source ~/miniconda3/etc/profile.d/conda.sh
conda activate lerobot
pkill -f base_host.py 2>/dev/null || true
sleep 1
setsid nohup python "$HOME/base_host.py" "${1:-/dev/ttyACM0}" >"$HOME/base_host.log" 2>&1 </dev/null &
sleep 3
echo "=== base_host.log ==="
cat "$HOME/base_host.log"
echo "=== 5555 listener ==="
ss -tlnp 2>/dev/null | grep 5555 || echo "NO LISTENER (check log above)"

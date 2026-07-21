#!/usr/bin/env bash
# Redeploy the board-side programs — hands-off, NO password. `board/` mirrors
# the board filesystem 1:1, so deploy == rsync code + restart.
#
# Run scripts/setup_board.sh ONCE first: it installs the systemd units and a
# scoped NOPASSWD sudoers rule, after which the restart below needs no password.
#
# Usage:  scripts/deploy_board.sh [ip]        (default 192.168.13.189)
set -euo pipefail

IP=${1:-192.168.13.189}
USER=jetson
REPO=$(cd "$(dirname "$0")/.." && pwd)

echo "== sync code -> $USER@$IP:/home/$USER =="
rsync -av --exclude='__pycache__' "$REPO/board/home/$USER/" "$USER@$IP:/home/$USER/"

echo "== restart services (passwordless via /etc/sudoers.d/lekiwi-deploy) =="
ssh "$USER@$IP" 'sudo systemctl restart base_host pad_teleop'

echo "== verify =="
ssh "$USER@$IP" 'systemctl is-active base_host pad_teleop; ss -ltn | grep -E "5555|5556" || echo "(ports not up yet)"'
echo "Done."

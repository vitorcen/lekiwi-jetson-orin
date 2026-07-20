#!/usr/bin/env bash
# One-time board setup — the ONLY step that needs the board's sudo password.
# Installs the systemd units + a scoped NOPASSWD sudoers rule, so from then on
# scripts/deploy_board.sh restarts the services WITHOUT any password.
#
# Usage:  scripts/setup_board.sh [ip]        (default 192.168.3.189)
# Prereq: passwordless ssh (ssh-copy-id jetson@<ip>, or a key already in place).
set -euo pipefail

IP=${1:-192.168.3.189}
USER=jetson
REPO=$(cd "$(dirname "$0")/.." && pwd)

echo "== stage board/ -> $USER@$IP =="
rsync -av --exclude='__pycache__' "$REPO/board/home/$USER/" "$USER@$IP:/home/$USER/"
ssh "$USER@$IP" 'mkdir -p ~/.stage/system ~/.stage/sudoers.d'   # rsync won't make nested parents
rsync -av "$REPO/board/etc/systemd/system/" "$USER@$IP:/home/$USER/.stage/system/"
rsync -av "$REPO/board/etc/sudoers.d/"      "$USER@$IP:/home/$USER/.stage/sudoers.d/"

echo "== install units + sudoers rule + (re)start  (sudo password, this once) =="
ssh -t "$USER@$IP" '
  set -e
  sudo install -m644 ~/.stage/system/*.service /etc/systemd/system/
  sudo install -m440 ~/.stage/sudoers.d/lekiwi-deploy /etc/sudoers.d/lekiwi-deploy
  sudo visudo -cf /etc/sudoers.d/lekiwi-deploy            # reject a bad rule before it locks sudo
  sudo systemctl daemon-reload
  sudo systemctl enable --now base_host pad_teleop
  sudo systemctl restart base_host pad_teleop
  rm -rf ~/.stage'

echo "== verify =="
ssh "$USER@$IP" 'systemctl is-active base_host pad_teleop; ss -ltn | grep -E "5555|5556" || echo "(ports not up yet)"'
echo "Done — future deploys are passwordless: scripts/deploy_board.sh $IP"

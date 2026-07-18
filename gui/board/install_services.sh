#!/usr/bin/env bash
# Install/refresh LeKiwi boot services. Run ON THE BOARD:
#   sudo bash ~/install_services.sh
# Expects base_host.py / pad_teleop.py / *.service already copied to ~jatson.
set -euo pipefail
install -m644 /home/jatson/base_host.service /home/jatson/pad_teleop.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now base_host.service pad_teleop.service
systemctl restart base_host.service pad_teleop.service
sleep 2
systemctl --no-pager -l status base_host.service pad_teleop.service | sed -n '1,40p'

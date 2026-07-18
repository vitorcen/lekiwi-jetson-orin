#!/usr/bin/env bash
# Stop the LeKiwi base host (releases wheel torque on exit).
pkill -f base_host.py && echo "base_host stopped" || echo "base_host was not running"

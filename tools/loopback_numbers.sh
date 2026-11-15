#!/usr/bin/env bash
# Latency and bandwidth of the mock demo over loopback, measured by the Python probe client.
#   tools/loopback_numbers.sh [motion] [seconds] [extra probe args]       motion: still | circle | jump
set -euo pipefail
cd "$(dirname "$0")/.."
MOTION="${1:-circle}"
SECONDS_="${2:-10}"
shift $(( $# > 2 ? 2 : $# ))
PORT="${PORT:-47835}"
PY="${PYTHON:-python3}"
export PYTHONPATH=server
"$PY" -m quest_teleop.demo --port "$PORT" --hz "${HZ:-60}" --seconds $((SECONDS_ + 5)) > /dev/null &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
sleep 1.0
"$PY" -m quest_teleop.probe --host 127.0.0.1 --port "$PORT" --seconds "$SECONDS_" --motion "$MOTION" "$@"

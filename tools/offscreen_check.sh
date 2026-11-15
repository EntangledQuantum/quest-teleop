#!/usr/bin/env bash
# Starts the mock demo server and renders a scripted session with the desktop client, without a window.
#   tools/offscreen_check.sh [script] [frames] [out.png]      script: circle | jump | edit | still
# Needs: build/desktop/qt_desktop (cmake -S client -B build/desktop && cmake --build build/desktop) and numpy.
set -euo pipefail
cd "$(dirname "$0")/.."
SCRIPT="${1:-circle}"
FRAMES="${2:-300}"
OUT="${3:-build/offscreen_${SCRIPT}.png}"
PORT="${PORT:-47833}"
PY="${PYTHON:-python3}"
PYTHONPATH=server "$PY" -m quest_teleop.demo --port "$PORT" --seconds $((FRAMES / 72 + 6)) &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
sleep 1.0
build/desktop/qt_desktop --offscreen --server "127.0.0.1:$PORT" --frames "$FRAMES" --script "$SCRIPT" --out "$OUT" ${EVERY:+--every $EVERY} ${VIEW:-}

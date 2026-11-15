#!/usr/bin/env bash
# Regenerates the README's screenshots (docs/img) from the demo, without a headset:
# the status page and the trajectory page (headless Chrome), and the headset UI as drawn by the desktop client.
#   tools/make_screenshots.sh
# Needs: build/desktop/qt_desktop, numpy, google-chrome (or set CHROME). PYTHON selects the interpreter.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
CHROME="${CHROME:-google-chrome}"
PORT=47850; WEB=8810
DATA="$(mktemp -d)"
OUT=docs/img
Q="build/desktop/qt_desktop --offscreen --server 127.0.0.1:$PORT --size 1500x1150"
export PYTHONPATH=server
"$PY" tools/sample_session.py --data-dir "$DATA" --port $PORT --status-port $WEB --serve 150 > /dev/null &
SRV=$!
trap 'kill $SRV 2>/dev/null || true; rm -rf "$DATA"' EXIT
until [ -f "$DATA/index.json" ]; do sleep 0.5; done
sleep 1
press() { "$PY" -c "
import sys, time
from quest_teleop.probe import ProbeClient
c = ProbeClient('127.0.0.1', $PORT, name='screenshots')
while not c.loaded: c.poll(0.01)
c.press($1)
t = time.time()
while time.time() - t < 0.6: c.poll(0.01)
c.close()"; }
shot() { "$CHROME" --headless=new --disable-gpu --hide-scrollbars --window-size="$2" --timeout=7000 --screenshot="$PWD/$OUT/$1" "$3" > /dev/null 2>&1; }

$Q --frames 150 --script menu --action 1 --pitch -0.05 --out $OUT/headset_menu.png > /dev/null
$Q --frames 200 --script jump --pitch -0.5 --out $OUT/headset_go_back_cue.png > /dev/null
press 1                                             # start recording: the wrist card and the badge show REC
$Q --frames 140 --script still --pitch -0.55 --out $OUT/headset_wrist_card.png > /dev/null
build/desktop/qt_desktop --offscreen --server 127.0.0.1:$PORT --frames 2600 --script circle --out "$DATA/unused.png" > /dev/null &
sleep 22
shot status_page.png 1500,2150 "http://localhost:$WEB/"
press 1                                             # stop: a second take
sleep 1
FILE=$("$PY" -c "import json; print(json.load(open('$DATA/index.json'))['takes'][0]['file'])")
shot trajectory_page.png 1500,1640 "http://localhost:$WEB/take?file=$FILE&at=0.62"
wait %2 2> /dev/null || true
ls -la $OUT

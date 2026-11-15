# Status page and trajectory viewer

`StatusPage` serves a local web page from your server process: who is connected and how, how fast the simulator
and the network run, where the latency goes, and the recorded takes. Each take opens in a trajectory viewer. Python
standard library only, live by server-sent events, static HTML with no build step, nothing loaded from the
internet.

![The status page of the demo: a state strip (server running, headset connected by Wi-Fi, session running, recording take 2), then Connection, Session, Speeds with charts of the simulator real-time factor and network traffic, Latency split into its parts with a bar and a table, and a Recordings table listing one take.](../assets/img/status_page.png)

## Start it

```python
from quest_teleop import StatusPage, TakeRecorder, TeleopServer

srv = TeleopServer(name="my sim").start()
rec = TakeRecorder(tools=("left", "right"), max_opening=0.08)
page = StatusPage(srv, recorder=rec, extra=my_status, title="My sim").start()
print(page.url)                      # http://localhost:8800
...
page.stop()
```

| Argument | Default | Meaning |
|---|---|---|
| `server` | | the `TeleopServer` to watch |
| `port` | 8800 | HTTP port |
| `host` | `127.0.0.1` | address to bind; only this computer can open the page. Use `0.0.0.0` to let other machines open it (it has no login) |
| `extra` | `None` | a function returning your application's part of the page (below), called once per period |
| `title` | `"Teleoperation"` | page title |
| `period` | 1.0 | seconds between updates |
| `history_s` | 600 | seconds of history kept for the charts |
| `recorder` | `None` | the `TakeRecorder` in use: its folder is listed, and the take being recorded cannot be deleted or renamed |
| `recordings_dir` | `None` | a recordings folder to list when there is no recorder in this process (e.g. a viewer for old takes) |

The demo starts it on port 8800 (`quest-teleop-demo --status-port 0` turns it off).

## What it shows

* **State strip**: server, headset, session and recording state, each as a shape and words.
* **Connection**: headset connected and worn, USB or Wi-Fi, device, address, display rate, packet loss each way,
  scene scale, other clients.
* **Session**: whatever your `extra` callback reports.
* **Speeds**: simulator real-time factor and steps per second, network Mbit/s and packets per second, headset frames
  per second; charts over the last 10 minutes.
* **Latency**: total (median and 95th percentile) and its parts: network + pickup, wait for the next simulator step,
  simulator step, server processing, display refresh. The step and processing parts come from
  `server.note_timing` ([Latency measurement](library.md#latency-measurement)).
* **Recordings**: every take in the folder, newest first, with its name, duration, steps, size, validity and the
  datasets converted from it. Each take has **Rename** and **Delete** buttons; tick several takes to delete them
  together.

## Managing takes

The list always matches the disk. When the page starts, and again at every refresh, the index is reconciled with the
take files that are present: a take whose file you removed by hand is no longer listed, and a take file you copied
in is. Opening the page of a take that is gone says so and links back to the list.

**Delete** (per take, or **Delete selected** for the ticked ones) opens a dialog that names the takes, lists every
file that will be removed and says that it cannot be undone. Cancel has the focus when it opens. If datasets were
converted from the takes, a box "Also delete the converted clips" appears; they are kept unless you tick it. The
headset menu's "Delete the last take" removes a take the same way.

**Rename** (on the status page and on the take's page) sets the name shown in the list and an optional note; the
take number, session and file name stay visible underneath. Tick "Also rename the file on disk" to change the file
name too: the name is checked as you type (no `/`, `\` or `..`, at most 80 characters, other characters become
`_`), an existing name is refused, and the index and converted datasets follow.

The take being recorded, or still being written, cannot be deleted or renamed; its buttons are disabled. Both
dialogs work from the keyboard (Tab, Space, Enter, Escape to cancel) and report errors in the dialog.
[Recording takes](recording.md#deleting-renaming-and-keeping-the-index-in-step) has the same operations as Python
functions.

## Your part of the page: `extra`

`extra()` returns a dict; every key is optional:

```python
def my_status():
    return {
        "session": {"name": "run-01",
                    "state": "running",            # running | paused | recording | idle
                    "state_text": "Session running",
                    "rows": [["Scene", "pick and place"], ["Left tool", "idle"]]},
        "sim": {"rtf": 0.98,                       # simulator speed / real time
                "steps_per_s": 59.0,
                "contact_n": 2.5},                 # contact force [N], drawn on the speed chart
        # "recordings": {...}                      # only to replace the list built from the folder; same
        #                                          # shape as quest_teleop.status.recordings_table(root)
    }
```

## Trajectory viewer

![The trajectory view of take 1: a 3D plot of the left and right tool paths with start and end labels and gripper-state markers, play and scrub controls, a legend, a properties table (session, take, duration, steps, rate, tools, validity, size), a per-tool table (path length, closes, opens, time closed, following, lost), and a gripper-over-time chart.](../assets/img/trajectory_page.png)

Selecting a take on the status page opens `/take?file=...`:

* each tool's path in 3D: drag to turn, shift-drag or right-drag to move, wheel to zoom; with the view focused,
  arrow keys turn, shift + arrows move, `+` and `-` zoom, space plays, Home and End jump to the ends;
* play and scrub through time, at several speeds, optionally with the controller paths;
* the gripper state marked along the path by shape as well as colour: open, closing, closed, opening, could not
  follow;
* the gripper opening over time per tool;
* the take's properties and a per-tool summary (path length, closes, opens, share of time closed and following,
  lost events).

## Endpoints

All on the page's own address (default `http://localhost:8800`):

| Path | Returns |
|---|---|
| `/` | the status page |
| `/api/state` | the newest sample as JSON: `server.monitor()` plus your `extra()` and the recordings table |
| `/api/history` | the samples of the last `history_s` seconds (JSON list) |
| `/events` | the live stream (server-sent events, one sample per period) |
| `/take?file=<session>/<take>.npz` | the trajectory viewer |
| `/api/take?file=<session>/<take>.npz` | that take's paths and properties as JSON |
| `/fonts/*`, `/manage.js` | the page's two typefaces; the rename and delete dialogs |
| `POST /api/delete` | body `{"files": ["<session>/<take>.npz", ...], "converted": false}`; returns `deleted`, `refused` (file and reason), `removed`, `converted_removed` |
| `POST /api/rename` | body `{"file": ..., "title": ..., "note": ..., "rename_file": false, "new_name": ...}`; returns `{"ok": true, "entry": {...}}`, or status 400 and `{"error": reason}` |

The two POST endpoints change files, so they accept only JSON requests that carry the header
`X-Requested-With: quest-teleop` and come from the page's own address; a form or a script on another web site
cannot call them. They act only inside the recordings folder. Example:

```bash
curl -s -X POST http://localhost:8800/api/rename -H 'Content-Type: application/json' \
     -H 'X-Requested-With: quest-teleop' -d '{"file": "run-01/take_003_x.npz", "title": "Clean pick"}'
```

`/api/state` is handy for scripts and agents: `curl -s http://localhost:8800/api/state`.

## Accessibility

Both pages use real text and tables, shapes plus words for every state, visible keyboard focus, keyboard control of
the 3D view, and text equivalents for the charts.

Next: [Protocol](protocol.md).

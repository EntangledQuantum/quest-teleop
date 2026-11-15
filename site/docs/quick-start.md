# Quick start

Run the bundled demo, connect a headset (or the desktop client), move two grippers, record a take, and look at it
on the status page. Needs the Python library ([Install](install.md#1-the-python-library)); the headset part needs
the app installed ([Quest 2 setup](quest-setup.md)).

## 1. Start the demo server

```bash
quest-teleop-demo                 # same as: python -m quest_teleop.demo
```

A mock world: two floating grippers over a table, a cube to pick and place, a rope and a cloth. It prints the
addresses it listens on and the status page's address:

```
[quest-teleop] listening on 0.0.0.0:47800 (TCP + UDP), addresses: ...
[demo] status page: http://localhost:8800    takes are saved in ~/teleop_recordings/...
```

Every 5 seconds while a client is connected it prints frames per second, traffic and the client's latency. Stop it
with Ctrl-C.

| Option | Default | Meaning |
|---|---|---|
| `--port` | 47800 | TCP and UDP port of the server |
| `--hz` | 60 | simulation and state rate |
| `--mode` | `clutch` | `clutch`: the gripper follows the hand's motion while the grip is held; `absolute`: the gripper goes where the hand is |
| `--seconds` | 0 | stop after this long (0: run until Ctrl-C) |
| `--status-port` | 8800 | port of the status page (0: no status page) |
| `--data-dir` | `$TELEOP_DATA_DIR`, else `~/teleop_recordings` | where takes are saved |
| `--session` | date and time | name of this run (a folder in the data directory) |
| `--host-label` | host name and address | what the headset shows as the computer |

## 2. Connect a client

**Headset over Wi-Fi.** Start **Quest Teleop** in the headset (Library, filter "Unknown sources"). It finds the
server by itself if the firewall ports are open ([Quest 2 setup](quest-setup.md#5-network-wi-fi)).

**Headset over USB.**

```bash
adb reverse tcp:47800 tcp:47800
adb shell am start -n com.entangledquantum.questteleop/android.app.NativeActivity
```

**No headset: the desktop client** ([Install](install.md#3-the-desktop-client-optional)):

```bash
build/desktop/qt_desktop
```

**No display at all: the probe**, a headless Python client that moves a synthetic controller and prints latency and
bandwidth:

```bash
quest-teleop-probe --host 127.0.0.1 --seconds 10 --motion circle     # --motion still | circle | jump, --tcp-only
```

When the scene appears, the headset places the working area in front of you at a comfortable size.

## 3. Controls in the headset

| Control | Action |
|---|---|
| **Grip** (hold) | move that hand's gripper (right controller: right gripper, left: left) |
| **Trigger** (hold) | close the gripper slowly (deeper is faster) |
| **A / X** (hold) | open the gripper slowly |
| **B** | start / stop recording a take |
| **Left menu button** | open / close the panel; the server is told and holds the robot meanwhile |

With the panel open:

| Control | Action |
|---|---|
| point + **trigger** | press the button the beam points at; "danger" actions (reset, delete) need the trigger held for about a second |
| one **grip** | drag and turn the scene |
| both **grips** | scale the scene |
| left **stick** | slide the scene |
| right **stick** | turn and raise the scene |

The panel shows the connection, the session, the demo's actions (start recording, reset the scene, delete the last
take), the control legend, and settings: larger text, left-handed, passthrough, shadows, scene size, placement.

**Try the go-back cue.** Each gripper has a limited reach (a sphere around its base post) and cannot enter the
table. Hold the grip and pull your hand beyond that: the gripper stops, the controller vibrates, and the headset
shows a see-through shell where the gripper really is, a marker where your hand asks it to be, a line between them,
and the text "Robot cannot follow". Bring your hand back to the highlighted tool to continue.

## 4. Desktop client controls

| Input | Action |
|---|---|
| right-drag | look around |
| `W A S D Q E` | move the head |
| left-drag | move the active hand (with Shift: turn it) |
| mouse wheel | move the hand nearer / farther |
| `TAB` | switch between left and right hand |
| `G` | grip |
| `T` | trigger |
| `1` / `2` | primary / secondary button (A / B on the right hand) |
| `M` | menu |
| `ESC` | quit |

## 5. Record a take and look at it

1. Press **B** (desktop: `TAB` to the right hand, then `2`). The wrist card shows `REC 00:00 take 1`.
2. Move a gripper, close it on the cube, move it, open it.
3. Press **B** again. The headset shows "Take 1 saved" with the number of steps.
4. Open <http://localhost:8800> on the computer. Under **Recordings**, select the take: the trajectory view shows
   each gripper's path in 3D with the gripper state marked along it.

The take is a `.npz` file in `~/teleop_recordings/<session>/`. See [Recording takes](recording.md) for its contents
and [Status page](status-page.md) for the page.

Next: [Use it with your own simulator](library.md).

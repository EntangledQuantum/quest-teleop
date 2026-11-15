# Use it with your own simulator

The Python library is the computer side. Your program owns the simulator (or the robot driver); quest-teleop
streams what it should look like to the headset and hands back the controllers. Nothing in the library or the app
is specific to a task: you describe the scene with meshes and poses, and the panel with rows and buttons.

## The loop

Every application has the same shape:

1. Build a `Scene`: meshes, and nodes that place them.
2. Start a `TeleopServer` and give it the scene.
3. Every simulation step: read the newest controller sample (`get_input`), step your simulator with it, write the
   new poses and vertices into the scene, and call `publish`.

`publish` sends each connected client only what changed since the frame it last acknowledged, so things that do not
move cost nothing. It runs in your thread and never blocks on the network.

## A complete example

A box that follows the right controller while its grip button is held, with a vibration when the grip closes and a
panel button that resets the box. Run it, then connect the headset, the desktop client or the probe
([Quick start](quick-start.md#2-connect-a-client)).

```python
"""A box that follows the right controller while its grip button is held."""
import time

import numpy as np
from quest_teleop import Action, Panel, Row, Scene, TeleopServer
from quest_teleop.scene import box_mesh

scene = Scene("my sim", bounds_min=(-1, -1, 0), bounds_max=(1, 1, 2), up_axis=2,
              focus_center=(0, 0, 0.3), focus_radius=0.4)
v, f, n = box_mesh(half=(0.03, 0.03, 0.03))
box = scene.add_node("box", scene.add_mesh(v, f, n), pos=(0, 0, 0.3), color=(0.55, 0.56, 1.0, 1))

srv = TeleopServer(name="my sim").start()
srv.set_scene(scene)

box_pos = np.array([0.0, 0.0, 0.3])
offset = None                                   # box position minus hand position while the grip is held
held_before = False
try:
    while True:
        inp = srv.get_input()                   # newest controller sample, or None
        right = inp.hands[1] if inp is not None else None
        held = bool(inp is not None and not inp.edit_mode and right.active and right.squeeze > 0.5)
        if held:
            hand_pos, hand_quat = inp.hand_in_scene(1)   # right controller in scene coordinates
            if not held_before:
                offset = box_pos - hand_pos
                srv.haptic(1, amplitude=0.5, duration=0.03)
            box_pos = hand_pos + offset
        held_before = held
        scene.set_pose(box, box_pos, (0, 0, 0, 1))

        panel = Panel(title="My sim", subtitle="one box")
        panel.status("ok" if held else "none", "Moving the box" if held else "Hold the right grip")
        panel.rows = [Row("BOX Z", f"{box_pos[2]:.2f} m", wrist=True)]
        panel.actions = [Action(1, "Reset the box", style="primary")]
        panel.legend = [("$R GRIP (HOLD)", "move the box")]
        srv.set_panel(panel)                    # nothing is sent when nothing changed
        if 1 in srv.pop_actions():
            box_pos = np.array([0.0, 0.0, 0.3])
            srv.set_text(1, "Box reset", ttl=1.5)

        srv.publish(echo=inp)                   # sends only what changed; echo gives the latency measurement
        time.sleep(1 / 60)
except KeyboardInterrupt:
    srv.stop()
```

The bundled demo (`server/quest_teleop/demo.py`, about 400 lines) is the full version: two grippers with
`ToolTeleop`, `RateGripper`, `ContactHaptics` and `GoBackCue`, recording and the status page.

## Coordinates

* **Scene space** is your simulator's world frame, in metres. `up_axis` says which axis is up (0 x, 1 y, 2 z).
  Quaternions are always `x y z w`.
* The headset draws the scene somewhere in the room and the user can move, turn and scale it. Controller poses
  arrive in the room's frame together with that placement; `InputFrame.hand_in_scene(i)` converts them to scene
  space for you. A scene drawn at twice its size halves the hand motion in simulator units.
* Positions are sent as 16-bit values over the scene bounds, so **the bounds must contain everything that moves**
  (values outside are clamped). A 2 m scene gives 0.03 mm steps.

## Scene

```python
from quest_teleop import Scene
scene = Scene(name="scene", bounds_min=(-1, -1, -1), bounds_max=(1, 1, 1), up_axis=2,
              focus_center=None, focus_radius=None, view_dir=(0, 0, 0))
```

`focus_center` and `focus_radius` describe the region the operator works in: the headset puts it in front of the
user at a comfortable size when the scene loads. `view_dir` is the horizontal direction, in scene coordinates, the
operator should look along at the start (zero: no preference).

| Method | What it does |
|---|---|
| `add_mesh(vertices, faces, normals=None, colors=None, double_sided=False)` | a mesh (sent once); returns its id. `vertices` (V, 3), `faces` (F, 3) ints, `normals` (V, 3), `colors` (V, 3 or 4) in 0..1 |
| `add_node(name, mesh, pos=(0,0,0), quat=(0,0,0,1), scale=1.0, color=(1,1,1,1), parent=P_SCENE, flags=F_VISIBLE)` | a rigid node that draws a mesh; returns its id. `color` multiplies the mesh colours; alpha below 1 draws it blended |
| `add_deform(name, vertices, faces, color=..., colors=None, parent=P_SCENE, flags=F_VISIBLE, double_sided=True)` | a mesh whose vertex positions you stream (cloth, soft bodies); at most 65535 vertices |
| `add_tube(name, points, radius, color=..., parent=P_SCENE, flags=F_VISIBLE)` | a polyline of fixed point count drawn as a tube (rope, cable, pointer line) |
| `set_pose(node, pos=None, quat=None)` | move a node |
| `set_poses(nodes, pos, quat)` | move many nodes at once (arrays) |
| `set_points(node, points)` | new vertex positions of a deform node, or new points of a tube |
| `set_visible(node, visible)` | show or hide a node |
| `set_color(node, color)` | change a node's colour |
| `vertex_deadband` | attribute, metres: streamed vertices are re-sent only when they moved more than this (default 0). A few hundredths of a millimetre stops solver jitter in resting cloth from being streamed |

Mesh helpers in `quest_teleop.scene` return `(vertices, faces, normals)`: `box_mesh(half=(0.5, 0.5, 0.5))`,
`sphere_mesh(radius=0.5, rings=12, sectors=20)`, `cylinder_mesh(radius=0.5, half_height=0.5, sectors=20,
cap_round=False)`.

**Parents.** A node's pose is given in its parent's frame. Nodes attached to the head or a controller move with it
on the headset with no network delay; use them for cues that must stick to the hand. Constants in
`quest_teleop.protocol`:

| Constant | Frame |
|---|---|
| `P_SCENE` | the scene (default) |
| `P_STAGE` | the room (y up, origin on the floor) |
| `P_HEAD` | the head |
| `P_LEFT_GRIP`, `P_RIGHT_GRIP` | a controller's grip pose |
| `P_LEFT_AIM`, `P_RIGHT_AIM` | a controller's pointing ray |

**Flags** (combine with `|`): `F_VISIBLE`, `F_BLEND` (use alpha), `F_UNLIT`, `F_ON_TOP` (no depth test), `F_FLAT`
(flat shading), `F_HIGHLIGHT` (accent rim, e.g. the tool that is following the hand).

Adding meshes or nodes after `set_scene` is allowed: the next `publish` notices and every client reloads the scene.
Calling `set_scene` with another `Scene` replaces it.

## TeleopServer

```python
from quest_teleop import TeleopServer
srv = TeleopServer(name="quest-teleop server", port=47800, bind="0.0.0.0", beacon=True, state_hz=60.0,
                   verbose=True).start()
```

`beacon=True` announces the server by UDP broadcast on port 47801 once per second, so the headset finds it.
`state_hz` is what the server tells clients its state rate is; the actual rate is how often you call `publish`.

| Method | What it does |
|---|---|
| `start()` | open the sockets and threads; returns the server |
| `stop()` | say goodbye to the clients and close everything |
| `set_scene(scene)` | stream this scene; every client (re)loads it |
| `get_input(max_age=0.5)` | the newest `InputFrame` of the controlling client, or `None` (no client, headset not worn, or older than `max_age` seconds) |
| `publish(echo=None)` | send one state frame. Pass the `InputFrame` this state results from as `echo` to get the latency measurement. Returns bytes sent |
| `haptic(hand, amplitude, duration=0.02, frequency=0.0)` | vibrate controller `hand` (0 left, 1 right); amplitude 0..1, duration in seconds. Quest 2 controllers ignore the frequency |
| `set_text(slot, text, anchor=TEXT_HUD, pos=(0,0,0), size=0.03, color=(1,1,1,1), ttl=0.0)` | show text in slot 0..255; an empty string clears the slot |
| `set_panel(panel)` | show or update the control panel; cheap to call every step |
| `pop_actions()` | ids of the panel buttons pressed since the last call |
| `note_timing(step_s, processing_s)` | report how long the simulator step and the rest of your work took (for the status page's latency breakdown) |
| `monitor()` | everything a status display needs (clients, rates, loss, latency parts); used by the status page |
| `pop_stats()` | traffic and the client's latency since the last call |
| `clients()`, `controller()`, `n_clients` | connected clients; the one whose input drives the simulator (the longest-connected one that is sending input) |
| `on_connect`, `on_disconnect` | attributes: set to a function `f(client)` to be called |

## Input

`get_input()` returns an `InputFrame` (or `None`):

| Field | Meaning |
|---|---|
| `hands` | `(left, right)`, two `Hand` objects |
| `edit_mode` | `True` while the user has the headset menu open or is placing the scene: **ignore the controls and hold the robot** |
| `hand_in_scene(i, aim=False)` | `(pos, quat)` of controller `i` in scene coordinates; `aim=True` gives the pointing ray instead of the grip |
| `scene_pose(pos, quat)` | convert any room-space pose to scene space |
| `head_pos`, `head_quat` | the head, room space |
| `place_pos`, `place_quat`, `place_scale` | where the scene is drawn in the room |
| `flags` | bit `IF_FOCUSED`: the headset is worn and the app has focus; `IF_PASSTHROUGH`: passthrough is on |
| `latency_us` | the client's last measured latency |

A `Hand`:

| Field | Meaning |
|---|---|
| `active` | the controller is present and its grip pose is valid |
| `trigger`, `squeeze` | 0..1 (index trigger, grip button) |
| `stick` | thumbstick x, y in -1..1 |
| `buttons`, `button(mask)` | button bits: `B_PRIMARY` (A / X), `B_SECONDARY` (B / Y), `B_STICK`, `B_MENU`, and touch bits `B_TRIGGER_TOUCH`, `B_STICK_TOUCH`, `B_PRIMARY_TOUCH`, `B_SECONDARY_TOUCH`, `B_THUMBREST_TOUCH` (constants in `quest_teleop.protocol`) |
| `grip_pos`, `grip_quat`, `aim_pos`, `aim_quat` | raw poses in room space |
| `vel`, `angvel` | grip velocity and angular velocity, room space |

The left menu button is used by the headset app itself (it opens the panel), so it is not a control for your
application. When the user turns on "left-handed" in the headset settings, the app swaps the two hands in the input
and the two haptic channels, so your code needs no special handling.

## Haptics

```python
srv.haptic(1, amplitude=0.6, duration=0.03)     # right controller, short tick
srv.haptic(0, 0.0)                              # amplitude 0 stops the left one
```

The haptic command travels in every state datagram until it is replaced, so a lost datagram does not lose it.
`ContactHaptics` turns a contact force into vibration: a strong short tick when contact starts, then an amplitude
that follows the force.

```python
from quest_teleop import ContactHaptics
touch = ContactHaptics(f_min=0.3, f_max=15.0)   # force thresholds in N
hp = touch.update(dt, force)                    # every step; returns (amplitude, duration) or None
if hp:
    srv.haptic(1, *hp)
```

## Text cues

```python
from quest_teleop import protocol as P
srv.set_text(0, "Recording", ttl=2.0)                                   # head-locked, for 2 s
srv.set_text(1, "grasp here", anchor=1 + P.P_SCENE, pos=(0.4, 0, 0.3),  # in the scene, facing the viewer
             size=0.02, color=(1, 0.85, 0.2, 1))
srv.set_text(1, "")                                                     # clear slot 1
```

`anchor=TEXT_HUD` (0) puts the text on a panel 1 m in front of the eyes, `pos` being the offset on it.
`anchor=1 + parent` puts it at `pos` in that parent's frame (`P_SCENE`, `P_LEFT_GRIP`, ...). `size` is the line
height in metres, `ttl` seconds (0: until replaced). Lines are separated by `\n`.

Cues other than text are ordinary nodes: add them hidden and switch them with `set_visible`, `set_pose` and
`set_color`. The go-back cue below is built that way.

## Panel and actions

The panel is what the headset shows on the wrist card and in the menu: a title, a status line, your rows, your
buttons and a control legend. The headset lays it out; your code only describes it.

```python
from quest_teleop import Action, Panel, Row

panel = Panel(title="Pick and place", subtitle="simulator A, config B")
panel.status("recording", "REC 00:12  take 3")          # style: none | recording | caution | ok
panel.rows = [Row("RUN", "session-01", wrist=True),       # wrist=True: also on the wrist card
              Row("SIM SPEED", "0.55x", style="caution")] # style: normal | accent | caution | failure
panel.actions = [Action(1, "Stop recording", hint="$Rs", style="primary", active=True),
                 Action(2, "Reset the scene", hint="hold", style="danger", confirm=True)]
panel.legend = [("$R GRIP", "move the gripper"), ("$R TRIGGER", "close slowly")]
srv.set_panel(panel)

for action_id in srv.pop_actions():
    ...
```

| `Action` field | Meaning |
|---|---|
| `id` | number returned by `pop_actions()` |
| `label`, `hint` | button text; the hint (a shortcut) is printed at its right edge |
| `style` | `normal`, `primary` or `danger` |
| `enabled` | greyed out when `False` |
| `confirm` | the user must hold the trigger on it for about a second |
| `active` | drawn as switched on |

**Role tokens** in any panel string are replaced on the headset, so the text stays right when the user swaps hands:
`$R` / `$L` = "Right" / "Left" (the controller in that role), `$Rp`, `$Rs`, `$Lp`, `$Ls` = that controller's primary
or secondary button (A, B, X, Y).

## Safe hand-to-tool mapping

For robots and grippers, `ToolTeleop` turns one controller into a tool-pose command that the robot can follow:

* The user holds a clutch (the grip button) to move the tool. In `clutch` mode the tool follows the hand's motion
  relative to where the clutch was pressed; in `absolute` mode the tool goes where the hand is.
* The command moves toward the hand at a bounded speed and never leads the measured tool by more than `max_lead`.
* If the hand gets further from the measured tool than the tether (sudden move, joint limit, out of reach, blocked),
  tracking stops (state `lost`): the robot holds, and `out.cue` says to show where the tool really is. Tracking
  resumes when the hand is back near the tool.

```python
from quest_teleop import GoBackCue, RateGripper, TeleopCfg, ToolTeleop
from quest_teleop import protocol as P

teleop = ToolTeleop(TeleopCfg(mode="clutch", max_speed=0.25, tether=0.08))
gripper = RateGripper(close_time=2.0, open_time=1.5)
cue = GoBackCue(scene, "right", tool_mesh=None)          # adds its hidden nodes to the scene

# every step, i = 1 for the right hand
hand, clutch, trigger, open_btn = None, False, 0.0, False
if inp is not None and not inp.edit_mode and inp.hands[i].active:
    h = inp.hands[i]
    hand, clutch, trigger, open_btn = inp.hand_in_scene(i), h.squeeze > 0.5, h.trigger, h.button(P.B_PRIMARY)
out = teleop.update(dt, hand, clutch, tool_pos, tool_quat, reachable=ik_ok)
closure = gripper.update(dt, trigger, open_btn)          # 0 open .. 1 closed
robot.command(out.cmd_pos, out.cmd_quat, closure)        # your robot or simulator
cue.update(dt, out, tool_pos, tool_quat, srv)            # shows / hides the go-back cue and its text
if out.lost_edge:
    srv.haptic(i, 1.0, 0.12)
```

`TeleopCfg` fields (defaults): `mode="clutch"`, `max_speed=0.25` m/s, `max_angular_speed=2.0` rad/s,
`max_lead=0.03` m, `max_angular_lead=0.35` rad, `tether=0.08` m, `tether_angle=0.9` rad, `tether_time=0.25` s,
`reengage=0.03` m, `reengage_angle=0.45` rad, `motion_scale=1.0`, `absolute_offset_pos`, `absolute_offset_quat`.

`update` returns a `TeleopOut` with `state` (`idle`, `tracking`, `lost`), `cmd_pos`, `cmd_quat`, `wish_pos`,
`wish_quat` (where the hand asks the tool to be), `lag`, `lag_angle`, `cue`, `engaged_edge`, `lost_edge`.

## Latency measurement

Pass the input sample to `publish(echo=inp)`. The headset then measures the time from sampling the controllers to
holding the state that resulted from that sample, and reports it back. To split it into parts on the status page,
also report your timing:

```python
inp = srv.get_input()
t0 = time.perf_counter()
step_simulator(inp)
step = time.perf_counter() - t0
srv.publish(echo=inp)
srv.note_timing(step, time.perf_counter() - t0 - step)
```

Next: [Recording takes](recording.md).

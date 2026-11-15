# Recording takes

A **take** is one recorded stretch of teleoperation: the tools' poses, the grippers, the controllers and the time at
every step, plus any data of your own. `TakeRecorder` writes one compressed `.npz` file per take and keeps an index
of all takes, which the [status page](status-page.md) lists and opens in the trajectory viewer.

## Recording

```python
from quest_teleop import TakeRecorder

rec = TakeRecorder(tools=("left", "right"), max_opening=0.08)   # folder: $TELEOP_DATA_DIR or ~/teleop_recordings
rec.write_session({"application": "my sim", "robot": "two grippers"})   # optional, once per session

rec.start(start_state={"cube": cube_pos})    # optional: your state just before the first step
while recording:
    rec.add(sim.time, tool_pos, tool_quat,                # (N, 3), (N, 4) for N tools
            opening=openings, opening_cmd=opening_cmds,   # (N,) metres
            target_pos=cmd_pos, target_quat=cmd_quat,
            tool_state=[1, 0],                            # 0 idle, 1 following the hand, 2 lost
            ctrl_pos=..., ctrl_quat=..., trigger=..., squeeze=..., buttons=...,
            extra={"cube_pos": cube_pos, "rope": rope_points})
entry = rec.stop(info={"valid": True, "flags": []})       # writes the file in the background
```

`add` does nothing while the recorder is not recording, so it can be called every step. Give the same fields at
every step of a take.

| `TakeRecorder` | |
|---|---|
| `TakeRecorder(root=None, session=None, tools=("left", "right"), max_opening=None, up_axis=2)` | `root`: data folder (else `$TELEOP_DATA_DIR`, else `~/teleop_recordings`); `session`: run name (default: date and time); `max_opening`: full gripper opening in metres, one value or one per tool (for the open / closed thresholds) |
| `start(start_state=None)` | begin a take |
| `add(t, tool_pos, tool_quat=None, *, opening, opening_cmd, grip_state, target_pos, target_quat, tool_state, ctrl_pos, ctrl_quat, trigger, squeeze, buttons, extra)` | one step; everything after `tool_pos` is optional |
| `stop(info=None)` | end the take, write it, return its index entry (or `None` if nothing was recorded). `info` is merged into the entry |
| `discard()` | end the take without writing it |
| `delete_last()` | delete the newest take of this session (file and index entry) |
| `write_session(info)` | write `session.json` for this session |
| `wait()` | wait until the last take is written (call before exiting) |
| `recording`, `steps`, `take`, `n_saved`, `elapsed`, `root`, `session` | state: recording or not, steps so far, next take number, takes saved this session, seconds since start, folders |

The `info` keys `valid` (true / false) and `flags` (a list of strings) are shown in the status page's validity
column.

## Files

```
~/teleop_recordings/index.json                      every take: file, session, take, started, steps, seconds,
                                                    wall_seconds, rate_hz, tools, and your info
~/teleop_recordings/<session>/session.json          what write_session() was given
~/teleop_recordings/<session>/take_003_<time>.npz   one take
```

## Arrays in a take

T steps, N tools. Only `t` and `tool/pos` are always present.

| Array | Shape | Meaning |
|---|---|---|
| `t` | (T,) | application time of each step [s] (for example simulated time) |
| `t_wall` | (T,) | wall-clock seconds since the take started |
| `tool/pos` | (T, N, 3) | measured tool positions [m], scene coordinates |
| `tool/quat` | (T, N, 4) | measured tool orientations, xyzw |
| `tool/target_pos`, `tool/target_quat` | (T, N, 3), (T, N, 4) | commanded tool poses |
| `tool/opening` | (T, N) | measured gripper opening [m] |
| `tool/opening_cmd` | (T, N) | commanded gripper opening [m] |
| `tool/grip_state` | (T, N) int8 | 0 open, 1 closing, 2 closed, 3 opening (given, or derived from the openings) |
| `tool/state` | (T, N) int8 | 0 idle, 1 following the hand, 2 lost (could not follow) |
| `ctrl/pos`, `ctrl/quat` | (T, N, 3), (T, N, 4) | the controllers in scene coordinates (NaN while not tracked) |
| `ctrl/trigger`, `ctrl/squeeze` | (T, N) | 0..1 |
| `ctrl/buttons` | (T, N) int32 | button bits (see [Input](library.md#input)) |
| `extra/<name>` | (T, ...) | your own per-step data |
| `start/<name>` | any | your state just before the first step |
| `meta` | string | JSON: tools, max_opening, rate_hz, session, take, started, up_axis |

## Loading takes

```python
import os
from quest_teleop import load_take, take_summary
from quest_teleop.recording import read_index

root = os.path.expanduser("~/teleop_recordings")
for entry in read_index(root):                       # every take listed in index.json
    take = load_take(os.path.join(root, entry["file"]))
    summary = take_summary(take)                     # per tool: path length, gripper closes / opens,
                                                     # share of time closed / following, lost events
    print(entry["session"], entry["take"], take["tool/pos"].shape, summary["tools"][0]["path_m"])
```

`load_take(path, payload=False)` skips the `extra/` and `start/` arrays (which can be most of the file) and lists
their names under `extra_keys`. The files are ordinary NumPy archives: `numpy.load(path)` reads them too.

Next: [Status page and trajectory viewer](status-page.md).

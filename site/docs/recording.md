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
~/teleop_recordings/<session>/take_003_<time>.json  its display name and note, once it has been renamed
~/teleop_recordings/converted/index.json            optional: datasets your application derived from takes
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

## Deleting, renaming and keeping the index in step

The index always says what is on disk. You can delete or move take files by hand: the next time the status page
starts or refreshes (or you call `reconcile`), entries whose file is gone are dropped and take files that are not
listed are added (read from the file).

```python
from quest_teleop.recording import reconcile, delete_takes, rename_take, take_files

reconcile(root)                                   # {"removed": [...], "added": [...], "converted_removed": [...]}
take_files(root, "run-01/take_003_x.npz")         # the take and its sidecar files
delete_takes(root, ["run-01/take_003_x.npz"])     # for good: the .npz, its sidecars, the index entry
rename_take(root, "run-01/take_003_x.npz", title="Clean pick", note="first try")
rename_take(root, "run-01/take_003_x.npz", new_name="clean pick")     # also renames the file
rec.delete_last()                                 # the newest take of this session (the headset menu's action)
```

| Function | What it does |
|---|---|
| `reconcile(root)` | makes `index.json` match the folder; also drops derived datasets whose folder is gone from `converted/index.json`. Cheap when nothing changed |
| `delete_takes(root, files, also_converted=False, busy=())` | removes each take's `.npz`, its sidecar files and its index entry. Returns `deleted`, `refused` (with a reason), `removed` (files) and `converted_removed` |
| `rename_take(root, file, title=None, note=None, new_name=None, busy=())` | sets the display name and note (index entry and `<take>.json`, so they survive a rebuilt index). With `new_name` the file and its sidecars are renamed too. Returns the index entry; raises `ValueError` with the reason when refused |
| `take_files(root, file)` | the files that belong to a take: the `.npz` and files in the same folder named `<take>.<anything>` or `<take>_<anything>` |
| `sanitize_name(text)` | the file name `rename_take` would use: letters, digits, space, dot, dash and underscore, at most 80 characters, no path separators and no `..` |

Rules that always hold:

* Nothing outside the recordings folder is touched. Paths with `..`, absolute paths and symbolic links are refused.
* A take that is being recorded or still being written is refused. Pass `busy=rec.busy_files()`; the status page
  does this when it is given the recorder.
* A file rename never replaces an existing file; the name is refused instead.
* `index.json` and `converted/index.json` are replaced atomically, under a lock file (`index.json.lock`) shared by
  the recorder, the status page and your own tools (`with index_lock(root): ...` around your own read-modify-write).
* Derived datasets are deleted only with `also_converted=True`, and only when their folder is inside
  `<root>/converted`. A file rename updates their take references.

`converted/index.json` is optional and written by your application:
`{"version": 1, "datasets": [{"name": ..., "out": folder, "takes": [take files]}]}`. The status page shows the
dataset names next to each take.

Next: [Status page and trajectory viewer](status-page.md).

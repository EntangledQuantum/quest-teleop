"""Recording teleoperated trajectories ("takes"). Generic: tools, grippers, controllers, time; plus whatever the
application wants to store per step.

    rec = TakeRecorder(tools=("left", "right"), max_opening=0.08)       # folder: $TELEOP_DATA_DIR or ~/teleop_recordings
    rec.start()
    every step:
        rec.add(t, tool_pos, tool_quat, opening=..., opening_cmd=..., target_pos=..., target_quat=...,
                tool_state=..., ctrl_pos=..., ctrl_quat=..., trigger=..., squeeze=..., buttons=...,
                extra={"my/state": array, ...})
    entry = rec.stop(info={"valid": True})

Layout on disk:

    <data dir>/index.json                        every take: file, session, take number, start time, steps, seconds,
                                                 wall_seconds, rate_hz, tools, and the application's `info`
    <data dir>/<session>/session.json            what the application says about the session (`write_session`)
    <data dir>/<session>/take_003_<time>.npz     one take

Arrays in a take (T steps, N tools; every array is optional except t and tool/pos):

    t (T,)                    application time of each step [s] (e.g. simulated time)
    t_wall (T,)               wall-clock seconds since the take started
    tool/pos (T, N, 3)        measured tool positions [m], scene coordinates
    tool/quat (T, N, 4)       measured tool orientations, xyzw
    tool/target_pos, tool/target_quat      the commanded tool poses
    tool/opening (T, N)       measured gripper opening [m]
    tool/opening_cmd (T, N)   commanded gripper opening [m]
    tool/grip_state (T, N)    int8: 0 open, 1 closing, 2 closed, 3 opening (given, or derived from the openings)
    tool/state (T, N)         int8: 0 idle, 1 following the hand, 2 lost (cannot follow)
    ctrl/pos (T, N, 3), ctrl/quat (T, N, 4)       the controllers in scene coordinates (NaN while not tracked)
    ctrl/trigger, ctrl/squeeze (T, N), ctrl/buttons (T, N) int32
    extra/<name> (T, ...)     the application's own per-step data
    start/<name>              the application's state just before the first step
    meta                      JSON string: tools, max_opening, rate_hz, session, take, started

`load_take(path)` reads one; `take_summary(take)` computes path lengths, gripper events and durations (the status
page's trajectory view uses it).
"""
from __future__ import annotations

import json
import os
import threading
import time

import numpy as np

GRIP_OPEN, GRIP_CLOSING, GRIP_CLOSED, GRIP_OPENING = 0, 1, 2, 3
GRIP_NAMES = ("open", "closing", "closed", "opening")
STATE_CODE = {"idle": 0, "tracking": 1, "lost": 2}


def data_dir(given: str | None = None, default: str | None = None) -> str:
    """Where takes live: the argument, else $TELEOP_DATA_DIR, else `default`, else ~/teleop_recordings."""
    return os.path.abspath(os.path.expanduser(given or os.environ.get("TELEOP_DATA_DIR") or default or "~/teleop_recordings"))


def derive_grip_state(opening, max_opening: float, eps: float | None = None) -> np.ndarray:
    """(T, N) openings [m] -> (T, N) int8 gripper state: closing / opening while the opening changes by more than
    eps per step, otherwise closed below 20 % of the full opening and open above."""
    o = np.asarray(opening, float)
    mx = max(float(max_opening), 1e-6)
    eps = 2e-4 * mx / 0.08 if eps is None else eps
    d = np.diff(o, axis=0, prepend=o[:1])
    st = np.where(o < 0.2 * mx, GRIP_CLOSED, GRIP_OPEN)
    st = np.where(d < -eps, GRIP_CLOSING, st)
    st = np.where(d > eps, GRIP_OPENING, st)
    return st.astype(np.int8)


class TakeRecorder:
    def __init__(self, root: str | None = None, session: str | None = None, tools=("left", "right"),
                 max_opening: float | tuple | None = None, up_axis: int = 2):
        """tools: their names, in the order of the arrays given to `add`. max_opening [m]: per tool or one value (for
        the open / closed thresholds). up_axis: which axis of the recorded coordinates points up (0 x, 1 y, 2 z)."""
        self.up_axis = int(up_axis)
        self.root = data_dir(root)
        self.session = session or time.strftime("%Y%m%d-%H%M")
        self.dir = os.path.join(self.root, self.session)
        self.tools = tuple(tools)
        n = len(self.tools)
        self.max_opening = None if max_opening is None else tuple(np.broadcast_to(np.asarray(max_opening, float), (n,)).tolist())
        self.recording = False
        self._rows: dict[str, list] = {}
        self._start: dict = {}
        self._t0 = 0.0
        self._started = ""
        self.steps = 0
        self._saver: threading.Thread | None = None
        self.last: dict | None = None
        self.take = self._next_take()

    # ---------------------------------------------------------------------------------------- session, index
    def write_session(self, info: dict) -> None:
        os.makedirs(self.dir, exist_ok=True)
        info = dict(info, session=self.session, created=time.strftime("%Y-%m-%d %H:%M:%S"), tools=list(self.tools))
        with open(os.path.join(self.dir, "session.json"), "w") as f:
            json.dump(info, f, indent=1, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))

    def _index_path(self) -> str:
        return os.path.join(self.root, "index.json")

    def index(self) -> list:
        return read_index(self.root)

    def _next_take(self) -> int:
        return 1 + max([t["take"] for t in self.index() if t["session"] == self.session], default=0)

    @property
    def n_saved(self) -> int:
        return sum(1 for t in self.index() if t["session"] == self.session)

    # ---------------------------------------------------------------------------------------- recording
    @property
    def elapsed(self) -> float:
        return time.time() - self._t0 if self.recording else 0.0

    def start(self, start_state: dict | None = None) -> None:
        self._rows, self.steps = {}, 0
        self._start = {f"start/{k}": np.asarray(v) for k, v in (start_state or {}).items()}
        self._t0, self._started = time.time(), time.strftime("%Y%m%d-%H%M%S")
        self.recording = True

    def add(self, t: float, tool_pos, tool_quat=None, *, opening=None, opening_cmd=None, grip_state=None, target_pos=None,
            target_quat=None, tool_state=None, ctrl_pos=None, ctrl_quat=None, trigger=None, squeeze=None, buttons=None,
            extra: dict | None = None) -> None:
        """One step. Arrays have the leading dimension n_tools. Give the same fields at every step of a take."""
        if not self.recording:
            return
        row = {"t": np.float64(t), "t_wall": np.float64(time.time() - self._t0), "tool/pos": np.asarray(tool_pos, np.float32)}
        for k, v, dt in (("tool/quat", tool_quat, np.float32), ("tool/opening", opening, np.float32),
                         ("tool/opening_cmd", opening_cmd, np.float32), ("tool/grip_state", grip_state, np.int8),
                         ("tool/target_pos", target_pos, np.float32), ("tool/target_quat", target_quat, np.float32),
                         ("tool/state", tool_state, np.int8), ("ctrl/pos", ctrl_pos, np.float32),
                         ("ctrl/quat", ctrl_quat, np.float32), ("ctrl/trigger", trigger, np.float32),
                         ("ctrl/squeeze", squeeze, np.float32), ("ctrl/buttons", buttons, np.int32)):
            if v is not None:
                row[k] = np.asarray(v, dt)
        for k, v in (extra or {}).items():
            row[f"extra/{k}"] = np.asarray(v)
        for k, v in row.items():
            self._rows.setdefault(k, []).append(v)
        self.steps += 1

    def extra_rows(self) -> dict:
        """The application's per-step data recorded so far: {name: list of arrays}."""
        return {k[len("extra/"):]: v for k, v in self._rows.items() if k.startswith("extra/")}

    def discard(self) -> None:
        self.recording, self._rows, self.steps = False, {}, 0

    def stop(self, info: dict | None = None) -> dict | None:
        """End the take and write it (in the background). Returns its index entry, or None when nothing was
        recorded. `info` is merged into the entry (e.g. validity flags, configuration name)."""
        if not self.recording:
            return None
        self.recording = False
        if self.steps == 0:
            return None
        rows, start, steps = self._rows, self._start, self.steps
        self._rows = {}
        name = f"take_{self.take:03d}_{self._started}.npz"
        t = np.asarray(rows["t"], float)
        seconds = float(t[-1] - t[0]) if steps > 1 else 0.0
        rate = (steps - 1) / seconds if seconds > 0 else 0.0
        entry = dict(file=os.path.join(self.session, name), session=self.session, take=self.take, started=self._started,
                     steps=steps, seconds=seconds, wall_seconds=float(rows["t_wall"][-1]), rate_hz=round(rate, 3),
                     tools=list(self.tools), **(info or {}))
        meta = dict(tools=list(self.tools), max_opening=self.max_opening, rate_hz=rate, session=self.session, take=self.take,
                    started=self._started, up_axis=self.up_axis)
        self.take += 1
        self.last = entry
        os.makedirs(self.dir, exist_ok=True)
        takes = self.index() + [entry]
        tmp = self._index_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dict(version=2, takes=takes), f, indent=1, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
        os.replace(tmp, self._index_path())
        max_open = self.max_opening

        def save():
            arrays = {}
            for k, v in rows.items():
                a = np.stack(v)
                arrays[k] = a.astype(np.float32) if a.dtype == np.float64 and k not in ("t", "t_wall") else a
            if "tool/grip_state" not in arrays:
                src = arrays.get("tool/opening_cmd", arrays.get("tool/opening"))
                if src is not None:
                    mx = max_open if max_open is not None else np.maximum(src.max(0), 1e-6).tolist()
                    arrays["tool/grip_state"] = np.stack([derive_grip_state(src[:, i], mx[i]) for i in range(src.shape[1])], 1)
            arrays.update(start)
            arrays["meta"] = np.array(json.dumps(meta))
            np.savez_compressed(os.path.join(self.dir, name), **arrays)
        self._saver = threading.Thread(target=save, daemon=False, name="take-writer")
        self._saver.start()
        return entry

    def delete_last(self) -> dict | None:
        """Remove the newest take of this session (file and index entry)."""
        self.wait()
        takes = self.index()
        mine = [t for t in takes if t["session"] == self.session]
        if not mine:
            return None
        last = max(mine, key=lambda t: t["take"])
        takes.remove(last)
        with open(self._index_path(), "w") as f:
            json.dump(dict(version=2, takes=takes), f, indent=1)
        try:
            os.remove(os.path.join(self.root, last["file"]))
        except OSError:
            pass
        self.take = self._next_take()
        return last

    def wait(self) -> None:
        if self._saver is not None:
            self._saver.join()


def read_index(root: str) -> list:
    try:
        with open(os.path.join(root, "index.json")) as f:
            return json.load(f)["takes"]
    except (OSError, ValueError, KeyError):
        return []


def load_take(path: str, payload: bool = True) -> dict:
    """{name: array} of one take file. payload=False skips the application's arrays (`extra/...`, `start/...`), which
    can be most of the file; their names are then listed under the key "extra_keys"."""
    with np.load(path, allow_pickle=False) as z:
        if payload:
            return {k: z[k] for k in z.files}
        out = {k: z[k] for k in z.files if not k.startswith(("extra/", "start/"))}
        out["extra_keys"] = sorted(k[len("extra/"):] for k in z.files if k.startswith("extra/"))
        return out


def take_meta(take: dict) -> dict:
    try:
        return json.loads(str(take["meta"]))
    except (KeyError, ValueError):
        return {}


def take_summary(take: dict) -> dict:
    """Numbers about a take: per tool the path length [m], how often the gripper closed and opened, the share of
    steps spent following the hand; durations and rate."""
    meta = take_meta(take)
    t = np.asarray(take["t"], float)
    pos = np.asarray(take["tool/pos"], float)
    n = pos.shape[1]
    tools = meta.get("tools") or [f"tool {i}" for i in range(n)]
    out = dict(steps=len(t), seconds=float(t[-1] - t[0]) if len(t) > 1 else 0.0,
               wall_seconds=float(take["t_wall"][-1]) if "t_wall" in take else None,
               rate_hz=(len(t) - 1) / max(float(t[-1] - t[0]), 1e-9) if len(t) > 1 else 0.0, tools=[])
    gs = take.get("tool/grip_state")
    for i in range(n):
        p = pos[:, i]
        d = dict(name=tools[i], path_m=float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum()),
                 extent_m=(p.max(0) - p.min(0)).tolist())
        if gs is not None:
            g = np.asarray(gs[:, i])
            d["closes"] = int(((g[1:] == GRIP_CLOSED) & (g[:-1] != GRIP_CLOSED)).sum())
            d["opens"] = int(((g[1:] == GRIP_OPEN) & (g[:-1] != GRIP_OPEN)).sum())
            d["closed_share"] = float((g == GRIP_CLOSED).mean())
        if "tool/state" in take:
            s = np.asarray(take["tool/state"][:, i])
            d["following_share"] = float((s == 1).mean())
            d["lost_events"] = int(((s[1:] == 2) & (s[:-1] != 2)).sum())
        out["tools"].append(d)
    return out

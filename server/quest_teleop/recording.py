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

Managing the folder (the status page and command-line tools use these; all of them keep index.json and the files on
disk in step, write the index atomically under a lock, and never touch anything outside the folder):

    reconcile(root)                         index.json := what is on disk (missing files dropped, unlisted takes added)
    delete_takes(root, [files], ...)        remove takes: the .npz, sidecar files, the index entry
    rename_take(root, file, title=..., note=..., new_name=...)     display name and note; optionally the file name
    take_files(root, file)                  the files that belong to a take
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
        self.take = self._next_take()                  # takes may have been deleted since the last one
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
        final = os.path.join(self.dir, name)
        open(final + ".part", "wb").close()            # "being written": the index entry is kept, the take cannot be deleted
        with index_lock(self.root):
            write_index(self.root, read_index(self.root) + [entry])
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
            with open(final + ".part", "wb") as f:     # complete file or none: the name appears only when it is whole
                np.savez_compressed(f, **arrays)
            os.replace(final + ".part", final)
        self._saver = threading.Thread(target=save, daemon=False, name="take-writer")
        self._saver.start()
        return entry

    def delete_last(self) -> dict | None:
        """Remove the newest take of this session (its file, sidecar files and index entry)."""
        self.wait()
        mine = [t for t in self.index() if t["session"] == self.session]
        if not mine:
            return None
        last = max(mine, key=lambda t: t["take"])
        delete_takes(self.root, [last["file"]])
        self.refresh()
        return last

    def refresh(self) -> None:
        """Take numbering from the index again (after takes were deleted or the folder changed)."""
        if not self.recording:
            self.take = self._next_take()

    def busy_files(self) -> list:
        """Takes that must not be deleted or renamed now: the one being recorded (the file it will get)."""
        if self.recording:
            return [os.path.join(self.session, f"take_{self.take:03d}_{self._started}.npz")]
        return []

    def wait(self) -> None:
        if self._saver is not None:
            self._saver.join()


# ------------------------------------------------------------------------------------------------ the folder
class index_lock:
    """Exclusive lock on a recordings folder's index (advisory, `<root>/index.json.lock`), so that the recorder, the
    status page and command-line tools do not write it at the same time."""

    def __init__(self, root: str):
        self.path = os.path.join(root, "index.json.lock")
        self._f = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._f = open(self.path, "a")
        try:
            import fcntl
            fcntl.flock(self._f, fcntl.LOCK_EX)
        except (ImportError, OSError):                 # no flock on this platform: best effort
            pass
        return self

    def __exit__(self, *exc):
        try:
            import fcntl
            fcntl.flock(self._f, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        self._f.close()


def _write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    os.replace(tmp, path)


def write_index(root: str, takes: list) -> None:
    """Replace index.json atomically. Hold `index_lock(root)` around read-modify-write."""
    os.makedirs(root, exist_ok=True)
    _write_json(os.path.join(root, "index.json"), dict(version=2, takes=takes))


def read_converted(root: str) -> list:
    """Datasets an application derived from takes (`<root>/converted/index.json`: name, out folder, takes)."""
    try:
        with open(os.path.join(root, "converted", "index.json")) as f:
            return json.load(f)["datasets"]
    except (OSError, ValueError, KeyError):
        return []


def _write_converted(root: str, datasets: list) -> None:
    os.makedirs(os.path.join(root, "converted"), exist_ok=True)
    _write_json(os.path.join(root, "converted", "index.json"), dict(version=1, datasets=datasets))


def _inside(root: str, path: str) -> bool:
    """True when `path`, with every symbolic link resolved, lies inside `root`."""
    rr = os.path.realpath(root)
    return os.path.realpath(path).startswith(rr + os.sep)


def _sidecar_path(root: str, rel: str) -> str:
    return os.path.join(root, os.path.splitext(rel)[0] + ".json")


_checked: dict = {}                                    # path -> (mtime, size, is a take file)


def _is_take_file(path: str) -> bool:
    """A readable .npz with the arrays every take has (checked once per file version)."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    key = (st.st_mtime_ns, st.st_size)
    hit = _checked.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with np.load(path, allow_pickle=False) as z:
            ok = "t" in z.files and "tool/pos" in z.files
    except Exception:
        ok = False
    _checked[path] = (key, ok)
    return ok


def _entry_from_file(root: str, rel: str) -> dict:
    """An index entry for a take file that is not in the index, from the file itself and its sidecar."""
    path = os.path.join(root, rel)
    with np.load(path, allow_pickle=False) as z:
        t = np.asarray(z["t"], float)
        wall = float(z["t_wall"][-1]) if "t_wall" in z.files else 0.0
        meta = json.loads(str(z["meta"])) if "meta" in z.files else {}
        n_tools = z["tool/pos"].shape[1]
    session = os.path.dirname(rel).replace(os.sep, "/") or meta.get("session", "")
    seconds = float(t[-1] - t[0]) if len(t) > 1 else 0.0
    started = meta.get("started") or time.strftime("%Y%m%d-%H%M%S", time.localtime(os.path.getmtime(path)))
    entry = dict(file=rel, session=session, take=int(meta.get("take", 0)), started=started, steps=int(len(t)), seconds=seconds,
                 wall_seconds=wall, rate_hz=round((len(t) - 1) / seconds, 3) if seconds > 0 else 0.0,
                 tools=meta.get("tools") or [f"tool {i}" for i in range(n_tools)], recovered=True)
    try:
        with open(_sidecar_path(root, rel)) as f:
            side = json.load(f)
        entry.update({k: side[k] for k in ("title", "note") if side.get(k)})
    except (OSError, ValueError):
        pass
    return entry


def reconcile(root: str) -> dict:
    """Make index.json say what is on disk: entries whose file is gone are dropped (unless the take is still being
    written), take files that are not listed are added (read from the file), and derived datasets whose folder is
    gone are dropped from `converted/index.json`. Cheap when nothing changed; safe to call often.
    Returns {"removed": [files], "added": [files], "converted_removed": [names]}."""
    out = dict(removed=[], added=[], converted_removed=[])
    if not os.path.isdir(root):
        return out
    with index_lock(root):
        takes = read_index(root)
        keep = []
        for t in takes:
            path = os.path.join(root, t.get("file", ""))
            if os.path.isfile(path) or os.path.exists(path + ".part"):
                keep.append(t)
            else:
                out["removed"].append(t.get("file"))
        listed = {t["file"] for t in keep}
        for session in sorted(os.listdir(root)):
            sdir = os.path.join(root, session)
            if session == "converted" or session.startswith(".") or not os.path.isdir(sdir) or os.path.islink(sdir):
                continue
            for name in sorted(os.listdir(sdir)):
                rel = os.path.join(session, name)
                if not name.endswith(".npz") or rel in listed or os.path.islink(os.path.join(sdir, name)):
                    continue
                if _is_take_file(os.path.join(sdir, name)):
                    try:
                        keep.append(_entry_from_file(root, rel))
                        out["added"].append(rel)
                    except Exception:                  # unreadable after all: leave it alone
                        pass
        if out["removed"] or out["added"]:
            write_index(root, keep)
        datasets = read_converted(root)
        alive = [d for d in datasets if os.path.isdir(d.get("out", "")) and os.listdir(d["out"])]
        if len(alive) != len(datasets):
            out["converted_removed"] = [d.get("name") for d in datasets if d not in alive]
            _write_converted(root, alive)
    return out


def take_files(root: str, rel: str) -> list:
    """The files that belong to one take (relative to root): the .npz and its sidecars, i.e. the other files in the
    same folder named `<take file without .npz>.<anything>` or `<take file without .npz>_<anything>` (notes, renders,
    exports). Another take's .npz is never a sidecar, and a file that fits a longer take name belongs to that take."""
    folder, name = os.path.split(rel)
    stem = os.path.splitext(name)[0]
    out = [rel]
    try:
        names = sorted(os.listdir(os.path.join(root, folder)))
    except OSError:
        return out
    longer = [os.path.splitext(n)[0] for n in names if n.endswith(".npz") and n != name and len(n) - 4 > len(stem)]
    for other in names:
        if other == name or other.endswith((".npz", ".part")) or not other.startswith((stem + ".", stem + "_")):
            continue
        if any(other.startswith((s + ".", s + "_")) for s in longer):
            continue
        out.append(os.path.join(folder, other))
    return out


def _check_take_path(root: str, rel: str) -> str:
    """The absolute path of a take given relative to root; ValueError when it is not a take inside the folder."""
    if not rel or os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/") or not rel.endswith(".npz"):
        raise ValueError("not a take of this folder")
    path = os.path.join(root, rel)
    if os.path.islink(path) or not _inside(root, path):
        raise ValueError("not a take of this folder")
    return path


def delete_takes(root: str, files, also_converted: bool = False, busy=()) -> dict:
    """Delete takes for good: the .npz, its sidecar files and its index entry. Only files inside `root` are ever
    removed. `busy`: takes that must not be touched (e.g. `recorder.busy_files()`); a take that is still being
    written is refused too. also_converted=True also removes the derived datasets that contain a deleted take
    (their folders under `<root>/converted` and their entries).
    Returns {"deleted": [files], "refused": [{"file", "reason"}], "removed": [paths], "converted_removed": [names]}."""
    out = dict(deleted=[], refused=[], removed=[], converted_removed=[])
    busy = set(busy)
    with index_lock(root):
        takes = read_index(root)
        for rel in files:
            try:
                path = _check_take_path(root, rel)
            except ValueError as e:
                out["refused"].append(dict(file=rel, reason=str(e)))
                continue
            if rel in busy or os.path.exists(path + ".part"):
                out["refused"].append(dict(file=rel, reason="the take is being recorded or written"))
                continue
            if not os.path.isfile(path) and not any(t.get("file") == rel for t in takes):
                out["refused"].append(dict(file=rel, reason="no such take"))
                continue
            for f in take_files(root, rel):
                fp = os.path.join(root, f)
                if os.path.isfile(fp) and not os.path.islink(fp) and _inside(root, fp):
                    os.remove(fp)
                    out["removed"].append(f)
            takes = [t for t in takes if t.get("file") != rel]
            out["deleted"].append(rel)
        if out["deleted"]:
            write_index(root, takes)
            if also_converted:
                import shutil
                gone, datasets, keep = set(out["deleted"]), read_converted(root), []
                conv_root = os.path.join(root, "converted")
                for d in datasets:
                    if gone & set(d.get("takes", [])):
                        folder = d.get("out", "")
                        if os.path.isdir(folder) and not os.path.islink(folder) and _inside(conv_root, folder):
                            shutil.rmtree(folder)
                        out["converted_removed"].append(d.get("name"))
                    else:
                        keep.append(d)
                if len(keep) != len(datasets):
                    _write_converted(root, keep)
    return out


def sanitize_name(name: str, max_len: int = 80) -> str:
    """A safe file name (without extension) from what a person typed: letters, digits, space, dot, dash and
    underscore only; no path separators, no leading dot, at most max_len characters. ValueError when nothing is left."""
    import re
    name = os.path.splitext(name)[0] if name.lower().endswith(".npz") else name
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("the file name must not contain / \\ or ..")
    clean = re.sub(r"[^A-Za-z0-9 ._-]+", "_", name).strip(" .")
    clean = re.sub(r"\s+", " ", clean)[:max_len].strip(" .")
    if not clean:
        raise ValueError("the file name is empty")
    return clean


def rename_take(root: str, rel: str, title: str | None = None, note: str | None = None, new_name: str | None = None,
                busy=()) -> dict:
    """Give a take a display name (`title`) and optionally a note; they are stored in the index entry and in a small
    sidecar next to the take (`<take>.json`), so they survive a rebuilt index. The take file keeps its name unless
    `new_name` is given: then the .npz and its sidecars are renamed inside their folder (name sanitised, never over
    an existing file) and the index and the derived-dataset references follow. Returns the updated index entry.
    ValueError explains a refusal (not a take of this folder, being recorded, name taken, bad name)."""
    with index_lock(root):
        path = _check_take_path(root, rel)
        takes = read_index(root)
        entry = next((t for t in takes if t.get("file") == rel), None)
        if entry is None or not os.path.isfile(path):
            raise ValueError("this take no longer exists")
        if rel in set(busy) or os.path.exists(path + ".part"):
            raise ValueError("the take is being recorded or written")
        if title is not None:
            title = " ".join(str(title).split())[:120]
            entry["title"] = title
            if not title:
                entry.pop("title", None)
        if note is not None:
            note = str(note).strip()[:2000]
            entry["note"] = note
            if not note:
                entry.pop("note", None)
        new_rel = rel
        if new_name is not None:
            clean = sanitize_name(new_name)
            folder = os.path.dirname(rel)
            new_rel = os.path.join(folder, clean + ".npz")
            if new_rel != rel:
                new_path = os.path.join(root, new_rel)
                if not _inside(root, new_path):
                    raise ValueError("the new name leaves the recordings folder")
                old_stem, new_stem = os.path.splitext(os.path.basename(rel))[0], clean
                moves = [(f, os.path.join(folder, new_stem + os.path.basename(f)[len(old_stem):])) for f in take_files(root, rel)]
                taken = [dst for _, dst in moves if os.path.lexists(os.path.join(root, dst))]
                taken += take_files(root, new_rel)[1:]         # files that would count as the renamed take's own
                if taken:
                    raise ValueError(f"a file named {os.path.basename(taken[0])} already exists")
                for src, dst in moves:
                    os.rename(os.path.join(root, src), os.path.join(root, dst))
                entry["file"] = new_rel
                entry.setdefault("original_file", rel)
        side = {k: entry[k] for k in ("title", "note", "original_file") if entry.get(k)}
        sp = _sidecar_path(root, new_rel)
        if side:
            _write_json(sp, side)
        elif os.path.isfile(sp):
            os.remove(sp)
        write_index(root, takes)
        if new_rel != rel:
            datasets, changed = read_converted(root), False
            for d in datasets:
                if rel in d.get("takes", []):
                    d["takes"] = [new_rel if f == rel else f for f in d["takes"]]
                    changed = True
            if changed:
                _write_converted(root, datasets)
        return entry


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

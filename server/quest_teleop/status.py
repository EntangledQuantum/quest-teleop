"""A local status page for a running `TeleopServer`: who is connected, how fast everything runs, where the latency
goes, what is being recorded. Served by the server process itself on localhost; standard library only, no build
step, nothing fetched from the internet.

    page = StatusPage(server, port=8800, extra=my_callback).start()     # http://localhost:8800

Once per second the page thread calls `server.monitor()` and `extra()` and pushes the result to every open browser
tab (server-sent events). `extra()` returns the application's part (all optional):

    {"session":    {"name": ..., "state": "running" | "paused" | "recording" | "idle", "state_text": ...,
                    "rows": [[label, value], ...]},
     "sim":        {"rtf": real-time factor, "steps_per_s": ..., "contact_n": contact force [N] ...},
     "recordings": {"dir": ..., "columns": [...], "rows": [[...], ...], "note": ...}}

With `recordings_dir` (a `quest_teleop.recording` folder) the page lists the takes, and each one opens in the
trajectory view: the tools' paths in 3D with the gripper state marked along them, and the take's properties.

Endpoints: `/` the page, `/api/state` the newest sample (JSON), `/api/history` the last minutes, `/events` the live
stream, `/take?file=...` the trajectory view, `/api/take?file=...` its data, `/fonts/*` the page's two typefaces.
"""
from __future__ import annotations

import collections
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status_page")


def _clean(o):
    """JSON without NaN / inf and without numpy types."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):
        try:
            o = o.item()
        except Exception:
            return str(o)
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    return o


def recordings_table(root: str, current_session: str | None = None) -> dict:
    """The takes of a recordings folder as the page's table (newest first)."""
    from .recording import read_index
    takes = sorted(read_index(root), key=lambda t: (t.get("started", ""), t.get("take", 0)), reverse=True)
    derived = {}
    try:                                   # optional: datasets an application made from takes
        with open(os.path.join(root, "converted", "index.json")) as f:
            for d in json.load(f)["datasets"]:
                for tf in d.get("takes", []):
                    derived.setdefault(tf, []).append(d["name"])
    except (OSError, ValueError, KeyError):
        pass
    rows, total = [], 0
    for t in takes:
        try:
            size = os.path.getsize(os.path.join(root, t["file"]))
        except OSError:
            size = None
        total += size or 0
        if "valid" not in t:
            val = dict(kind="none", text="not checked")
        elif t["valid"]:
            val = dict(kind="good", text="valid")
        else:
            val = dict(kind="bad", text="flagged: " + ", ".join(map(str, t.get("flags", []))))
        st = t.get("started", "")
        conv = derived.get(t["file"])
        secs = t.get("seconds", t.get("sim_seconds", 0.0))
        rows.append([t["session"] + ("  (this run)" if t["session"] == current_session else ""),
                     dict(text=f"take {t['take']}", href="/take?file=" + t["file"], label=f"Open take {t['take']} of {t['session']}"),
                     f"{st[:4]}-{st[4:6]}-{st[6:8]} {st[9:11]}:{st[11:13]}:{st[13:15]}" if len(st) >= 15 else st,
                     f"{secs:.1f} s", f"{t.get('wall_seconds', 0):.0f} s", t["steps"],
                     ("writing" if size is None else (f"{size / 1e3:.0f} kB" if size < 1e6 else f"{size / 1e6:.1f} MB")), val,
                     dict(kind="good", text=", ".join(conv)) if conv else dict(kind="none", text="no"), t["file"]])
    cols = [dict(name="Session"), dict(name="Take", link=True), dict(name="Recorded"), dict(name="Duration", num=True),
            dict(name="Wall time", num=True), dict(name="Steps", num=True), dict(name="Size", num=True),
            dict(name="Validity", tag=True), dict(name="Converted", tag=True), dict(name="File", mono=True)]
    return dict(dir=root, columns=cols, rows=rows, summary=f"{sum(t['steps'] for t in takes)} steps, {total / 1e6:.0f} MB",
                note="Select a take to see its trajectory.")


def take_json(root: str | None, rel: str, max_points: int = 4000) -> dict:
    """One take for the trajectory view: its properties and the tools' paths (thinned to max_points)."""
    import numpy as np

    from .recording import GRIP_NAMES, load_take, read_index, take_meta, take_summary
    if not root:
        raise ValueError("this server has no recordings folder")
    path = os.path.abspath(os.path.join(root, rel))
    if not path.startswith(root + os.sep) or not path.endswith(".npz") or not os.path.isfile(path):
        raise ValueError("no such take")
    take = load_take(path, payload=False)                  # the payload can be large and is not shown
    extra = take.pop("extra_keys")
    if "tool/pos" not in take:
        raise ValueError("this take has no tool poses (it was recorded before the generic format)")
    entry = next((t for t in read_index(root) if t["file"] == rel), {})
    meta, summ = take_meta(take), take_summary(take)
    T = len(take["t"])
    step = max(1, int(np.ceil(T / max_points)))
    idx = np.arange(0, T, step)
    if idx[-1] != T - 1:
        idx = np.r_[idx, T - 1]
    r4 = lambda a: np.round(np.asarray(a, float), 4).tolist()                                  # noqa: E731
    tools = []
    for i, d in enumerate(summ["tools"]):
        tl = dict(d, pos=r4(take["tool/pos"][idx, i]))
        for key, name in (("tool/grip_state", "grip"), ("tool/state", "state")):
            if key in take:
                tl[name] = np.asarray(take[key][idx, i]).astype(int).tolist()
        for key, name in (("tool/opening", "opening"), ("tool/opening_cmd", "opening_cmd")):
            if key in take:
                tl[name] = r4(take[key][idx, i])
        if "ctrl/pos" in take:
            c = np.asarray(take["ctrl/pos"][idx, i], float)
            tl["ctrl"] = [None if not np.isfinite(v).all() else np.round(v, 4).tolist() for v in c]
        tools.append(tl)
    return dict(file=rel, entry=entry, meta=meta, summary={k: v for k, v in summ.items() if k != "tools"},
                size_bytes=os.path.getsize(path), t=np.round(np.asarray(take["t"], float)[idx] - float(take["t"][0]), 4).tolist(),
                tools=tools, grip_names=list(GRIP_NAMES), up_axis=int(meta.get("up_axis", 2)),
                max_opening=meta.get("max_opening"), extra_keys=extra, thinned_by=step)


class StatusPage:
    def __init__(self, server, port: int = 8800, host: str = "127.0.0.1", extra=None, title: str = "Teleoperation",
                 period: float = 1.0, history_s: float = 600.0, recordings_dir: str | None = None):
        """recordings_dir: a `quest_teleop.recording` data folder. Its takes are listed on the page and each one opens
        in the trajectory view (`/take?file=...`)."""
        self.server, self.port, self.host, self.extra, self.title, self.period = server, port, host, extra, title, period
        self.recordings_dir = os.path.abspath(recordings_dir) if recordings_dir else None
        self.history = collections.deque(maxlen=int(history_s / period))
        self.latest: dict = {}
        self._cond = threading.Condition()
        self._seq = 0
        self._running = False
        self._httpd = None

    @property
    def url(self) -> str:
        return f"http://{'localhost' if self.host in ('127.0.0.1', '0.0.0.0') else self.host}:{self.port}"

    def start(self) -> "StatusPage":
        page = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):                       # quiet
                pass

            def _send(self, code, body: bytes, ctype: str, cache: str = "no-store"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/", "/index.html"):
                    with open(os.path.join(_DIR, "index.html"), "rb") as f:
                        self._send(200, f.read().replace(b"{{TITLE}}", page.title.encode()), "text/html; charset=utf-8")
                elif path.startswith("/fonts/") and "/" not in path[7:] and ".." not in path:
                    fp = os.path.join(_DIR, "fonts", path[7:])
                    if os.path.isfile(fp):
                        with open(fp, "rb") as f:
                            self._send(200, f.read(), "font/ttf", "max-age=86400")
                    else:
                        self._send(404, b"not found", "text/plain")
                elif path == "/take":
                    with open(os.path.join(_DIR, "take.html"), "rb") as f:
                        self._send(200, f.read().replace(b"{{TITLE}}", page.title.encode()), "text/html; charset=utf-8")
                elif path == "/api/take":
                    from urllib.parse import parse_qs, urlparse
                    rel = (parse_qs(urlparse(self.path).query).get("file") or [""])[0]
                    try:
                        body = json.dumps(_clean(take_json(page.recordings_dir, rel))).encode()
                        self._send(200, body, "application/json")
                    except (OSError, ValueError, KeyError) as e:
                        self._send(404, json.dumps(dict(error=str(e))).encode(), "application/json")
                elif path == "/api/state":
                    self._send(200, json.dumps(page.latest).encode(), "application/json")
                elif path == "/api/history":
                    self._send(200, json.dumps(list(page.history)).encode(), "application/json")
                elif path == "/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    seen = -1
                    try:
                        while page._running:
                            with page._cond:
                                page._cond.wait_for(lambda: page._seq != seen or not page._running, timeout=15.0)
                                changed, seen, data = page._seq != seen, page._seq, page.latest
                            msg = f"data: {json.dumps(data)}\n\n" if changed else ": keep-alive\n\n"
                            self.wfile.write(msg.encode())
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                else:
                    self._send(404, b"not found", "text/plain")

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._running = True
        threading.Thread(target=self._httpd.serve_forever, daemon=True, name="status-http").start()
        threading.Thread(target=self._sample_loop, daemon=True, name="status-sample").start()
        return self

    def stop(self) -> None:
        self._running = False
        with self._cond:
            self._cond.notify_all()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def _sample_loop(self) -> None:
        nxt = time.perf_counter()
        while self._running:
            nxt += self.period
            time.sleep(max(0.0, nxt - time.perf_counter()))
            try:
                s = self.server.monitor()
                if self.extra is not None:
                    s.update(self.extra() or {})
                if self.recordings_dir and "recordings" not in s:
                    s["recordings"] = recordings_table(self.recordings_dir, (s.get("session") or {}).get("name"))
                s = _clean(s)
            except Exception as e:                           # the page must never take the server down
                s = dict(time=time.time(), error=f"{type(e).__name__}: {e}")
            lat, sim, ses = s.get("latency_ms") or {}, s.get("sim") or {}, s.get("session") or {}
            point = dict(t=s.get("time"), rtf=sim.get("rtf"), steps=sim.get("steps_per_s"), contact=sim.get("contact_n"),
                         down=s.get("down_mbit"), up=s.get("up_mbit"), fps=s.get("headset_fps"), total=lat.get("total"),
                         link=lat.get("link"), wait=lat.get("wait"), step=lat.get("sim_step"), proc=lat.get("processing"),
                         rec=ses.get("state") == "recording", connected=bool(s.get("clients")))
            self.history.append(point)
            s["point"] = point
            with self._cond:
                self.latest = s
                self._seq += 1
                self._cond.notify_all()

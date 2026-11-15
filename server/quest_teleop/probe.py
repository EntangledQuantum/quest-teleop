"""A headless reference client in Python: speaks the full protocol, keeps the decoded scene state, sends synthetic
controller input, and measures latency and bandwidth. For tests and for checking a server without a headset.

    python -m quest_teleop.probe --host 127.0.0.1 --seconds 10 --motion circle

`ProbeClient.poll()` must be called regularly (it is single-threaded: it reads the sockets, applies state, and
acknowledges). The decoded state is in `.pose` (N, 7), `.points[node]`, `.rgba`, `.flags`, `.texts`, `.haptics`.
"""
from __future__ import annotations

import argparse
import select
import socket
import struct
import time

import numpy as np

from . import protocol as P


class ProbeClient:
    def __init__(self, host: str | None = None, port: int = P.DEFAULT_PORT, name: str = "probe", tcp_only: bool = False,
                 input_hz: float = 72.0, discover_timeout: float = 3.0, drop: float = 0.0, seed: int = 0):
        """drop: fraction of incoming state datagrams to throw away (to test recovery from packet loss)."""
        self.drop, self._rng = drop, np.random.default_rng(seed)
        if host is None:
            host, port = discover(discover_timeout)
        self.host, self.port, self.tcp_only, self.input_dt = host, port, tcp_only, 1.0 / input_hz
        self.tcp = socket.create_connection((host, port), timeout=5.0)
        self.tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("0.0.0.0", 0))
        self.udp.setblocking(False)
        self.tcp.sendall(P.frame_msg(P.M_HELLO, struct.pack("<HHHf", P.MAGIC, P.VERSION, P.HF_TCP_ONLY if tcp_only else 0,
                                                             input_hz) + P.pack_str(name) + P.pack_str("python probe")))
        self._rx = bytearray()
        self.session = 0
        self.server_name = ""
        self.scene_id, self.scene_name, self.loaded = -1, "", False
        self.meshes: dict[int, dict] = {}
        self.nodes: list[dict] = []
        self.pose = np.zeros((0, 7))
        self.rgba = np.zeros((0, 4), np.uint8)
        self.flags = np.zeros(0, np.uint16)
        self.points: dict[int, np.ndarray] = {}
        self.texts: dict[int, str] = {}
        self.panel: dict | None = None
        self.haptics = [dict(seq=0, amplitude=0.0, duration=0.0, count=0) for _ in range(2)]
        self.newest_frame, self._frags, self.ack_frame = 0, set(), 0
        self.input = P.InputFrame()
        self._seq, self._next_input = 0, time.perf_counter()
        self.latency_ms: list[float] = []          # echo latency per state frame (input sent -> resulting state here)
        self.hold_ms: list[float] = []             # server-side part of it
        self.frame_times: list[float] = []
        self.rx_bytes = self.rx_dgrams = self.tx_bytes = self.frames = self.lost_frames = 0
        self.def_bytes = 0
        self._last_latency_us = 0
        self.tcp.setblocking(False)

    # ---------------------------------------------------------------------------------------- loop
    def poll(self, timeout: float = 0.0) -> None:
        r, _, _ = select.select([self.tcp, self.udp], [], [], timeout)
        if self.tcp in r:
            try:
                data = self.tcp.recv(1 << 20)
            except BlockingIOError:
                data = None
            if data == b"":
                raise ConnectionError("server closed the connection")
            if data:
                self._rx += data
                while len(self._rx) >= 6:
                    n, mtype = struct.unpack_from("<IH", self._rx, 0)
                    if len(self._rx) < 4 + n:
                        break
                    body = bytes(self._rx[6:4 + n])
                    del self._rx[:4 + n]
                    self._on_msg(mtype, body)
        if self.udp in r:
            while True:
                try:
                    d, _ = self.udp.recvfrom(2048)
                except BlockingIOError:
                    break
                self._on_dgram(d)
        now = time.perf_counter()
        if self.session and now >= self._next_input:
            self._next_input = max(self._next_input + self.input_dt, now - self.input_dt)
            self.send_input()

    def run(self, seconds: float, on_tick=None) -> None:
        t_end = time.perf_counter() + seconds
        while time.perf_counter() < t_end:
            self.poll(0.002)
            if on_tick:
                on_tick(self)

    def send_input(self) -> None:
        f = self.input
        self._seq += 1
        f.seq, f.client_time_us = self._seq, int(time.perf_counter() * 1e6)
        f.ack_frame, f.ack_scene, f.latency_us = self.ack_frame, max(self.scene_id, 0), self._last_latency_us
        d = P.pack_input(self.session, f)
        self.tx_bytes += len(d)
        if self.tcp_only:
            self._send_tcp(P.frame_msg(P.M_DGRAM, d))
        else:
            self.udp.sendto(d, (self.host, self.port))

    def _send_tcp(self, m: bytes) -> None:
        self.tcp.setblocking(True)
        self.tcp.sendall(m)
        self.tcp.setblocking(False)

    def press(self, action_id: int) -> None:
        """Press a panel action, as the headset does when the operator triggers its button."""
        self._send_tcp(P.frame_msg(P.M_ACTION, struct.pack("<H", action_id)))

    def close(self) -> None:
        try:
            self._send_tcp(P.frame_msg(P.M_BYE))
        except OSError:
            pass
        self.tcp.close()
        self.udp.close()

    # ---------------------------------------------------------------------------------------- reliable channel
    def _on_msg(self, mtype: int, b: bytes) -> None:
        if mtype == P.M_WELCOME:
            _, _, self.session, _, _ = struct.unpack_from("<HHIHf", b, 0)
            self.server_name, _ = P.unpack_str(b, 14)
        elif mtype == P.M_SCENE_BEGIN:
            v = struct.unpack_from("<HBB6f3ff3fHH", b, 0)
            self.scene_id, self.up_axis = v[0], v[1]
            self.lo, self.hi = np.array(v[3:6], np.float32), np.array(v[6:9], np.float32)
            self.focus_center, self.focus_radius = np.array(v[9:12]), v[12]
            self.scene_name, _ = P.unpack_str(b, 60)
            self.meshes, self.nodes, self.points, self.loaded = {}, [], {}, False
            self.newest_frame, self._frags, self.ack_frame = 0, set(), 0
            self.def_bytes = len(b)
        elif mtype == P.M_MESH:
            m = P.unpack_mesh(b)
            self.meshes[m["mesh_id"]] = m
            self.def_bytes += len(b)
        elif mtype == P.M_NODE:
            v = struct.unpack_from("<HBBHH4B3f4f3ffH", b, 0)
            node = dict(id=v[0], kind=v[1], parent=v[2], mesh=v[3], flags=v[4], rgba=v[5:9], pos=np.array(v[9:12]),
                        quat=np.array(v[12:16]), scale=np.array(v[16:19]), param=v[19], n_points=v[20])
            off = 58
            if node["kind"] == P.K_TUBE:
                self.points[v[0]] = np.frombuffer(b, np.float32, v[20] * 3, off).reshape(-1, 3).copy()
                off += v[20] * 12
            elif node["kind"] == P.K_DEFORM:
                self.points[v[0]] = self.meshes[v[3]]["vertices"].copy()
            node["name"], _ = P.unpack_str(b, off)
            self.nodes.append(node)
            self.def_bytes += len(b)
        elif mtype == P.M_SCENE_END:
            n = len(self.nodes)
            self.pose = np.array([np.r_[nd["pos"], nd["quat"]] for nd in self.nodes]).reshape(n, 7)
            self.rgba = np.array([nd["rgba"] for nd in self.nodes], np.uint8).reshape(n, 4)
            self.flags = np.array([nd["flags"] for nd in self.nodes], np.uint16)
            self.loaded = True
            self._send_tcp(P.frame_msg(P.M_SCENE_READY, struct.pack("<H", self.scene_id)))
        elif mtype == P.M_TEXT:
            slot = b[0]
            text, _ = P.unpack_str(b, 28)
            if text:
                self.texts[slot] = text
            else:
                self.texts.pop(slot, None)
        elif mtype == P.M_PANEL:
            self.panel = _unpack_panel(b)
        elif mtype == P.M_DGRAM:
            self._on_dgram(b)
        elif mtype == P.M_BYE:
            raise ConnectionError("server said bye: " + P.unpack_str(b, 0)[0] if b else "bye")

    # ---------------------------------------------------------------------------------------- state
    def _on_dgram(self, d: bytes) -> None:
        if len(d) < P.STATE_OVERHEAD:
            return
        magic, ver, dtype, session = P.DGRAM_HDR.unpack_from(d, 0)
        if magic != P.MAGIC or dtype != P.D_STATE or session != self.session:
            return
        scene, frame, frag, nfrag, t_us, echo_seq, echo_t, hold_us = P.STATE_HEAD.unpack_from(d, 8)
        if scene != self.scene_id or not self.loaded or frame < self.newest_frame:
            return
        if self.drop > 0 and self._rng.random() < self.drop:
            return
        self.rx_bytes += len(d)
        self.rx_dgrams += 1
        if frame > self.newest_frame:
            if self.newest_frame and len(self._frags) and self.ack_frame != self.newest_frame:
                self.lost_frames += 1
            self.newest_frame, self._frags = frame, set()
            now = time.perf_counter()
            self.frame_times.append(now)
            if echo_t:
                lat = now * 1e6 - echo_t
                self._last_latency_us = int(max(0, min(lat, 4e9)))
                self.latency_ms.append(lat / 1e3)
                self.hold_ms.append(hold_us / 1e3)
            off = 8 + P.STATE_HEAD.size
            for i in range(2):
                seq, amp, _, dur, freq = P.STATE_HAPTIC.unpack_from(d, off + 8 * i)
                h = self.haptics[i]
                if seq != h["seq"]:
                    h.update(seq=seq, amplitude=amp / 255.0, duration=dur / 1e3, count=h["count"] + 1)
        self._frags.add(frag)
        off = P.STATE_OVERHEAD
        while off + 4 <= len(d):
            ctype, _, node = P.CHUNK_HDR.unpack_from(d, off)
            off += 4
            if ctype == P.C_POSE:
                q = struct.unpack_from("<3H4h", d, off)
                off += 14
                lo, hi = (self.lo, self.hi) if self.nodes[node]["parent"] == P.P_SCENE else P.LOCAL_BOUNDS
                self.pose[node, :3] = P.dequantize_pos(np.array(q[:3]), lo, hi)
                self.pose[node, 3:] = P.dequantize_quat(np.array(q[3:]))[0]
            elif ctype == P.C_APPEARANCE:
                v = struct.unpack_from("<4BHH", d, off)
                off += 8
                self.rgba[node], self.flags[node] = v[:4], v[4]
            elif ctype == P.C_VERTS:
                first, count = struct.unpack_from("<HH", d, off)
                off += 4
                lo, hi = (self.lo, self.hi) if self.nodes[node]["parent"] == P.P_SCENE else P.LOCAL_BOUNDS
                self.points[node][first:first + count] = P.dequantize_pos(
                    np.frombuffer(d, np.uint16, count * 3, off).reshape(-1, 3), lo, hi)
                off += count * 6
            else:
                break
        if len(self._frags) == nfrag and self.ack_frame != frame:
            self.ack_frame = frame
            self.frames += 1

    def node(self, name: str) -> int:
        return next(n["id"] for n in self.nodes if n["name"] == name)

    def summary(self, seconds: float) -> dict:
        lat, hold = np.array(self.latency_ms or [np.nan]), np.array(self.hold_ms or [np.nan])
        ft = np.diff(self.frame_times) * 1e3 if len(self.frame_times) > 2 else np.array([np.nan])
        return dict(frames_per_s=self.frames / seconds, down_mbit=self.rx_bytes * 8e-6 / seconds,
                    up_kbit=self.tx_bytes * 8e-3 / seconds, dgrams_per_frame=self.rx_dgrams / max(self.frames, 1),
                    bytes_per_frame=self.rx_bytes / max(self.frames, 1), lost_frames=self.lost_frames,
                    latency_ms_median=float(np.median(lat)), latency_ms_p95=float(np.percentile(lat, 95)),
                    latency_ms_max=float(lat.max()), server_hold_ms_median=float(np.median(hold)),
                    frame_interval_ms_p95=float(np.percentile(ft, 95)), definition_kb=self.def_bytes / 1e3)


def _unpack_panel(b: bytes) -> dict:
    out, off = {}, 0
    for k in ("title", "subtitle", "host"):
        out[k], off = P.unpack_str(b, off)
    out["status_style"] = b[off]
    out["status_text"], off = P.unpack_str(b, off + 1)
    n, off = b[off], off + 1
    out["rows"] = []
    for _ in range(n):
        style = b[off]
        label, off = P.unpack_str(b, off + 1)
        value, off = P.unpack_str(b, off)
        out["rows"].append((label, value, style))
    n, off = b[off], off + 1
    out["actions"] = []
    for _ in range(n):
        aid, style, flags = struct.unpack_from("<HBB", b, off)
        label, off = P.unpack_str(b, off + 4)
        hint, off = P.unpack_str(b, off)
        out["actions"].append(dict(id=aid, style=style, flags=flags, label=label, hint=hint))
    n, off = b[off], off + 1
    out["legend"] = []
    for _ in range(n):
        k, off = P.unpack_str(b, off)
        t, off = P.unpack_str(b, off)
        out["legend"].append((k, t))
    return out


def discover(timeout: float = 3.0) -> tuple[str, int]:
    """Find a server on the LAN: broadcast DISCOVER, wait for an OFFER."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.3)
    t_end = time.perf_counter() + timeout
    while time.perf_counter() < t_end:
        for addr in ("255.255.255.255", "127.0.0.1"):
            try:
                s.sendto(P.dgram(P.D_DISCOVER, 0), (addr, P.DEFAULT_PORT))
            except OSError:
                pass
        try:
            d, a = s.recvfrom(2048)
        except socket.timeout:
            continue
        if len(d) >= 10 and d[3] == P.D_OFFER:
            (port,) = struct.unpack_from("<H", d, 8)
            return a[0], port
    raise TimeoutError("no quest-teleop server answered the discovery broadcast")


def set_motion(c: ProbeClient, kind: str, t: float) -> None:
    """Synthetic controller input: both hands tracked; 'circle' holds the right clutch and moves on a 6 cm circle;
    'jump' also teleports the hand 30 cm away between t = 3 s and 5 s (a move the robot cannot follow)."""
    f = c.input
    f.flags = P.IF_FOCUSED
    f.place_pos, f.place_quat, f.place_scale = np.array([0.0, 1.0, -0.5]), np.array([-np.sqrt(0.5), 0, 0, np.sqrt(0.5)]), 1.0
    f.head_pos = np.array([0.0, 1.6, 0.0])
    for i, h in enumerate(f.hands):
        h.flags = P.HAND_ACTIVE | P.HAND_GRIP_VALID | P.HAND_AIM_VALID | P.HAND_TRACKED
        h.grip_pos = np.array([-0.2 + 0.4 * i, 1.2, -0.3])
    if kind == "still":
        return
    h = f.hands[1]
    h.squeeze = 1.0 if t > 0.5 else 0.0
    h.grip_pos = np.array([0.2 + 0.06 * np.cos(2 * t), 1.2 + 0.06 * np.sin(2 * t), -0.3])
    h.trigger = 1.0 if int(t / 3) % 2 else 0.0
    h.buttons = 0 if int(t / 3) % 2 else P.B_PRIMARY
    if kind == "jump" and 3.0 < t < 5.0:
        h.grip_pos = h.grip_pos + np.array([0.3, 0.0, 0.0])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=None, help="server address (default: discover by broadcast)")
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--hz", type=float, default=72.0, help="input rate (the headset's display rate)")
    ap.add_argument("--motion", default="circle", choices=["still", "circle", "jump"])
    ap.add_argument("--tcp-only", action="store_true")
    args = ap.parse_args()
    c = ProbeClient(args.host, args.port, tcp_only=args.tcp_only, input_hz=args.hz)
    t0 = time.perf_counter()
    while not c.loaded and time.perf_counter() - t0 < 10:
        c.poll(0.01)
    print(f"connected to '{c.server_name}' at {c.host}:{c.port}; scene '{c.scene_name}': {len(c.meshes)} meshes, "
          f"{len(c.nodes)} nodes, definition {c.def_bytes / 1e3:.1f} kB, loaded in {(time.perf_counter() - t0) * 1e3:.0f} ms")
    c.rx_bytes = c.tx_bytes = c.frames = c.rx_dgrams = 0
    c.latency_ms, c.hold_ms, c.frame_times = [], [], []
    t1 = time.perf_counter()
    c.run(args.seconds, lambda cl: set_motion(cl, args.motion, time.perf_counter() - t1))
    for k, v in c.summary(time.perf_counter() - t1).items():
        print(f"  {k:24s} {v:10.3f}")
    print(f"  haptic events            L {c.haptics[0]['count']}  R {c.haptics[1]['count']}")
    print(f"  texts                    {c.texts}")
    c.close()


if __name__ == "__main__":
    main()

"""`TeleopServer`: session handshake, scene streaming, controller input, haptics and text cues. Simulator-agnostic.

    srv = TeleopServer(name="my sim")
    srv.start()
    srv.set_scene(scene)                       # full model, sent once per client (and again when replaced)
    while running:
        inp = srv.get_input()                  # newest controller sample (or None)
        ... step the simulator with it, write poses / vertices into `scene` ...
        srv.haptic(1, amplitude=0.6, duration=0.03)
        srv.publish(echo=inp)                  # only what changed since each client's last acknowledged frame

Threads: one accept thread, one reader and one writer thread per client (TCP), one UDP receiver, one beacon thread.
`publish` runs in the caller's thread and never blocks on the network (UDP sendto; TCP goes through the writer).
"""
from __future__ import annotations

import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from . import protocol as P
from .scene import Scene, _rgba8

MAX_RUN = (P.MAX_DATAGRAM - P.STATE_OVERHEAD - P.CHUNK_HDR.size - 4) // 6     # vertices per VERTS chunk


@dataclass
class Client:
    sock: socket.socket
    addr: tuple
    session: int
    name: str = ""
    device: str = ""
    display_hz: float = 0.0
    tcp_only: bool = False
    udp_addr: tuple | None = None
    scene_ready: int = -1               # scene id the client has finished loading
    ack_frame: int = 0
    ack_scene: int = -1
    last_input: P.InputFrame | None = None
    last_input_time: float = 0.0
    out: queue.Queue = field(default_factory=queue.Queue)
    alive: bool = True
    connected_at: float = 0.0
    connected_wall: float = 0.0         # time.time() at connect
    in_count: int = 0                   # input samples received / missing (gaps in their sequence numbers)
    in_missing: int = 0
    acked: int = 0                      # state frames the client acknowledged as complete


class TeleopServer:
    def __init__(self, name: str = "quest-teleop server", port: int = P.DEFAULT_PORT, bind: str = "0.0.0.0",
                 beacon: bool = True, state_hz: float = 60.0, verbose: bool = True):
        self.name, self.port, self.bind, self.beacon, self.state_hz, self.verbose = name, port, bind, beacon, state_hz, verbose
        self._lock = threading.RLock()
        self._clients: dict[int, Client] = {}
        self._next_session = int(time.time() * 1000) & 0x7FFFFFFF or 1
        self._scene: Scene | None = None
        self._scene_id = 0
        self._scene_rev = -1
        self._frame = 0
        self._haptic = [[0, 0, 0, 0], [0, 0, 0, 0]]      # seq, amplitude 0..255, duration_ms, frequency_hz
        self._texts: dict[int, bytes] = {}
        self._panel_msg: bytes | None = None
        self._panel_body: bytes = b""
        self._actions: list[int] = []
        self._running = False
        self._threads: list[threading.Thread] = []
        self.on_connect = None                              # callback(client)
        self.on_disconnect = None
        # statistics (reset by `pop_stats`)
        self._st = dict(tx_bytes=0, tx_dgrams=0, rx_bytes=0, rx_dgrams=0, frames=0, t0=time.perf_counter())
        self._lat: list[int] = []                          # client-reported echo latencies [us] since `pop_stats`
        # monitoring window (read and reset by `monitor`): independent of `pop_stats`
        self._mon = dict(tx_bytes=0, tx_dgrams=0, rx_bytes=0, rx_dgrams=0, frames=0, in_count=0, in_missing=0, acked=0,
                         sent_to_ctrl=0, lat=[], hold=[], step=[], proc=[], t0=time.perf_counter())
        self.started_wall = time.time()

    # ---------------------------------------------------------------------------------------- lifecycle
    def start(self) -> "TeleopServer":
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind((self.bind, self.port))
        self._tcp.listen(4)
        self.port = self._tcp.getsockname()[1]
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        self._udp.bind((self.bind, self.port))
        self._running = True
        for fn in (self._accept_loop, self._udp_loop) + ((self._beacon_loop,) if self.beacon else ()):
            t = threading.Thread(target=fn, daemon=True, name=f"qt-{fn.__name__}")
            t.start()
            self._threads.append(t)
        self._log(f"listening on {self.bind}:{self.port} (TCP + UDP), addresses: {', '.join(local_addresses())}")
        return self

    def stop(self) -> None:
        self._running = False
        with self._lock:
            clients = list(self._clients.values())
        for c in clients:
            self._drop(c, "server stopped", bye=True)
        for s in (self._tcp, self._udp):
            try:
                s.close()
            except OSError:
                pass

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[quest-teleop] {msg}", flush=True)

    # ---------------------------------------------------------------------------------------- public API
    @property
    def n_clients(self) -> int:
        return len(self._clients)

    def clients(self) -> list[Client]:
        with self._lock:
            return list(self._clients.values())

    def controller(self) -> Client | None:
        """The client whose input drives the simulator: of those that are sending input (a headset that is not worn
        sends none), the one connected the longest."""
        now = time.perf_counter()
        with self._lock:
            live = [c for c in self._clients.values() if c.last_input is not None and now - c.last_input_time < 0.5]
            return min(live or self._clients.values(), key=lambda c: c.connected_at, default=None)

    def get_input(self, max_age: float = 0.5) -> P.InputFrame | None:
        """Newest controller sample of the controlling client; None when there is none or it is older than max_age."""
        c = self.controller()
        if c is None or c.last_input is None or time.perf_counter() - c.last_input_time > max_age:
            return None
        return c.last_input

    def set_scene(self, scene: Scene) -> None:
        """Make `scene` the streamed scene. Every client (re)loads it. Call again after adding meshes or nodes."""
        with self._lock:
            self._scene = scene
            self._scene_id = (self._scene_id + 1) & 0xFFFF or 1
            self._scene_rev = scene.revision
            n, t = len(scene.nodes), len(scene.verts)
            # The definition carries the current state, so that is the baseline the deltas start from. Poses are
            # sent once more in frame 1 anyway, and so are deformable vertices (the definition has them quantised
            # to the mesh's own box, not to the scene bounds).
            self._q_pose, self._q_vert, self._q_app = scene.quantized()
            self._pose_changed = np.ones(n, np.int64)
            self._app_changed = np.zeros(n, np.int64)
            self._vert_changed = np.zeros(t, np.int64)
            for node, (o, c) in scene.v_off.items():
                if scene.nodes[node]["kind"] == P.K_DEFORM:
                    self._vert_changed[o:o + c] = 1
            self._frame = 0
            msgs = scene.definition_messages(self._scene_id)
            self._scene_msgs = msgs
            for c in self._clients.values():
                c.scene_ready, c.ack_frame, c.ack_scene = -1, 0, -1
                for m in msgs:
                    c.out.put(m)
        self._log(f"scene '{scene.name}': {len(scene.mesh_msgs)} meshes, {len(scene.nodes)} nodes, "
                  f"{len(scene.verts)} streamed points, definition {sum(map(len, msgs)) / 1e3:.1f} kB")

    def haptic(self, hand: int, amplitude: float, duration: float = 0.02, frequency: float = 0.0) -> None:
        """Vibrate controller `hand` (0 left, 1 right): amplitude 0..1, duration [s], frequency [Hz] (0 = the
        runtime's default; Quest 2 controllers ignore it). Travels in every state frame until replaced, so a lost
        datagram does not lose it."""
        h = self._haptic[hand]
        h[0] = (h[0] + 1) & 0xFFFF
        h[1] = int(np.clip(round(amplitude * 255.0), 0, 255))
        h[2] = int(np.clip(round(duration * 1000.0), 0, 65535))
        h[3] = int(np.clip(round(frequency), 0, 65535))

    def set_text(self, slot: int, text: str, anchor: int = P.TEXT_HUD, pos=(0.0, 0.0, 0.0), size: float = 0.03,
                 color=(1, 1, 1, 1), ttl: float = 0.0) -> None:
        """Show `text` in slot 0..255 (an empty string clears the slot). anchor: TEXT_HUD = head-locked (pos is an
        offset in metres on a panel 1 m in front of the eyes); 1 + parent (protocol.P_*) = at `pos` in that frame,
        facing the viewer. size = line height [m]. ttl [s]: 0 = until replaced."""
        body = struct.pack("<BBBB4Bf3fI", slot, anchor, 0, 0, *_rgba8(color), size, *pos, int(ttl * 1000)) + P.pack_str(text)
        msg = P.frame_msg(P.M_TEXT, body)
        with self._lock:
            if ttl == 0 and self._texts.get(slot + 1000) == body:
                return
            self._texts[slot + 1000] = body
            if text and ttl == 0:
                self._texts[slot] = msg
            else:
                self._texts.pop(slot, None)
            for c in self._clients.values():
                c.out.put(msg)

    def set_panel(self, panel) -> None:
        """Show / update the control panel on every client (`quest_teleop.panel.Panel`). Cheap to call every step:
        nothing is sent when nothing changed."""
        if not panel.host:
            import socket as _s
            addrs = local_addresses()
            panel.host = _s.gethostname() + (f"  {addrs[0]}" if addrs else "")
        body = panel.pack()
        with self._lock:
            if body == self._panel_body:
                return
            self._panel_body = body
            self._panel_msg = P.frame_msg(P.M_PANEL, body)
            for c in self._clients.values():
                c.out.put(self._panel_msg)

    def pop_actions(self) -> list[int]:
        """Ids of the panel actions the operator pressed since the last call."""
        with self._lock:
            out, self._actions = self._actions, []
        return out

    def publish(self, echo: P.InputFrame | None = None) -> int:
        """Send one state frame: for each client, everything that changed since the frame it last acknowledged.
        echo: the input sample this state results from (for the latency measurement). Returns bytes sent."""
        sc = self._scene
        if sc is None:
            return 0
        if sc.revision != self._scene_rev:
            self.set_scene(sc)
        with self._lock:
            self._frame += 1
            frame = self._frame
            pose, vq, app = sc.quantized()
            ch = (pose != self._q_pose).any(1)
            self._pose_changed[ch] = frame
            self._q_pose = pose
            ch = (app != self._q_app).any(1)
            self._app_changed[ch] = frame
            self._q_app = app
            if len(vq):
                # a vertex counts as changed when it moved more than the scene's dead band from the value last sent
                band = int(sc.vertex_deadband / max(float((sc.hi - sc.lo).max()), 1e-9) * 65535.0)
                ch = (np.abs(vq.astype(np.int32) - self._q_vert) > band).any(1)
                self._vert_changed[ch] = frame
                self._q_vert[ch] = vq[ch]
            now = time.perf_counter()
            if echo is not None:
                echo_part = (echo.seq, echo.client_time_us, int(max(0.0, now - echo.recv_time) * 1e6) & 0xFFFFFFFF)
                self._mon["hold"].append(max(0.0, now - echo.recv_time))
            else:
                echo_part = (0, 0, 0)
            hap = b"".join(P.STATE_HAPTIC.pack(h[0], h[1], 0, h[2], h[3]) for h in self._haptic)
            sent = 0
            for c in list(self._clients.values()):
                if c.scene_ready != self._scene_id or not c.alive:
                    continue
                ack = c.ack_frame if c.ack_scene == self._scene_id else 0
                chunks = self._chunks(sc, ack)
                sent += self._send_state(c, frame, chunks, echo_part, hap, now)
            self._st["frames"] += 1
            self._mon["frames"] += 1
            ctrl = self.controller()
            if ctrl is not None and ctrl.scene_ready == self._scene_id:
                self._mon["sent_to_ctrl"] += 1
        return sent

    def note_timing(self, step_s: float, processing_s: float) -> None:
        """Tell the monitor how long the simulator step and the rest of the server's work took for the frame just
        published (for the latency breakdown)."""
        with self._lock:
            self._mon["step"].append(step_s)
            self._mon["proc"].append(processing_s)

    def monitor(self) -> dict:
        """Everything a status display needs, for the window since the last call: who is connected and how, traffic
        rates, loss, and the latency of the controlling client split into its parts.

        Latency (all ms, medians over the window; `total_p95` too):
          total        controller sample -> the resulting state held by the client, measured by the client
          sim_step     the simulator step                                          } reported by the caller through
          processing   the rest of the server's work per frame                     } `note_timing`
          wait         the input waiting for the next simulator step to start (hold - sim_step - processing)
          link         total - hold: both network directions plus the client picking the state up at its next
                       display frame (on average half a frame)
          display      one refresh period of the client's display (scan-out; not measured, 1000 / display_hz)"""
        with self._lock:
            m, now = self._mon, time.perf_counter()
            dt = max(now - m["t0"], 1e-6)
            med = lambda v: float(np.median(v)) * 1e3 if len(v) else None                      # noqa: E731
            ctrl = self.controller()
            clients = []
            for c in self._clients.values():
                live = c.last_input is not None and now - c.last_input_time < 1.0
                clients.append(dict(name=c.name, device=c.device, address=c.addr[0], session=c.session,
                                    link="USB (adb reverse, TCP tunnel)" if c.tcp_only and c.addr[0].startswith("127.")
                                    else ("TCP tunnel" if c.tcp_only else "Wi-Fi / LAN (UDP)"),
                                    display_hz=c.display_hz, connected_s=time.time() - c.connected_wall, sending=live,
                                    controlling=c is ctrl, scene_loaded=c.scene_ready == self._scene_id,
                                    edit_mode=bool(live and c.last_input.edit_mode),
                                    worn=bool(live and c.last_input.flags & P.IF_FOCUSED),
                                    scale=float(c.last_input.place_scale) if c.last_input is not None else None))
            total, hold = med(np.asarray(m["lat"], float) / 1e6), med(m["hold"])
            step, proc = med(m["step"]), med(m["proc"])
            lat = dict(total=total, total_p95=float(np.percentile(m["lat"], 95)) / 1e3 if m["lat"] else None, hold=hold,
                       sim_step=step, processing=proc,
                       wait=max(0.0, hold - (step or 0.0) - (proc or 0.0)) if hold is not None else None,
                       link=max(0.0, total - hold) if total is not None and hold is not None else None,
                       display=1000.0 / ctrl.display_hz if ctrl is not None and ctrl.display_hz > 1 else None)
            got, miss = m["in_count"], m["in_missing"]
            out = dict(time=time.time(), window_s=dt, server=self.name, port=self.port, uptime_s=time.time() - self.started_wall,
                       scene=self._scene.name if self._scene is not None else None, clients=clients,
                       frames_per_s=m["frames"] / dt, down_mbit=m["tx_bytes"] * 8e-6 / dt, up_mbit=m["rx_bytes"] * 8e-6 / dt,
                       down_packets_per_s=m["tx_dgrams"] / dt, up_packets_per_s=m["rx_dgrams"] / dt,
                       headset_fps=got / dt if ctrl is not None else None,
                       up_loss=miss / (got + miss) if got + miss else None,
                       down_loss=max(0.0, 1.0 - m["acked"] / m["sent_to_ctrl"]) if m["sent_to_ctrl"] > 5 else None,
                       latency_ms=lat)
            self._mon = dict(tx_bytes=0, tx_dgrams=0, rx_bytes=0, rx_dgrams=0, frames=0, in_count=0, in_missing=0, acked=0,
                             sent_to_ctrl=0, lat=[], hold=[], step=[], proc=[], t0=now)
        return out

    def pop_stats(self) -> dict:
        """Traffic since the last call: rates in bytes/s and datagrams/s, and the controlling client's own latency
        measurement [ms] (controller sample -> resulting state frame received)."""
        with self._lock:
            st, now = self._st, time.perf_counter()
            dt = max(now - st["t0"], 1e-6)
            c = self.controller()
            lat = np.asarray(self._lat, float) / 1e3 if self._lat else np.array([np.nan])
            self._lat = []
            out = dict(dt=dt, tx_bps=st["tx_bytes"] / dt, rx_bps=st["rx_bytes"] / dt, tx_dgrams=st["tx_dgrams"] / dt,
                       rx_dgrams=st["rx_dgrams"] / dt, frames=st["frames"] / dt,
                       client_latency_ms=float(np.median(lat)), client_latency_p95_ms=float(np.percentile(lat, 95)),
                       client_latency_max_ms=float(lat.max()))
            self._st = dict(tx_bytes=0, tx_dgrams=0, rx_bytes=0, rx_dgrams=0, frames=0, t0=now)
        return out

    # ---------------------------------------------------------------------------------------- state encoding
    def _chunks(self, sc: Scene, ack: int) -> list[bytes]:
        out = []
        for i in np.nonzero(self._pose_changed > ack)[0]:
            p = self._q_pose[i]
            out.append(P.CHUNK_HDR.pack(P.C_POSE, 0, i) + struct.pack("<3H4h", *p))
        for i in np.nonzero(self._app_changed > ack)[0]:
            a = self._q_app[i]
            out.append(P.CHUNK_HDR.pack(P.C_APPEARANCE, 0, i) + struct.pack("<4BHH", *a[:4], a[4], 0))
        if len(self._vert_changed):
            changed = self._vert_changed > ack
            if changed.any():
                for node, (o, c) in sc.v_off.items():
                    m = changed[o:o + c]
                    if not m.any():
                        continue
                    idx = np.nonzero(m)[0]
                    # runs of changed vertices; a gap of one unchanged vertex is cheaper to send than a new chunk
                    brk = np.nonzero(np.diff(idx) > 2)[0]
                    starts, ends = np.r_[idx[0], idx[brk + 1]], np.r_[idx[brk], idx[-1]] + 1
                    for s, e in zip(starts, ends):
                        for a in range(s, e, MAX_RUN):
                            b = min(e, a + MAX_RUN)
                            out.append(P.CHUNK_HDR.pack(P.C_VERTS, 0, node) + struct.pack("<HH", a, b - a)
                                       + self._q_vert[o + a:o + b].tobytes())
        return out

    def _send_state(self, c: Client, frame: int, chunks: list[bytes], echo_part, hap: bytes, now: float) -> int:
        budget = P.MAX_DATAGRAM - P.STATE_OVERHEAD
        groups, cur, size = [], [], 0
        for ch in chunks:
            if size + len(ch) > budget and cur:
                groups.append(cur)
                cur, size = [], 0
            cur.append(ch)
            size += len(ch)
        groups.append(cur)
        if len(groups) > 255:                      # cannot happen with sane scenes; keep the newest part consistent
            groups = groups[:255]
        t_us = int(now * 1e6)
        sent = 0
        for k, g in enumerate(groups):
            d = b"".join([P.DGRAM_HDR.pack(P.MAGIC, P.VERSION, P.D_STATE, c.session),
                          P.STATE_HEAD.pack(self._scene_id, frame, k, len(groups), t_us, *echo_part), hap] + g)
            sent += len(d)
            if c.tcp_only or c.udp_addr is None:
                if c.tcp_only:
                    c.out.put(P.frame_msg(P.M_DGRAM, d))
            else:
                try:
                    self._udp.sendto(d, c.udp_addr)
                except OSError:
                    pass
        self._st["tx_bytes"] += sent
        self._st["tx_dgrams"] += len(groups)
        self._mon["tx_bytes"] += sent
        self._mon["tx_dgrams"] += len(groups)
        return sent

    # ---------------------------------------------------------------------------------------- network threads
    def _beacon_loop(self) -> None:
        offer = P.dgram(P.D_OFFER, 0, struct.pack("<H", self.port) + P.pack_str(self.name))
        targets = ["255.255.255.255"] + broadcast_addresses()
        while self._running:
            for addr in targets:
                try:
                    self._udp.sendto(offer, (addr, P.BEACON_PORT))
                except OSError:
                    pass
            time.sleep(1.0)

    def _udp_loop(self) -> None:
        offer = P.dgram(P.D_OFFER, 0, struct.pack("<H", self.port) + P.pack_str(self.name))
        while self._running:
            try:
                data, addr = self._udp.recvfrom(2048)
            except OSError:
                if not self._running:
                    return
                continue
            if len(data) < P.DGRAM_HDR.size:
                continue
            magic, ver, dtype, session = P.DGRAM_HDR.unpack_from(data, 0)
            if magic != P.MAGIC or ver != P.VERSION:
                continue
            if dtype == P.D_DISCOVER:
                try:
                    self._udp.sendto(offer, addr)
                except OSError:
                    pass
            elif dtype == P.D_INPUT:
                self._on_input(session, data, addr)

    def _on_input(self, session: int, data: bytes, addr) -> None:
        c = self._clients.get(session)
        if c is None or len(data) < P.DGRAM_HDR.size + P.INPUT_HEAD.size + 2 * P.INPUT_HAND.size:
            return
        f = P.unpack_input(memoryview(data)[P.DGRAM_HDR.size:])
        f.recv_time = time.perf_counter()
        with self._lock:
            if addr is not None:
                c.udp_addr = addr
            if c.last_input is not None and ((f.seq - c.last_input.seq) & 0xFFFFFFFF) > 0x7FFFFFFF:
                return                                         # older than what we have (reordered)
            ctrl = self.controller()
            if c.last_input is not None:
                gap = ((f.seq - c.last_input.seq) & 0xFFFFFFFF) - 1
                if 0 < gap < 1000:
                    c.in_missing += gap
                    if c is ctrl:
                        self._mon["in_missing"] += gap
            c.in_count += 1
            c.last_input, c.last_input_time = f, f.recv_time
            if f.ack_scene == self._scene_id:
                if c.ack_scene != f.ack_scene or f.ack_frame > c.ack_frame:
                    c.ack_scene, c.ack_frame = f.ack_scene, f.ack_frame
                    c.acked += 1
                    if c is ctrl or ctrl is None:
                        self._mon["acked"] += 1
            if f.latency_us:
                self._lat.append(f.latency_us)
                if c is ctrl or ctrl is None:
                    self._mon["lat"].append(f.latency_us)
            if c is ctrl or ctrl is None:
                self._mon["in_count"] += 1
            self._st["rx_bytes"] += len(data)
            self._st["rx_dgrams"] += 1
            self._mon["rx_bytes"] += len(data)
            self._mon["rx_dgrams"] += 1

    def _accept_loop(self) -> None:
        while self._running:
            try:
                s, addr = self._tcp.accept()
            except OSError:
                return
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._client_loop, args=(s, addr), daemon=True, name="qt-client").start()

    def _client_loop(self, s: socket.socket, addr) -> None:
        c = None
        try:
            mtype, body = _read_msg(s)
            if mtype != P.M_HELLO:
                s.close()
                return
            magic, proto, flags, hz = struct.unpack_from("<HHHf", body, 0)
            name, off = P.unpack_str(body, 10)
            device, off = P.unpack_str(body, off)
            if magic != P.MAGIC or proto != P.VERSION:
                self._log(f"{addr[0]}: protocol {proto} not supported (this server speaks {P.VERSION})")
                s.sendall(P.frame_msg(P.M_BYE, P.pack_str(f"protocol version {P.VERSION} required")))
                s.close()
                return
            with self._lock:
                self._next_session = (self._next_session + 1) & 0x7FFFFFFF or 1
                c = Client(sock=s, addr=addr, session=self._next_session, name=name, device=device, display_hz=hz,
                           tcp_only=bool(flags & P.HF_TCP_ONLY), connected_at=time.perf_counter(),
                           connected_wall=time.time())
                c.out.put(P.frame_msg(P.M_WELCOME, struct.pack("<HHIHf", P.VERSION, 0, c.session, self.port,
                                                               self.state_hz) + P.pack_str(self.name)))
                if self._scene is not None:
                    for m in self._scene_msgs:
                        c.out.put(m)
                for slot, m in self._texts.items():
                    if slot < 1000:
                        c.out.put(m)
                if self._panel_msg is not None:
                    c.out.put(self._panel_msg)
                self._clients[c.session] = c
            threading.Thread(target=self._writer_loop, args=(c,), daemon=True, name="qt-writer").start()
            self._log(f"client connected: {name} ({device}, {hz:.0f} Hz) from {addr[0]}"
                      f"{' [TCP only]' if c.tcp_only else ''}, session {c.session}")
            if self.on_connect:
                self.on_connect(c)
            while self._running and c.alive:
                mtype, body = _read_msg(s)
                if mtype == P.M_SCENE_READY:
                    (sid,) = struct.unpack_from("<H", body, 0)
                    with self._lock:
                        c.scene_ready, c.ack_frame, c.ack_scene = sid, 0, -1
                elif mtype == P.M_DGRAM:
                    if len(body) >= P.DGRAM_HDR.size and body[3] == P.D_INPUT:
                        self._on_input(c.session, bytes(body), None)
                elif mtype == P.M_ACTION:
                    (aid,) = struct.unpack_from("<H", body, 0)
                    with self._lock:
                        self._actions.append(aid)
                elif mtype == P.M_PING:
                    c.out.put(P.frame_msg(P.M_PONG, bytes(body[:8]) + struct.pack("<Q", int(time.perf_counter() * 1e6))))
                elif mtype == P.M_BYE:
                    break
        except (OSError, ConnectionError, struct.error):
            pass
        finally:
            if c is not None:
                self._drop(c, "disconnected")
            else:
                try:
                    s.close()
                except OSError:
                    pass

    def _writer_loop(self, c: Client) -> None:
        try:
            while c.alive:
                m = c.out.get()
                if m is None:
                    break
                c.sock.sendall(m)
        except OSError:
            pass
        finally:
            self._drop(c, "send failed")

    def _drop(self, c: Client, why: str, bye: bool = False) -> None:
        with self._lock:
            if not c.alive:
                return
            c.alive = False
            self._clients.pop(c.session, None)
        if bye:
            try:
                c.sock.sendall(P.frame_msg(P.M_BYE, P.pack_str(why)))
            except OSError:
                pass
        c.out.put(None)
        try:
            c.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        c.sock.close()
        self._log(f"client {c.name} ({c.addr[0]}) {why}")
        if self.on_disconnect:
            self.on_disconnect(c)


def _read_exact(s: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        part = s.recv(n - len(buf))
        if not part:
            raise ConnectionError("closed")
        buf += part
    return bytes(buf)


def _read_msg(s: socket.socket) -> tuple[int, memoryview]:
    n, mtype = struct.unpack("<IH", _read_exact(s, 6))
    if n < 2 or n > (1 << 28):
        raise ConnectionError("bad frame")
    return mtype, memoryview(_read_exact(s, n - 2))


def local_addresses() -> list[str]:
    """IPv4 addresses of this machine (best effort, no dependencies)."""
    out = []
    try:
        import subprocess
        txt = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=2).stdout
        for line in txt.splitlines():
            parts = line.split()
            if "inet" in parts:
                a = parts[parts.index("inet") + 1].split("/")[0]
                if not a.startswith("127."):
                    out.append(a)
    except Exception:
        pass
    if not out:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            out.append(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    return out


def broadcast_addresses() -> list[str]:
    out = []
    try:
        import subprocess
        txt = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=2).stdout
        for line in txt.splitlines():
            parts = line.split()
            if "brd" in parts:
                out.append(parts[parts.index("brd") + 1])
    except Exception:
        pass
    return out

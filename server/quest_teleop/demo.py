"""A self-contained example (numpy only): two floating grippers over a table with a cube to pick and place, a rope
and a cloth. It streams the scene, takes the controllers, records takes and serves the status page.

    python -m quest_teleop.demo                 # then start the headset app or the desktop client

Right controller -> gripper 1, left controller -> gripper 0. Hold the grip button to move a gripper (clutch), the
trigger closes it slowly, A / X opens it slowly, B starts / stops recording a take (saved in ~/teleop_recordings
or --data-dir). The status page (http://localhost:8800) lists the takes; each opens in the trajectory view. Each gripper has a limited reach (a sphere around its base post) and
cannot enter the table: pull the hand beyond that and the go-back cue appears.

It exercises every protocol feature: rigid nodes, a streamed deformable mesh, a streamed tube, appearance changes,
haptics, text cues, the go-back behaviour, and the latency echo. `MockWorld` is also what the tests run against.
"""
from __future__ import annotations

import argparse
import time

import numpy as np

from . import protocol as P
from .cues import GoBackCue
from .panel import Action, Panel, Row
from .recording import STATE_CODE, TakeRecorder
from .scene import Scene, box_mesh, cylinder_mesh, sphere_mesh
from .server import TeleopServer
from .teleop import ContactHaptics, RateGripper, TeleopCfg, ToolTeleop

DOWN = np.array([1.0, 0.0, 0.0, 0.0])              # tool z pointing down (180 deg about x)


def _colored(mesh, rgb):
    v, f, n = mesh
    return v, f, n, np.tile(np.r_[np.asarray(rgb, float), 1.0], (len(v), 1))


def _merge(parts):
    vs, fs, ns, cs, off = [], [], [], [], 0
    for v, f, n, c in parts:
        vs.append(v), fs.append(f + off), ns.append(n), cs.append(c)
        off += len(v)
    return np.concatenate(vs), np.concatenate(fs), np.concatenate(ns), np.concatenate(cs)


def _shift(mesh, d):
    v, f, n = mesh
    return v + np.asarray(d, np.float32), f, n


class MockWorld:
    """Kinematic toy world. All state lives in numpy arrays; `step` advances it by dt."""
    N_ROPE, CLOTH, BOX = 40, 14, 0.02                        # BOX: half size of the cube [m]
    REACH, MAX_OPEN, FINGER = 0.55, 0.08, 0.05

    def __init__(self):
        self.bases = np.array([[0.0, 0.35, 0.35], [0.0, -0.35, 0.35]])
        self.tool_pos = np.array([[0.35, 0.18, 0.22], [0.35, -0.18, 0.22]])
        self.tool_quat = np.tile(DOWN, (2, 1))
        self.closure = np.zeros(2)
        self.force = np.zeros(2)                    # contact force on each tool [N] (for haptics)
        n = self.N_ROPE
        self.rope = np.stack([np.linspace(0.25, 0.60, n), np.full(n, -0.05), np.full(n, 0.006)], 1)
        self.rope_prev = self.rope.copy()
        self.seg = float(np.linalg.norm(self.rope[1] - self.rope[0]))
        self.grabbed = [None, None]                 # rope node held by each tool
        c = self.CLOTH
        gx, gy = np.meshgrid(np.linspace(0.30, 0.50, c), np.linspace(0.10, 0.30, c), indexing="ij")
        self.cloth = np.stack([gx, gy, np.full_like(gx, 0.12)], -1).reshape(-1, 3)
        self.cloth_prev = self.cloth.copy()
        self.cloth_pin = np.zeros(c * c, bool)
        self.cloth_pin[:c] = True                   # the x = min edge is clamped
        self.cloth_rest = self.cloth.copy()
        idx = np.arange(c * c).reshape(c, c)
        self.cloth_faces = np.concatenate([np.stack([idx[:-1, :-1], idx[1:, :-1], idx[:-1, 1:]], -1).reshape(-1, 3),
                                           np.stack([idx[1:, :-1], idx[1:, 1:], idx[:-1, 1:]], -1).reshape(-1, 3)])
        e = np.concatenate([np.stack([idx[:-1], idx[1:]], -1).reshape(-1, 2), np.stack([idx[:, :-1], idx[:, 1:]], -1).reshape(-1, 2),
                            np.stack([idx[:-1, :-1], idx[1:, 1:]], -1).reshape(-1, 2)])
        self.cloth_edges, self.cloth_len = e, np.linalg.norm(self.cloth[e[:, 0]] - self.cloth[e[:, 1]], axis=1)
        self.box = np.array([0.42, -0.22, self.BOX])          # a cube to pick up and put down (centre)
        self.box_held = None                                  # index of the tool carrying it
        self.t = 0.0

    def pinch(self, i):
        return self.tool_pos[i] + P.quat_rotate(self.tool_quat[i], np.array([0.0, 0.0, self.FINGER]))

    def step(self, dt, cmd_pos, cmd_quat, closure):
        """cmd_pos (2, 3), cmd_quat (2, 4): tool pose commands; closure (2,) 0..1."""
        self.t += dt
        a = 1.0 - np.exp(-dt / 0.04)                               # the tools follow with a 40 ms lag
        for i in range(2):
            want = self.tool_pos[i] + a * (np.asarray(cmd_pos[i]) - self.tool_pos[i])
            d = want - self.bases[i]                               # reach limit
            n = np.linalg.norm(d)
            if n > self.REACH:
                want = self.bases[i] + d * (self.REACH / n)
            floor = 0.012 + self.FINGER * max(0.0, float(P.quat_rotate(cmd_quat[i], [0, 0, 1.0])[2] * -1.0))
            depth = floor - np.asarray(cmd_pos[i])[2]
            self.force[i] = max(0.0, depth) * 400.0                # pressing into the table
            want[2] = max(want[2], floor)
            self.tool_pos[i] = want
            rv = P.quat_to_rotvec(P.quat_mul(cmd_quat[i], P.quat_conj(self.tool_quat[i])))
            self.tool_quat[i] = P.quat_mul(P.quat_from_rotvec(a * rv), self.tool_quat[i])
            self.tool_quat[i] /= np.linalg.norm(self.tool_quat[i])
            self.closure[i] = closure[i]
        # rope: verlet + distance constraints + table, grabbed by a closed gripper
        g = np.array([0.0, 0.0, -9.81])
        x, xp = self.rope, self.rope_prev
        new = x + 0.97 * (x - xp) + g * dt * dt
        self.rope_prev, self.rope = x, new
        for i in range(2):
            p = self.pinch(i)
            if self.closure[i] > 0.75:
                if self.grabbed[i] is None:
                    d = np.linalg.norm(self.rope - p, axis=1)
                    k = int(np.argmin(d))
                    if d[k] < 0.02:
                        self.grabbed[i] = k
                        self.force[i] += 3.0
            else:
                self.grabbed[i] = None
        for _ in range(12):
            for i in range(2):
                if self.grabbed[i] is not None:
                    self.rope[self.grabbed[i]] = self.pinch(i)
            d = self.rope[1:] - self.rope[:-1]
            n = np.linalg.norm(d, axis=1, keepdims=True)
            corr = 0.5 * d * (1.0 - self.seg / np.maximum(n, 1e-9))
            self.rope[:-1] += corr
            self.rope[1:] -= corr
            self.rope[:, 2] = np.maximum(self.rope[:, 2], 0.006)
        for i in range(2):
            if self.grabbed[i] is not None:
                self.rope[self.grabbed[i]] = self.pinch(i)
                self.force[i] += 1.0
        # the cube: carried by a gripper that closed around it, otherwise it drops onto the table
        if self.box_held is not None and self.closure[self.box_held] < 0.45:
            self.box_held = None
        if self.box_held is None:
            for i in range(2):
                if 0.5 < self.closure[i] and np.linalg.norm(self.pinch(i) - self.box) < 0.03:
                    self.box_held = i
                    break
        if self.box_held is not None:
            self.box = self.pinch(self.box_held).copy()
            self.force[self.box_held] += 2.0
        else:
            self.box[2] = max(self.BOX, self.box[2] - 0.6 * dt)
        # cloth: verlet + edge constraints, pushed by the tools
        x, xp = self.cloth, self.cloth_prev
        new = x + 0.96 * (x - xp) + g * dt * dt
        self.cloth_prev, self.cloth = x, new
        e, L = self.cloth_edges, self.cloth_len
        for _ in range(8):
            d = self.cloth[e[:, 1]] - self.cloth[e[:, 0]]
            n = np.linalg.norm(d, axis=1, keepdims=True)
            corr = 0.5 * d * (1.0 - L[:, None] / np.maximum(n, 1e-9))
            np.add.at(self.cloth, e[:, 0], 0.5 * corr)
            np.add.at(self.cloth, e[:, 1], -0.5 * corr)
            for i in range(2):
                p = self.pinch(i)
                d = self.cloth - p
                n = np.linalg.norm(d, axis=1, keepdims=True)
                hit = n[:, 0] < 0.03
                if hit.any():
                    self.cloth[hit] = p + d[hit] / np.maximum(n[hit], 1e-9) * 0.03
                    self.force[i] += 0.6 * float(hit.sum()) / 8.0
            self.cloth[:, 2] = np.maximum(self.cloth[:, 2], 0.003)
            self.cloth[self.cloth_pin] = self.cloth_rest[self.cloth_pin]


class DemoSession:
    """MockWorld + scene + teleop logic. `tick(dt, inp)` does one step; the caller publishes."""

    def __init__(self, server: TeleopServer | None = None, mode: str = "clutch", data_dir: str | None = None,
                 session: str | None = None, host_label: str = ""):
        self.server, self.world = server, MockWorld()
        self.recorder = TakeRecorder(data_dir, session, tools=("left", "right"), max_opening=MockWorld.MAX_OPEN)
        self.host_label = host_label
        w = self.world
        sc = self.scene = Scene("quest-teleop demo", bounds_min=(-0.8, -1.0, -0.1), bounds_max=(1.0, 1.0, 1.2), up_axis=2,
                                focus_center=(0.38, 0.0, 0.12), focus_radius=0.32, view_dir=(1.0, 0.0, 0.0))
        table = _merge([_colored(_shift(box_mesh((0.45, 0.6, 0.02)), (0.35, 0.0, -0.02)), (0.55, 0.45, 0.35)),
                        _colored(_shift(box_mesh((0.02, 0.02, 0.2)), (0.0, 0.35, 0.15)), (0.3, 0.3, 0.32)),
                        _colored(_shift(box_mesh((0.02, 0.02, 0.2)), (0.0, -0.35, 0.15)), (0.3, 0.3, 0.32))])
        sc.add_node("table", sc.add_mesh(table[0], table[1], table[2], table[3]))
        palm = sc.add_mesh(*_merge([_colored(box_mesh((0.05, 0.02, 0.012)), (0.25, 0.27, 0.3)),
                                    _colored(_shift(cylinder_mesh(0.018, 0.03), (0, 0, -0.04)), (0.8, 0.8, 0.82))]))
        finger = sc.add_mesh(*_colored(_shift(box_mesh((0.004, 0.012, 0.5 * w.FINGER)), (0, 0, 0.5 * w.FINGER)), (0.15, 0.15, 0.17)))
        self.palm, self.fingers = [], []
        for i, col in enumerate(((0.55, 0.75, 1.0, 1.0), (1.0, 0.75, 0.55, 1.0))):
            self.palm.append(sc.add_node(f"tool{i}/palm", palm, color=col))
            self.fingers.append([sc.add_node(f"tool{i}/finger{k}", finger, color=col) for k in range(2)])
        self.rope = sc.add_tube("rope", w.rope, radius=0.004, color=(0.95, 0.95, 0.9, 1.0))
        cc = np.zeros((len(w.cloth), 4))
        c = w.CLOTH
        chk = (np.add.outer(np.arange(c), np.arange(c)) % 2).reshape(-1)
        cc[:] = np.where(chk[:, None] > 0, [0.75, 0.15, 0.18, 1.0], [0.9, 0.85, 0.8, 1.0])
        self.cloth = sc.add_deform("cloth", w.cloth, w.cloth_faces, colors=cc)
        self.box = sc.add_node("cube", sc.add_mesh(*_colored(box_mesh((w.BOX, w.BOX, w.BOX)), (0.25, 0.55, 0.45))), pos=w.box)
        v, f, n = sphere_mesh(1.0, 8, 12)
        ball = sc.add_mesh(v, f, n)
        self.reach = [sc.add_node(f"tool{i}/base", ball, pos=w.bases[i], scale=0.025, color=(0.3, 0.3, 0.32, 1)) for i in range(2)]
        cfg = TeleopCfg(mode=mode)
        self.teleop = [ToolTeleop(cfg), ToolTeleop(cfg)]
        self.grip = [RateGripper(), RateGripper()]
        self.haptics = [ContactHaptics(), ContactHaptics()]
        self.cues = [GoBackCue(sc, f"tool{i}", text_slot=i) for i in range(2)]
        self.cmd_pos, self.cmd_quat = w.tool_pos.copy(), w.tool_quat.copy()
        self.states = ["idle", "idle"]
        self.ticks, self.dt_nominal = 0, 1.0 / 60.0
        self._prev_b = False
        self._sync()

    # panel actions: 1 = record / stop, 2 = reset, 3 = delete the last take
    def _panel(self) -> Panel:
        rec = self.recorder
        p = Panel(title="Pick and place demo", subtitle="two grippers, a cube, a rope and a cloth", host=self.host_label)
        if rec.recording:
            e = int(rec.elapsed)
            p.status("recording", f"REC {e // 60:02d}:{e % 60:02d}  take {rec.take}")
        else:
            p.status("ok", "Ready to record")
        p.rows = [Row("RUN", rec.session, wrist=True), Row("TAKES SAVED", str(rec.n_saved), wrist=True),
                  Row("THIS TAKE", f"{rec.steps} steps" if rec.recording else "-"),
                  Row("LEFT TOOL", self.states[0], style="failure" if self.states[0] == "lost" else "normal"),
                  Row("RIGHT TOOL", self.states[1], style="failure" if self.states[1] == "lost" else "normal"),
                  Row("SAVED TO", rec.dir)]
        p.actions = [Action(1, "Stop recording" if rec.recording else "Start recording", hint="$Rs", style="primary",
                            active=rec.recording),
                     Action(2, "Reset the scene", hint="HOLD", style="danger", confirm=True),
                     Action(3, "Delete the last take", hint="HOLD", style="danger", confirm=True,
                            enabled=not rec.recording and rec.n_saved > 0)]
        p.legend = [("$R / $L GRIP (HOLD)", "move that gripper"), ("TRIGGER (HOLD)", "close slowly"),
                    ("$Rp / $Lp (HOLD)", "open slowly"), ("$Rs", "start / stop recording")]
        return p

    def status(self) -> dict:
        """The application part of the status page (see quest_teleop.status)."""
        now = time.perf_counter()
        n, t = getattr(self, "_st_mark", (0, now - 1.0))
        self._st_mark = (self.ticks, now)
        rate = (self.ticks - n) / max(now - t, 1e-6)
        rec = self.recorder
        inp = self.server.get_input() if self.server is not None else None
        if rec.recording:
            state, text = "recording", f"REC take {rec.take}, {rec.steps} steps"
        elif inp is None:
            state, text = "idle", "Session waiting for the headset"
        elif inp.edit_mode:
            state, text = "paused", "Session paused (headset menu open)"
        else:
            state, text = "running", "Session running"
        return dict(session=dict(name=rec.session, state=state, state_text=text,
                                 rows=[["Run", rec.session], ["State", text], ["Scene", self.scene.name],
                                       ["Current take", f"take {rec.take}: {rec.steps} steps" if rec.recording else f"none (next: take {rec.take})"],
                                       ["Takes this session", str(rec.n_saved)], ["Left tool", self.states[0]],
                                       ["Right tool", self.states[1]], ["Recordings folder", rec.root]]),
                    sim=dict(rtf=rate * self.dt_nominal, steps_per_s=rate, contact_n=float(self.world.force.max())))

    def _toggle_recording(self):
        srv, rec = self.server, self.recorder
        if not rec.recording:
            if rec.n_saved == 0:
                rec.write_session(dict(application="quest_teleop.demo", scene=self.scene.name, up_axis=self.scene.up_axis))
            rec.start()
            for h in (0, 1):
                srv.haptic(h, 0.8, 0.05)
            srv.set_text(20, f"Recording take {rec.take}", ttl=1.5, color=(0.94, 0.44, 0.23, 1))
        else:
            entry = rec.stop(dict(valid=True, flags=[]))
            for h in (0, 1):
                srv.haptic(h, 0.5, 0.12)
            srv.set_text(20, f"Take {entry['take']} saved: {entry['steps']} steps" if entry else "Nothing recorded", ttl=2.5)

    def _reset(self):
        if self.recorder.recording:
            self._toggle_recording()
        self.world = MockWorld()
        for t in self.teleop:
            t.reset()
        for h in (0, 1):
            self.server.haptic(h, 1.0, 0.2)
        self.server.set_text(20, "Scene reset", ttl=2.0)

    def _sync(self):
        w, sc = self.world, self.scene
        for i in range(2):
            sc.set_pose(self.palm[i], w.tool_pos[i], w.tool_quat[i])
            half = 0.5 * w.MAX_OPEN * (1.0 - w.closure[i]) + 0.004
            for k, s in enumerate((1.0, -1.0)):
                sc.set_pose(self.fingers[i][k], w.tool_pos[i] + P.quat_rotate(w.tool_quat[i], [s * half, 0, 0.012]), w.tool_quat[i])
        sc.set_points(self.rope, w.rope)
        sc.set_points(self.cloth, w.cloth)
        sc.set_pose(self.box, w.box)

    def tick(self, dt: float, inp: P.InputFrame | None):
        self.ticks += 1
        self.dt_nominal = dt
        if self.server is not None:
            acts = self.server.pop_actions()
            b = bool(inp is not None and not inp.edit_mode and inp.hands[1].button(P.B_SECONDARY))
            if 1 in acts or (b and not self._prev_b):
                self._toggle_recording()
            if 2 in acts:
                self._reset()
            if 3 in acts and not self.recorder.recording:
                gone = self.recorder.delete_last()
                self.server.set_text(20, f"Take {gone['take']} deleted" if gone else "No take to delete", ttl=2.0)
            self._prev_b = b
            self.server.set_panel(self._panel())
        w = self.world
        closure = w.closure.copy()
        for i in range(2):
            hand, clutch, trig, opn = None, False, 0.0, False
            if inp is not None and not inp.edit_mode and inp.hands[i].active:
                h = inp.hands[i]
                hand, clutch, trig, opn = inp.hand_in_scene(i), h.squeeze > 0.5, h.trigger, h.button(P.B_PRIMARY)
            out = self.teleop[i].update(dt, hand, clutch, w.tool_pos[i], w.tool_quat[i])
            self.cmd_pos[i], self.cmd_quat[i] = out.cmd_pos, out.cmd_quat
            closure[i] = self.grip[i].update(dt, trig, opn)
            self.cues[i].update(dt, out, w.tool_pos[i], w.tool_quat[i], self.server)
            self.states[i] = out.state
            if self.server is not None:
                if out.engaged_edge:
                    self.server.haptic(i, 0.5, 0.02)
                elif out.lost_edge:
                    self.server.haptic(i, 1.0, 0.12)
        w.step(dt, self.cmd_pos, self.cmd_quat, closure)
        if self.recorder.recording:                               # one row per step: tools, grippers, controllers
            hands = [inp.hand_in_scene(i) if inp is not None and inp.hands[i].active else (np.full(3, np.nan), np.full(4, np.nan))
                     for i in range(2)]
            self.recorder.add(w.t, w.tool_pos, w.tool_quat, opening=w.MAX_OPEN * (1.0 - w.closure),
                              opening_cmd=w.MAX_OPEN * (1.0 - closure), target_pos=self.cmd_pos, target_quat=self.cmd_quat,
                              tool_state=[STATE_CODE[st] for st in self.states], ctrl_pos=[h[0] for h in hands],
                              ctrl_quat=[h[1] for h in hands],
                              trigger=[inp.hands[i].trigger if inp else 0.0 for i in range(2)],
                              squeeze=[inp.hands[i].squeeze if inp else 0.0 for i in range(2)],
                              buttons=[inp.hands[i].buttons if inp else 0 for i in range(2)],
                              extra={"cube_pos": w.box, "rope": w.rope})
        if self.server is not None:
            for i in range(2):
                hp = self.haptics[i].update(dt, w.force[i])
                if hp and self.states[i] != "lost":
                    self.server.haptic(i, *hp)
        self._sync()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    ap.add_argument("--hz", type=float, default=60.0, help="simulation and state rate")
    ap.add_argument("--mode", default="clutch", choices=["clutch", "absolute"])
    ap.add_argument("--seconds", type=float, default=0.0, help="stop after this long (0 = run until Ctrl-C)")
    ap.add_argument("--status-port", type=int, default=8800, help="local status web page (0 = none)")
    ap.add_argument("--data-dir", default=None, help="where takes are recorded (default: $TELEOP_DATA_DIR, else ~/teleop_recordings)")
    ap.add_argument("--session", default=None, help="name of this run (default: date and time)")
    ap.add_argument("--host-label", default="", help="what the headset shows as the computer (default: host name and address)")
    args = ap.parse_args()
    srv = TeleopServer(name="quest-teleop demo", port=args.port, state_hz=args.hz).start()
    ses = DemoSession(srv, args.mode, args.data_dir, args.session, args.host_label)
    srv.set_scene(ses.scene)
    page = None
    if args.status_port:
        from .status import StatusPage
        page = StatusPage(srv, port=args.status_port, extra=ses.status, title="Quest Teleop demo",
                          recordings_dir=ses.recorder.root).start()
        print(f"[demo] status page: {page.url}    takes are saved in {ses.recorder.root}", flush=True)
    dt = 1.0 / args.hz
    nxt, t_end, t_stat = time.perf_counter(), time.perf_counter() + args.seconds, time.perf_counter()
    try:
        while args.seconds <= 0 or time.perf_counter() < t_end:
            inp = srv.get_input()
            t0 = time.perf_counter()
            ses.tick(dt, inp)
            step = time.perf_counter() - t0
            srv.publish(echo=inp)
            srv.note_timing(step, time.perf_counter() - t0 - step)
            if time.perf_counter() - t_stat > 5.0 and srv.n_clients:
                st = srv.pop_stats()
                print(f"[demo] {st['frames']:.0f} fps | down {st['tx_bps'] * 8e-6:.2f} Mbit/s ({st['tx_dgrams']:.0f} dgram/s) | "
                      f"up {st['rx_bps'] * 8e-3:.0f} kbit/s | client latency median {st['client_latency_ms']:.1f} p95 "
                      f"{st['client_latency_p95_ms']:.1f} max {st['client_latency_max_ms']:.1f} ms | "
                      f"tools {ses.states}", flush=True)
                t_stat = time.perf_counter()
            nxt += dt
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.perf_counter()
    except KeyboardInterrupt:
        pass
    if page is not None:
        page.stop()
    if ses.recorder.recording:
        ses.recorder.stop(dict(valid=True, flags=[]))
    ses.recorder.wait()
    srv.stop()


if __name__ == "__main__":
    main()

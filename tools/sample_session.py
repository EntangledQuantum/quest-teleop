"""Records a scripted pick-and-place take with the demo (no headset needed) and keeps the server and the status page
up, so the pages can be looked at and screenshots made.

    PYTHONPATH=server python tools/sample_session.py --data-dir /tmp/teleop_sample --serve 120
    # then: http://localhost:8800  (status)   and its Recordings table -> the trajectory view

The script plays both controllers: the right gripper picks the cube up, carries it and puts it down; the left one
closes on the rope and lets go.
"""
import argparse
import time

import numpy as np

from quest_teleop import TeleopServer
from quest_teleop import protocol as P
from quest_teleop.demo import DemoSession
from quest_teleop.status import StatusPage

# (time [s], right tool target xyz, right trigger, right open button, left tool target xyz, left trigger, left open)
KEYS = [
    (0.0, (0.35, -0.18, 0.22), 0, 0, (0.35, 0.18, 0.22), 0, 0),
    (1.5, (0.42, -0.22, 0.14), 0, 0, (0.40, 0.02, 0.16), 0, 0),
    (2.6, (0.42, -0.22, 0.065), 0, 0, (0.42, -0.05, 0.066), 0, 0),
    (4.6, (0.42, -0.22, 0.065), 1, 0, (0.42, -0.05, 0.066), 1, 0),      # close: 2 s for the full stroke
    (6.0, (0.42, -0.22, 0.20), 0, 0, (0.42, -0.02, 0.15), 0, 0),        # lift
    (8.0, (0.28, -0.02, 0.20), 0, 0, (0.46, 0.06, 0.15), 0, 0),         # carry
    (9.2, (0.28, -0.02, 0.066), 0, 0, (0.46, 0.06, 0.10), 0, 0),        # lower
    (10.8, (0.28, -0.02, 0.066), 0, 1, (0.46, 0.06, 0.10), 0, 1),       # open
    (12.0, (0.30, -0.10, 0.20), 0, 0, (0.38, 0.16, 0.20), 0, 0),
]


def frame(t, seq):
    f = P.InputFrame(seq=seq, client_time_us=int(time.perf_counter() * 1e6), flags=P.IF_FOCUSED)
    f.place_pos, f.place_quat, f.place_scale = np.zeros(3), np.array([-np.sqrt(0.5), 0, 0, np.sqrt(0.5)]), 1.0
    f.recv_time = time.perf_counter()
    k = max(i for i, key in enumerate(KEYS) if key[0] <= t) if t < KEYS[-1][0] else len(KEYS) - 2
    a, b = KEYS[k], KEYS[min(k + 1, len(KEYS) - 1)]
    u = float(np.clip((t - a[0]) / max(b[0] - a[0], 1e-9), 0.0, 1.0))
    u = u * u * (3 - 2 * u)
    for hand, (pi, ti, oi) in enumerate(((4, 5, 6), (1, 2, 3))):           # left, right
        h = f.hands[hand]
        h.flags = P.HAND_ACTIVE | P.HAND_GRIP_VALID | P.HAND_AIM_VALID | P.HAND_TRACKED
        p = (1 - u) * np.asarray(a[pi]) + u * np.asarray(b[pi])            # scene (z up) -> stage (y up)
        h.grip_pos = np.array([p[0], p[2], -p[1]])
        h.squeeze = 1.0
        h.trigger = float(b[ti]) if t > a[0] else 0.0
        h.buttons = P.B_PRIMARY if b[oi] and t > a[0] else 0
    return f


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--session", default="sample")
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    ap.add_argument("--status-port", type=int, default=8800)
    ap.add_argument("--serve", type=float, default=0.0, help="keep the server and the page up this long after recording [s]")
    ap.add_argument("--host-label", default="workstation  192.168.1.20")
    args = ap.parse_args()
    srv = TeleopServer(name="Quest Teleop demo", port=args.port, beacon=False).start()
    ses = DemoSession(srv, data_dir=args.data_dir, session=args.session, host_label=args.host_label)
    srv.set_scene(ses.scene)
    page = StatusPage(srv, port=args.status_port, extra=ses.status, title="Quest Teleop demo", recorder=ses.recorder).start()
    dt = 1.0 / 60.0
    for i in range(30):                                   # the grippers engage at the hands' start pose
        ses.tick(dt, frame(0.0, i + 1))
        srv.publish()
    ses._toggle_recording()
    n = int(KEYS[-1][0] / dt)
    for i in range(n):
        ses.tick(dt, frame(i * dt, 100 + i))
        srv.publish()
    ses._toggle_recording()
    ses.recorder.wait()
    print(f"recorded {ses.recorder.last['file']}: {ses.recorder.last['steps']} steps; status page {page.url}", flush=True)
    t_end = time.perf_counter() + args.serve
    nxt = time.perf_counter()
    while time.perf_counter() < t_end:                    # keep simulating for clients that connect
        inp = srv.get_input()
        t0 = time.perf_counter()
        ses.tick(dt, inp)
        step = time.perf_counter() - t0
        srv.publish(echo=inp)
        srv.note_timing(step, time.perf_counter() - t0 - step)
        nxt += dt
        time.sleep(max(0.0, nxt - time.perf_counter()))
    page.stop()
    srv.stop()


if __name__ == "__main__":
    main()

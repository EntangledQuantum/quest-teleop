"""Server <-> probe client over loopback: scene definition, state decoding, deltas, haptics, go-back cue, latency."""
import tempfile
import threading
import time
import urllib.error

import numpy as np
import pytest

from quest_teleop import protocol as P
from quest_teleop.demo import DemoSession
from quest_teleop.probe import ProbeClient, set_motion
from quest_teleop.server import TeleopServer


def test_quantisation_roundtrip():
    rng = np.random.default_rng(0)
    q = rng.normal(size=(1000, 4))
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    d = P.dequantize_quat(P.quantize_quat(q))
    ang = 2 * np.arccos(np.clip(np.abs((q * d).sum(1)), 0, 1))
    assert ang.max() < 1e-4
    lo, hi = np.array([-1.0, -1.0, 0.0]), np.array([1.0, 1.0, 2.0])
    p = rng.uniform(lo, hi, (1000, 3))
    assert np.abs(P.dequantize_pos(P.quantize_pos(p, lo, hi), lo, hi) - p).max() < 2.0 / 65535


def test_input_roundtrip():
    f = P.InputFrame(seq=7, client_time_us=123456789012, ack_frame=5, ack_scene=2, flags=3, latency_us=999,
                     place_scale=1.5)
    f.hands[1].trigger, f.hands[1].buttons = 0.25, P.B_PRIMARY | P.B_STICK
    f.hands[1].grip_pos = np.array([1.0, 2.0, 3.0])
    d = P.pack_input(42, f)
    assert len(d) == 292
    g = P.unpack_input(memoryview(d)[8:])
    assert (g.seq, g.client_time_us, g.ack_frame, g.flags) == (7, 123456789012, 5, 3)
    assert g.hands[1].button(P.B_STICK) and abs(g.hands[1].trigger - 0.25) < 1e-6
    assert np.allclose(g.hands[1].grip_pos, [1, 2, 3]) and abs(g.place_scale - 1.5) < 1e-6


def _run(tcp_only: bool, seconds: float = 6.0, motion: str = "jump", hz: float = 60.0, drop: float = 0.0):
    srv = TeleopServer(port=0, beacon=False, verbose=False).start()
    ses = DemoSession(srv, data_dir=tempfile.mkdtemp())
    srv.set_scene(ses.scene)
    stop = threading.Event()
    states = []

    def sim():
        dt, nxt = 1.0 / hz, time.perf_counter()
        while not stop.is_set():
            inp = srv.get_input()
            ses.tick(dt, inp)
            srv.publish(echo=inp)
            states.append(ses.states[1])
            nxt += dt
            time.sleep(max(0.0, nxt - time.perf_counter()))

    th = threading.Thread(target=sim, daemon=True)
    th.start()
    c = ProbeClient("127.0.0.1", srv.port, tcp_only=tcp_only, drop=drop)
    t0 = time.perf_counter()
    while not c.loaded:
        c.poll(0.01)
        assert time.perf_counter() - t0 < 5
    t1 = time.perf_counter()
    seen_text = []

    def tick(cl):
        set_motion(cl, motion, time.perf_counter() - t1)
        if cl.texts:
            seen_text.append(dict(cl.texts))

    c.run(seconds, tick)
    summary = c.summary(time.perf_counter() - t1)
    stop.set()
    th.join()
    for _ in range(10):
        c.poll(0.01)
    if drop > 0:                      # the simulation stands still: keep publishing until the losses are repaired
        c.drop = min(drop, 0.5)
        for _ in range(60):
            c.send_input()
            time.sleep(0.005)
            srv.publish()
            c.poll(0.01)
    out = dict(client=c, session=ses, summary=summary, states=states, seen_text=seen_text)
    c.close()
    srv.stop()
    return out


@pytest.mark.parametrize("tcp_only", [False, True])
def test_stream_matches_server_state(tcp_only):
    r = _run(tcp_only, seconds=3.0, motion="circle")
    c, ses, s = r["client"], r["session"], r["summary"]
    sc = ses.scene
    assert len(c.nodes) == len(sc.nodes) and len(c.meshes) == len(sc.mesh_msgs)
    assert s["frames_per_s"] > 50 and s["lost_frames"] == 0
    # the decoded state equals the server's state up to quantisation (the sim thread is stopped: both are final)
    step = (sc.hi - sc.lo).max() / 65535
    assert np.abs(c.pose[:, :3] - sc.pos).max() < 2 * step
    assert np.abs(c.points[ses.rope] - ses.world.rope).max() < 2 * step
    assert np.abs(c.points[ses.cloth] - ses.world.cloth).max() < 2 * step
    assert (c.flags == sc.flags).all() and (c.rgba == sc.rgba).all()
    assert "tracking" in r["states"]                       # the clutch engaged and the tool followed
    assert np.linalg.norm(ses.world.tool_pos[1] - [0.35, -0.18, 0.22]) > 0.01
    assert ses.world.closure[1] > 0.0 or "tracking" in r["states"]
    assert s["latency_ms_median"] < 40.0


def test_delta_stream_is_small_when_the_scene_is_at_rest():
    r = _run(False, seconds=2.0, motion="still")
    # rope and cloth come to rest: a frame is then one header-only datagram
    assert r["summary"]["dgrams_per_frame"] < 2.5


def test_go_back_cue_and_haptics():
    r = _run(False, seconds=6.5, motion="jump")
    c, ses = r["client"], r["session"]
    assert "lost" in r["states"], "the 30 cm hand jump must trip the tether"
    assert r["states"][-1] == "tracking", "tracking resumes when the hand is back"
    assert any("Move your hand back" in t for d in r["seen_text"] for t in d.values())
    assert not c.texts, "cue text cleared after re-engaging"
    assert c.haptics[1]["count"] >= 2                      # engage tick + lost buzz at least
    shell = c.node("tool1/cue_shell")
    assert not (c.flags[shell] & P.F_VISIBLE)


def test_change_before_first_frame_is_sent():
    """A value that changes between the scene definition and the first state frame, and never again."""
    from quest_teleop.scene import Scene, box_mesh
    sc = Scene("t", bounds_min=(-1, -1, -1), bounds_max=(1, 1, 1))
    box = sc.add_node("box", sc.add_mesh(*box_mesh()))
    tube = sc.add_tube("tube", np.zeros((5, 3)), 0.01)
    srv = TeleopServer(port=0, beacon=False, verbose=False).start()
    srv.set_scene(sc)
    c = ProbeClient("127.0.0.1", srv.port)
    while not c.loaded:
        c.poll(0.01)
    time.sleep(0.05)
    pts = np.linspace([0, 0, 0], [0.5, 0.2, -0.3], 5)
    sc.set_points(tube, pts)
    sc.set_pose(box, (0.25, -0.5, 0.75), (0, 0, 0, 1))
    for _ in range(3):
        srv.publish()
        c.send_input()
        time.sleep(0.02)
        c.poll(0.02)
    assert np.abs(c.points[tube] - pts).max() < 1e-4
    assert np.abs(c.pose[box, :3] - [0.25, -0.5, 0.75]).max() < 1e-4
    c.close()
    srv.stop()


def test_packet_loss_heals():
    """30 % of the state datagrams are dropped: the client's state still converges to the server's, because every
    frame carries all changes since the last acknowledged frame."""
    r = _run(False, seconds=3.0, motion="circle", drop=0.3)
    c, ses = r["client"], r["session"]
    sc = ses.scene
    step = (sc.hi - sc.lo).max() / 65535
    assert c.lost_frames > 10
    assert np.abs(c.pose[:, :3] - sc.pos).max() < 2 * step
    assert np.abs(c.points[ses.rope] - ses.world.rope).max() < 2 * step
    assert np.abs(c.points[ses.cloth] - ses.world.cloth).max() < 2 * step
    assert (c.flags == sc.flags).all()


def test_status_page_and_recording(tmp_path):
    """Record a take with the library's recorder through the headset's Record action, then read everything back the
    way the browser does: the status page, its JSON with the latency breakdown, the recordings table, the
    trajectory view's data."""
    import json
    import urllib.request

    from quest_teleop.recording import load_take, take_summary
    from quest_teleop.status import StatusPage
    srv = TeleopServer(port=0, beacon=False, verbose=False).start()
    ses = DemoSession(srv, data_dir=str(tmp_path), session="demo")
    srv.set_scene(ses.scene)
    page = StatusPage(srv, port=0, extra=ses.status, title="Test", period=0.2, recordings_dir=str(tmp_path)).start()
    c = ProbeClient("127.0.0.1", srv.port)
    while not c.loaded:
        c.poll(0.01)
    c.press(1)                                                   # Start recording
    t1 = time.perf_counter()
    while time.perf_counter() - t1 < 1.6:
        set_motion(c, "circle", time.perf_counter() - t1 + 1.0)
        c.input.hands[1].buttons = 0                             # (the motion script's B presses would toggle recording)
        c.input.hands[1].trigger = 1.0 if time.perf_counter() - t1 > 0.5 else 0.0
        c.poll(0.002)
        inp = srv.get_input()
        t0 = time.perf_counter()
        ses.tick(1 / 60, inp)
        step = time.perf_counter() - t0
        srv.publish(echo=inp)
        srv.note_timing(step, time.perf_counter() - t0 - step)
        time.sleep(0.01)
    assert ses.recorder.recording and c.panel["status_style"] == 1
    c.press(1)                                                   # Stop
    for _ in range(5):
        c.poll(0.01)
        ses.tick(1 / 60, srv.get_input())
        srv.publish()
    ses.recorder.wait()
    time.sleep(0.5)
    base = f"http://127.0.0.1:{page.port}"
    html = urllib.request.urlopen(base + "/").read().decode()
    assert "<h1" in html and "Test" in html and "{{TITLE}}" not in html and 'lang="en"' in html
    assert urllib.request.urlopen(base + "/fonts/HostGrotesk-Regular.ttf").status == 200
    st = json.loads(urllib.request.urlopen(base + "/api/state").read())
    assert st["clients"][0]["name"] == "probe" and st["clients"][0]["link"].startswith("Wi-Fi") and st["clients"][0]["controlling"]
    assert st["session"]["name"] == "demo"
    hist = json.loads(urllib.request.urlopen(base + "/api/history").read())
    assert len(hist) >= 2 and "rtf" in hist[-1] and any(h["total"] is not None for h in hist) and any(h["rec"] for h in hist)
    ev = urllib.request.urlopen(base + "/events")
    assert ev.readline().startswith(b"data: {")
    ev.close()
    # the take on disk, in the generic format
    index = json.load(open(tmp_path / "index.json"))["takes"]
    assert len(index) == 1 and index[0]["tools"] == ["left", "right"] and index[0]["steps"] > 30 and index[0]["valid"]
    take = load_take(str(tmp_path / index[0]["file"]))
    T = index[0]["steps"]
    assert take["tool/pos"].shape == (T, 2, 3) and take["tool/quat"].shape == (T, 2, 4) and take["tool/opening"].shape == (T, 2)
    assert take["tool/grip_state"].shape == (T, 2) and take["ctrl/buttons"].shape == (T, 2) and take["extra/rope"].shape[0] == T
    summ = take_summary(take)
    assert summ["tools"][1]["name"] == "right" and summ["tools"][1]["path_m"] > 0.01
    assert (take["tool/grip_state"][:, 1] == 1).any(), "the right gripper was closing while the trigger was held"
    # the recordings table and the trajectory view
    table = st["recordings"]
    assert table["takes"][0]["href"] == "/take?file=" + index[0]["file"] and table["takes"][0]["validity"]["kind"] == "good"
    tj = json.loads(urllib.request.urlopen(base + "/api/take?file=" + index[0]["file"]).read())
    assert len(tj["tools"]) == 2 and len(tj["tools"][1]["pos"]) == len(tj["t"]) == T and tj["grip_names"][2] == "closed"
    assert tj["entry"]["take"] == 1 and "cube_pos" in tj["extra_keys"] and tj["tools"][1]["path_m"] > 0.01
    assert "Trajectory" in urllib.request.urlopen(base + "/take?file=" + index[0]["file"]).read().decode()
    for bad in ("/api/take?file=../status.py", "/api/take?file=nope.npz", "/fonts/../status.py"):
        try:
            urllib.request.urlopen(base + bad)
            assert False, "must not be served: " + bad
        except urllib.error.HTTPError as e:
            assert e.code == 404
    c.close()
    page.stop()
    srv.stop()

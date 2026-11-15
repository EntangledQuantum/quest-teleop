"""Wire format of quest-teleop, protocol version 1 (see docs/PROTOCOL.md). Everything is little-endian.

Two channels:
* TCP (reliable): handshake, scene definition (meshes, nodes), text cues. Messages are framed
  ``[u32 length][u16 type][payload]`` with length = 2 + len(payload).
* UDP (unreliable): controller input (client -> server) and state frames (server -> client). Every datagram starts
  with the 8-byte header ``[u16 magic][u8 version][u8 type][u32 session]``.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

import numpy as np

VERSION = 1
MAGIC = 0x5451                      # bytes 'Q', 'T'
DEFAULT_PORT = 47800                # TCP listen port and UDP data / discovery port
BEACON_PORT = 47801                 # UDP port clients listen on for broadcast OFFER beacons
MAX_DATAGRAM = 1200                 # bytes, including the 8-byte header

# datagram types
D_DISCOVER, D_OFFER, D_INPUT, D_STATE = 1, 2, 3, 4
# TCP message types
(M_HELLO, M_WELCOME, M_SCENE_BEGIN, M_MESH, M_NODE, M_SCENE_END, M_SCENE_READY, M_TEXT, M_DGRAM, M_PING, M_PONG,
 M_BYE, M_PANEL, M_ACTION) = range(1, 15)
# state chunk types
C_POSE, C_VERTS, C_APPEARANCE = 1, 2, 3
# node kinds
K_MESH, K_DEFORM, K_TUBE = 1, 2, 3
# node parents (the frame a node's pose is given in)
P_SCENE, P_STAGE, P_HEAD, P_LEFT_GRIP, P_RIGHT_GRIP, P_LEFT_AIM, P_RIGHT_AIM = range(7)
# node flags
F_VISIBLE, F_BLEND, F_UNLIT, F_ON_TOP, F_FLAT, F_HIGHLIGHT = 1, 2, 4, 8, 16, 32
# mesh flags
MF_NORMALS, MF_COLORS, MF_INDEX32, MF_DOUBLE_SIDED = 1, 2, 4, 8
# HELLO flags
HF_TCP_ONLY = 1
# input flags
IF_EDIT_MODE, IF_FOCUSED, IF_PASSTHROUGH = 1, 2, 4
# hand flags
HAND_ACTIVE, HAND_GRIP_VALID, HAND_AIM_VALID, HAND_TRACKED = 1, 2, 4, 8
# buttons
(B_PRIMARY, B_SECONDARY, B_STICK, B_MENU, B_TRIGGER_TOUCH, B_STICK_TOUCH, B_PRIMARY_TOUCH, B_SECONDARY_TOUCH,
 B_THUMBREST_TOUCH) = (1 << i for i in range(9))
# text anchors: 0 = HUD (head-locked), 1 + parent = attached to that frame
TEXT_HUD = 0

LOCAL_BOUNDS = (np.full(3, -2.0, np.float32), np.full(3, 2.0, np.float32))   # pose range of non-SCENE parents [m]

DGRAM_HDR = struct.Struct("<HBBI")
INPUT_HEAD = struct.Struct("<IQIHHI8f7f")          # seq, t_us, ack_frame, ack_scene, flags, latency_us, placement, head
INPUT_HAND = struct.Struct("<HHffff7f7f3f3f")      # flags, buttons, trigger, squeeze, stick xy, grip, aim, vel, angvel
STATE_HEAD = struct.Struct("<HIBBQIQI")            # scene, frame, frag, nfrag, t_us, echo seq, echo t_us, echo hold_us
STATE_HAPTIC = struct.Struct("<HBBHH")             # seq, amplitude, reserved, duration_ms, frequency_hz
CHUNK_HDR = struct.Struct("<BBH")                  # type, reserved, node
STATE_OVERHEAD = DGRAM_HDR.size + STATE_HEAD.size + 2 * STATE_HAPTIC.size
QUAT_SCALE = 32767.0 * np.sqrt(2.0)


# ------------------------------------------------------------------------------------------------ helpers
def pack_str(s: str) -> bytes:
    b = s.encode("utf-8")[:65535]
    return struct.pack("<H", len(b)) + b


def unpack_str(buf, off: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("<H", buf, off)
    return bytes(buf[off + 2:off + 2 + n]).decode("utf-8", "replace"), off + 2 + n


def frame_msg(mtype: int, payload: bytes = b"") -> bytes:
    return struct.pack("<IH", 2 + len(payload), mtype) + payload


def dgram(dtype: int, session: int, payload: bytes = b"") -> bytes:
    return DGRAM_HDR.pack(MAGIC, VERSION, dtype, session) + payload


def quantize_pos(p, lo, hi) -> np.ndarray:
    """(..., 3) float -> uint16, linear over the box [lo, hi] (clamped)."""
    u = (np.asarray(p, np.float64) - lo) / np.maximum(hi - lo, 1e-9)
    return np.clip(np.rint(u * 65535.0), 0, 65535).astype(np.uint16)


def dequantize_pos(q, lo, hi) -> np.ndarray:
    return (np.asarray(q, np.float32) / 65535.0) * (hi - lo) + lo


def quantize_quat(q) -> np.ndarray:
    """(N, 4) xyzw unit quaternions -> (N, 4) int16: the three components that are not the largest (in xyzw order),
    scaled by 32767 * sqrt(2), and the index of the dropped component (sign chosen so that it is positive)."""
    q = np.asarray(q, np.float64).reshape(-1, 4)
    q = q / np.maximum(np.linalg.norm(q, axis=1, keepdims=True), 1e-12)
    k = np.argmax(np.abs(q), axis=1)
    rows = np.arange(len(q))
    q = q * np.where(q[rows, k] < 0, -1.0, 1.0)[:, None]
    keep = np.array([[1, 2, 3], [0, 2, 3], [0, 1, 3], [0, 1, 2]])[k]
    out = np.empty((len(q), 4), np.int16)
    out[:, :3] = np.clip(np.rint(q[rows[:, None], keep] * QUAT_SCALE), -32767, 32767)
    out[:, 3] = k
    return out


def dequantize_quat(qq) -> np.ndarray:
    qq = np.asarray(qq).reshape(-1, 4)
    k = qq[:, 3].astype(int)
    c = qq[:, :3].astype(np.float64) / QUAT_SCALE
    keep = np.array([[1, 2, 3], [0, 2, 3], [0, 1, 3], [0, 1, 2]])[k]
    out = np.zeros((len(qq), 4))
    rows = np.arange(len(qq))
    out[rows[:, None], keep] = c
    out[rows, k] = np.sqrt(np.maximum(0.0, 1.0 - (c * c).sum(1)))
    return out


# ------------------------------------------------------------------------------------------------ input
@dataclass
class Hand:
    flags: int = 0
    buttons: int = 0
    trigger: float = 0.0
    squeeze: float = 0.0
    stick: np.ndarray = field(default_factory=lambda: np.zeros(2, np.float32))
    grip_pos: np.ndarray = field(default_factory=lambda: np.zeros(3, np.float32))
    grip_quat: np.ndarray = field(default_factory=lambda: np.array([0, 0, 0, 1], np.float32))
    aim_pos: np.ndarray = field(default_factory=lambda: np.zeros(3, np.float32))
    aim_quat: np.ndarray = field(default_factory=lambda: np.array([0, 0, 0, 1], np.float32))
    vel: np.ndarray = field(default_factory=lambda: np.zeros(3, np.float32))
    angvel: np.ndarray = field(default_factory=lambda: np.zeros(3, np.float32))

    @property
    def active(self) -> bool:
        return bool(self.flags & HAND_ACTIVE) and bool(self.flags & HAND_GRIP_VALID)

    def button(self, mask: int) -> bool:
        return bool(self.buttons & mask)


@dataclass
class InputFrame:
    """One controller sample. Poses are in the client's STAGE space (y up, metres, floor origin) unless obtained
    through `scene_pose`; quaternions are xyzw. `placement` is where the client currently draws the scene:
    p_stage = place_pos + place_scale * rotate(place_quat, p_scene)."""
    seq: int = 0
    client_time_us: int = 0
    ack_frame: int = 0
    ack_scene: int = 0
    flags: int = 0
    latency_us: int = 0
    place_pos: np.ndarray = field(default_factory=lambda: np.zeros(3, np.float32))
    place_quat: np.ndarray = field(default_factory=lambda: np.array([0, 0, 0, 1], np.float32))
    place_scale: float = 1.0
    head_pos: np.ndarray = field(default_factory=lambda: np.zeros(3, np.float32))
    head_quat: np.ndarray = field(default_factory=lambda: np.array([0, 0, 0, 1], np.float32))
    hands: tuple = field(default_factory=lambda: (Hand(), Hand()))      # (left, right)
    recv_time: float = 0.0             # server clock (time.perf_counter) when the datagram arrived

    @property
    def edit_mode(self) -> bool:
        return bool(self.flags & IF_EDIT_MODE)

    def scene_pose(self, pos, quat) -> tuple[np.ndarray, np.ndarray]:
        """A stage-space pose expressed in scene (simulation) coordinates."""
        qi = quat_conj(self.place_quat)
        p = quat_rotate(qi, np.asarray(pos, np.float64) - self.place_pos) / max(float(self.place_scale), 1e-9)
        return p, quat_mul(qi, quat)

    def hand_in_scene(self, i: int, aim: bool = False) -> tuple[np.ndarray, np.ndarray]:
        h = self.hands[i]
        return self.scene_pose(h.aim_pos, h.aim_quat) if aim else self.scene_pose(h.grip_pos, h.grip_quat)


def pack_input(session: int, f: InputFrame) -> bytes:
    out = [DGRAM_HDR.pack(MAGIC, VERSION, D_INPUT, session),
           INPUT_HEAD.pack(f.seq, f.client_time_us, f.ack_frame, f.ack_scene, f.flags, f.latency_us,
                           *f.place_pos, *f.place_quat, f.place_scale, *f.head_pos, *f.head_quat)]
    for h in f.hands:
        out.append(INPUT_HAND.pack(h.flags, h.buttons, h.trigger, h.squeeze, *h.stick, *h.grip_pos, *h.grip_quat,
                                   *h.aim_pos, *h.aim_quat, *h.vel, *h.angvel))
    return b"".join(out)


def unpack_input(buf) -> InputFrame:
    """buf: datagram payload after the 8-byte header."""
    v = INPUT_HEAD.unpack_from(buf, 0)
    f = InputFrame(seq=v[0], client_time_us=v[1], ack_frame=v[2], ack_scene=v[3], flags=v[4], latency_us=v[5],
                   place_pos=np.array(v[6:9]), place_quat=np.array(v[9:13]), place_scale=v[13],
                   head_pos=np.array(v[14:17]), head_quat=np.array(v[17:21]))
    hands, off = [], INPUT_HEAD.size
    for _ in range(2):
        w = INPUT_HAND.unpack_from(buf, off)
        off += INPUT_HAND.size
        hands.append(Hand(flags=w[0], buttons=w[1], trigger=w[2], squeeze=w[3], stick=np.array(w[4:6]),
                          grip_pos=np.array(w[6:9]), grip_quat=np.array(w[9:13]), aim_pos=np.array(w[13:16]),
                          aim_quat=np.array(w[16:20]), vel=np.array(w[20:23]), angvel=np.array(w[23:26])))
    f.hands = tuple(hands)
    return f


# ------------------------------------------------------------------------------------------------ meshes
def pack_mesh(mesh_id: int, vertices, faces, normals=None, colors=None, double_sided=False) -> bytes:
    """MESH message payload. vertices (V, 3) float, faces (F, 3) int, normals (V, 3) optional, colors (V, 4) uint8 or
    float 0..1 optional. Positions are quantised to 16 bit inside the mesh's bounding box, normals to 8 bit; the
    block is deflate-compressed."""
    v = np.asarray(vertices, np.float32).reshape(-1, 3)
    idx = np.asarray(faces).reshape(-1)
    lo, hi = (v.min(0), v.max(0)) if len(v) else (np.zeros(3, np.float32), np.zeros(3, np.float32))
    flags = 0
    parts = [quantize_pos(v, lo, hi).tobytes()]
    if normals is not None:
        flags |= MF_NORMALS
        n = np.asarray(normals, np.float32).reshape(-1, 3)
        n = n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        n4 = np.zeros((len(v), 4), np.int8)
        n4[:, :3] = np.rint(n * 127.0)
        parts.append(n4.tobytes())
    if colors is not None:
        flags |= MF_COLORS
        c = np.asarray(colors)
        if c.dtype != np.uint8:
            c = np.clip(np.rint(c * 255.0), 0, 255).astype(np.uint8)
        if c.shape[1] == 3:
            c = np.concatenate([c, np.full((len(c), 1), 255, np.uint8)], 1)
        parts.append(np.ascontiguousarray(c).tobytes())
    if len(v) > 65535:
        flags |= MF_INDEX32
        parts.append(idx.astype(np.uint32).tobytes())
    else:
        parts.append(idx.astype(np.uint16).tobytes())
    if double_sided:
        flags |= MF_DOUBLE_SIDED
    raw = b"".join(parts)
    return struct.pack("<HHII6fI", mesh_id, flags, len(v), len(idx), *lo, *hi, len(raw)) + zlib.compress(raw, 6)


def unpack_mesh(buf) -> dict:
    mesh_id, flags, nv, ni = struct.unpack_from("<HHII", buf, 0)
    b = struct.unpack_from("<6fI", buf, 12)
    lo, hi = np.array(b[:3], np.float32), np.array(b[3:6], np.float32)
    raw = zlib.decompress(bytes(buf[40:]))
    assert len(raw) == b[6]
    off = 0
    v = dequantize_pos(np.frombuffer(raw, np.uint16, nv * 3, off).reshape(-1, 3), lo, hi)
    off += nv * 6
    out = dict(mesh_id=mesh_id, flags=flags, vertices=v, normals=None, colors=None)
    if flags & MF_NORMALS:
        out["normals"] = np.frombuffer(raw, np.int8, nv * 4, off).reshape(-1, 4)[:, :3] / 127.0
        off += nv * 4
    if flags & MF_COLORS:
        out["colors"] = np.frombuffer(raw, np.uint8, nv * 4, off).reshape(-1, 4)
        off += nv * 4
    out["faces"] = np.frombuffer(raw, np.uint32 if flags & MF_INDEX32 else np.uint16, ni, off).reshape(-1, 3)
    return out


# ------------------------------------------------------------------------------------------------ quaternions (xyzw)
def quat_mul(a, b):
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    ax, ay, az, aw = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    bx, by, bz, bw = b[..., 0], b[..., 1], b[..., 2], b[..., 3]
    return np.stack([aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz], -1)


def quat_conj(q):
    q = np.asarray(q, np.float64)
    return q * np.array([-1.0, -1.0, -1.0, 1.0])


def quat_rotate(q, v):
    q, v = np.asarray(q, np.float64), np.asarray(v, np.float64)
    u, w = q[..., :3], q[..., 3:4]
    t = 2.0 * np.cross(u, v)
    return v + w * t + np.cross(u, t)


def quat_angle(a, b) -> float:
    """Rotation angle [rad] between two orientations."""
    d = abs(float(np.dot(np.asarray(a, np.float64), np.asarray(b, np.float64))))
    return 2.0 * float(np.arccos(min(1.0, d)))


def quat_from_rotvec(r):
    r = np.asarray(r, np.float64)
    a = float(np.linalg.norm(r))
    if a < 1e-12:
        return np.array([0.0, 0.0, 0.0, 1.0])
    return np.r_[r / a * np.sin(0.5 * a), np.cos(0.5 * a)]


def quat_to_rotvec(q):
    q = np.asarray(q, np.float64)
    if q[3] < 0:
        q = -q
    s = float(np.linalg.norm(q[:3]))
    if s < 1e-12:
        return np.zeros(3)
    return q[:3] / s * (2.0 * np.arctan2(s, q[3]))


def quat_to_matrix(q):
    x, y, z, w = np.asarray(q, np.float64)
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])

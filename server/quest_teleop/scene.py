"""Server-side scene: what the headset draws. Task-agnostic: meshes, nodes that carry them, and per-frame state.

    sc = Scene("demo", bounds_min=(-1, -1, 0), bounds_max=(1, 1, 2), up_axis=2)
    m = sc.add_mesh(vertices, faces, colors=vertex_colors)
    box = sc.add_node("box", m, pos=(0, 0, 0.5))
    cloth = sc.add_deform("cloth", vertices, faces, color=(0.8, 0.2, 0.2, 1))
    rope = sc.add_tube("rope", points, radius=0.002)
    ...
    sc.set_pose(box, pos, quat); sc.set_points(cloth, new_vertices); sc.set_points(rope, new_points)

Node kinds:
* MESH: a rigid mesh; per frame only its pose changes.
* DEFORM: a mesh whose vertex positions are streamed (topology fixed).
* TUBE: a polyline with a radius (rope, cable, pointer line); its points are streamed and the client builds the tube.

All positions are metres; quaternions are xyzw. Poses of nodes whose parent is SCENE are in the scene's (simulator's)
world frame; the client places, rotates and scales that frame in the room. Positions sent per frame are quantised to
16 bit over the scene bounds, so the bounds must contain everything that moves (values outside are clamped).
"""
from __future__ import annotations

import struct

import numpy as np

from . import protocol as P


def _rgba8(c) -> np.ndarray:
    c = np.asarray(c, np.float64).reshape(-1)
    if len(c) == 3:
        c = np.r_[c, 1.0]
    return np.clip(np.rint(c * 255.0), 0, 255).astype(np.uint8)


class Scene:
    def __init__(self, name: str = "scene", bounds_min=(-1.0, -1.0, -1.0), bounds_max=(1.0, 1.0, 1.0),
                 up_axis: int = 2, focus_center=None, focus_radius: float | None = None,
                 view_dir=(0.0, 0.0, 0.0)):
        """up_axis: 0 / 1 / 2 = the scene's up direction is +x / +y / +z. focus_center / focus_radius: the region
        the operator works in; the client uses it for the initial placement and scale. view_dir: the horizontal direction (scene
        coordinates) the operator should look along at the start; zero = no preference."""
        self.view_dir = np.asarray(view_dir, np.float32)
        self.name = name
        self.lo = np.asarray(bounds_min, np.float32)
        self.hi = np.asarray(bounds_max, np.float32)
        self.up_axis = int(up_axis)
        self.focus_center = np.asarray(0.5 * (self.lo + self.hi) if focus_center is None else focus_center, np.float32)
        self.focus_radius = float(0.5 * np.linalg.norm(self.hi - self.lo) if focus_radius is None else focus_radius)
        self.mesh_msgs: list[bytes] = []            # MESH payloads, index = mesh id
        self.nodes: list[dict] = []
        self.pos = np.zeros((0, 3))
        self.quat = np.zeros((0, 4))
        self.rgba = np.zeros((0, 4), np.uint8)
        self.flags = np.zeros(0, np.uint16)
        self.verts = np.zeros((0, 3), np.float32)   # all streamed vertices / tube points, concatenated
        self.v_off: dict[int, tuple[int, int]] = {}  # node -> (offset, count)
        self.revision = 0                           # bumped by anything that changes the definition
        # Streamed vertices / tube points are re-sent only when they moved more than this [m] since they were last
        # sent. 0 = every change of the 16-bit value. A small value (a few hundredths of a millimetre) stops solver
        # jitter in resting cloth from being streamed.
        self.vertex_deadband = 0.0

    # ---------------------------------------------------------------------------------------- definition
    def add_mesh(self, vertices, faces, normals=None, colors=None, double_sided: bool = False) -> int:
        mid = len(self.mesh_msgs)
        self.mesh_msgs.append(P.pack_mesh(mid, vertices, faces, normals, colors, double_sided))
        self.revision += 1
        return mid

    def _add(self, name, kind, mesh, pos, quat, scale, color, parent, flags, param, points) -> int:
        nid = len(self.nodes)
        if nid >= 65535:
            raise ValueError("too many nodes")
        scale = np.broadcast_to(np.asarray(scale, np.float32), (3,)).copy()
        rgba = _rgba8(color)
        if rgba[3] < 255:
            flags |= P.F_BLEND
        self.nodes.append(dict(name=name, kind=kind, mesh=0xFFFF if mesh is None else int(mesh), parent=int(parent),
                               scale=scale, param=float(param), n_points=0 if points is None else len(points)))
        self.pos = np.vstack([self.pos, np.asarray(pos, np.float64).reshape(1, 3)])
        self.quat = np.vstack([self.quat, np.asarray(quat, np.float64).reshape(1, 4)])
        self.rgba = np.vstack([self.rgba, rgba.reshape(1, 4)])
        self.flags = np.r_[self.flags, np.uint16(flags)]
        if points is not None:
            pts = np.asarray(points, np.float32).reshape(-1, 3)
            self.v_off[nid] = (len(self.verts), len(pts))
            self.verts = np.vstack([self.verts, pts])
        self.revision += 1
        return nid

    def add_node(self, name: str, mesh: int | None, pos=(0, 0, 0), quat=(0, 0, 0, 1), scale=1.0, color=(1, 1, 1, 1),
                 parent: int = P.P_SCENE, flags: int = P.F_VISIBLE) -> int:
        """A rigid mesh. color multiplies the mesh's vertex colours (alpha < 1 draws it blended)."""
        return self._add(name, P.K_MESH, mesh, pos, quat, scale, color, parent, flags, 0.0, None)

    def add_deform(self, name: str, vertices, faces, color=(1, 1, 1, 1), colors=None, parent: int = P.P_SCENE,
                   flags: int = P.F_VISIBLE, double_sided: bool = True) -> int:
        """A mesh with streamed vertex positions (`set_points`). Drawn flat-shaded (normals from the triangles)."""
        if len(vertices) > 65535:
            raise ValueError("a deformable mesh can have at most 65535 vertices")
        mesh = self.add_mesh(vertices, faces, colors=colors, double_sided=double_sided)
        return self._add(name, P.K_DEFORM, mesh, (0, 0, 0), (0, 0, 0, 1), 1.0, color, parent, flags | P.F_FLAT, 0.0,
                         vertices)

    def add_tube(self, name: str, points, radius: float, color=(1, 1, 1, 1), parent: int = P.P_SCENE,
                 flags: int = P.F_VISIBLE) -> int:
        """A polyline of fixed point count drawn as a tube of the given radius (`set_points` moves it)."""
        return self._add(name, P.K_TUBE, None, (0, 0, 0), (0, 0, 0, 1), 1.0, color, parent, flags, radius, points)

    # ---------------------------------------------------------------------------------------- per-frame state
    def set_pose(self, node: int, pos=None, quat=None) -> None:
        if pos is not None:
            self.pos[node] = pos
        if quat is not None:
            self.quat[node] = quat

    def set_poses(self, nodes, pos, quat) -> None:
        self.pos[nodes] = pos
        self.quat[nodes] = quat

    def set_points(self, node: int, points) -> None:
        o, n = self.v_off[node]
        self.verts[o:o + n] = points

    def set_visible(self, node: int, visible: bool) -> None:
        self.flags[node] = (int(self.flags[node]) | P.F_VISIBLE) if visible else (int(self.flags[node]) & ~P.F_VISIBLE)

    def set_color(self, node: int, color) -> None:
        self.rgba[node] = _rgba8(color)

    # ---------------------------------------------------------------------------------------- wire
    def bounds_of(self, node: int):
        return (self.lo, self.hi) if self.nodes[node]["parent"] == P.P_SCENE else P.LOCAL_BOUNDS

    def definition_messages(self, scene_id: int) -> list[bytes]:
        """The framed TCP messages that define this scene (current state as the initial state)."""
        out = [P.frame_msg(P.M_SCENE_BEGIN, struct.pack("<HBB6f3ff3fHH", scene_id, self.up_axis, 0, *self.lo, *self.hi,
                                                        *self.focus_center, self.focus_radius, *self.view_dir,
                                                        len(self.mesh_msgs),
                                                        len(self.nodes)) + P.pack_str(self.name))]
        out += [P.frame_msg(P.M_MESH, m) for m in self.mesh_msgs]
        for i, n in enumerate(self.nodes):
            body = struct.pack("<HBBHH4B3f4f3ffH", i, n["kind"], n["parent"], n["mesh"], int(self.flags[i]),
                               *self.rgba[i], *self.pos[i], *self.quat[i], *n["scale"], n["param"], n["n_points"])
            if n["kind"] == P.K_TUBE:
                o, c = self.v_off[i]
                body += self.verts[o:o + c].astype(np.float32).tobytes()
            out.append(P.frame_msg(P.M_NODE, body + P.pack_str(n["name"])))
        out.append(P.frame_msg(P.M_SCENE_END, struct.pack("<H", scene_id)))
        return out

    def quantized(self):
        """(pose (N, 7) int32: pos u16 x 3, quat i16 x 3, dropped index), verts (T, 3) uint16, appearance (N, 5))."""
        n = len(self.nodes)
        pose = np.zeros((n, 7), np.int32)
        scene_parent = np.array([nd["parent"] == P.P_SCENE for nd in self.nodes], bool)
        if n:
            lo = np.where(scene_parent[:, None], self.lo, P.LOCAL_BOUNDS[0])
            hi = np.where(scene_parent[:, None], self.hi, P.LOCAL_BOUNDS[1])
            pose[:, :3] = P.quantize_pos(self.pos, lo, hi)
            pose[:, 3:] = P.quantize_quat(self.quat)
        vq = np.zeros((len(self.verts), 3), np.uint16)
        for node, (o, c) in self.v_off.items():
            lo, hi = self.bounds_of(node)
            vq[o:o + c] = P.quantize_pos(self.verts[o:o + c], lo, hi)
        app = np.concatenate([self.rgba.astype(np.int32), self.flags.astype(np.int32)[:, None]], 1)
        return pose, vq, app


# ---------------------------------------------------------------------------------------------- primitive meshes
def box_mesh(half=(0.5, 0.5, 0.5)):
    """(vertices (24, 3), faces (12, 3), normals (24, 3)) of a box with flat faces."""
    hx, hy, hz = half
    v, n, f = [], [], []
    for axis in range(3):
        for s in (-1.0, 1.0):
            a, b = (axis + 1) % 3, (axis + 2) % 3
            nrm = np.zeros(3)
            nrm[axis] = s
            base = len(v)
            for da, db in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                p = np.zeros(3)
                p[axis], p[a], p[b] = s, da, db
                v.append(p * [hx, hy, hz])
                n.append(nrm)
            f += [[base, base + 1, base + 2], [base, base + 2, base + 3]] if s > 0 else \
                 [[base, base + 2, base + 1], [base, base + 3, base + 2]]
    return np.array(v, np.float32), np.array(f, np.int32), np.array(n, np.float32)


def sphere_mesh(radius=0.5, rings=12, sectors=20):
    th = np.linspace(0.0, np.pi, rings + 1)
    ph = np.linspace(0.0, 2 * np.pi, sectors + 1)
    T, Ph = np.meshgrid(th, ph, indexing="ij")
    n = np.stack([np.sin(T) * np.cos(Ph), np.sin(T) * np.sin(Ph), np.cos(T)], -1).reshape(-1, 3)
    f = []
    for i in range(rings):
        for j in range(sectors):
            a, b = i * (sectors + 1) + j, (i + 1) * (sectors + 1) + j
            f += [[a, b, a + 1], [a + 1, b, b + 1]]
    return (n * radius).astype(np.float32), np.array(f, np.int32), n.astype(np.float32)


def cylinder_mesh(radius=0.5, half_height=0.5, sectors=20, cap_round=False):
    """Along +z. cap_round=True makes a capsule (hemispherical caps beyond +-half_height)."""
    if cap_round:
        rings = 6
        th = np.r_[np.linspace(0.0, 0.5 * np.pi, rings + 1), np.linspace(0.5 * np.pi, np.pi, rings + 1)]
        zoff = np.r_[np.full(rings + 1, half_height), np.full(rings + 1, -half_height)]
        ph = np.linspace(0.0, 2 * np.pi, sectors + 1)
        T, Ph = np.meshgrid(th, ph, indexing="ij")
        n = np.stack([np.sin(T) * np.cos(Ph), np.sin(T) * np.sin(Ph), np.cos(T)], -1)
        v = n * radius + np.stack([np.zeros_like(T), np.zeros_like(T), np.broadcast_to(zoff[:, None], T.shape)], -1)
        f = []
        for i in range(len(th) - 1):
            for j in range(sectors):
                a, b = i * (sectors + 1) + j, (i + 1) * (sectors + 1) + j
                f += [[a, b, a + 1], [a + 1, b, b + 1]]
        return v.reshape(-1, 3).astype(np.float32), np.array(f, np.int32), n.reshape(-1, 3).astype(np.float32)
    ph = np.linspace(0.0, 2 * np.pi, sectors + 1)
    c, s = np.cos(ph), np.sin(ph)
    side_n = np.stack([c, s, np.zeros_like(c)], -1)
    v = [np.c_[radius * c, radius * s, np.full_like(c, half_height)], np.c_[radius * c, radius * s, np.full_like(c, -half_height)]]
    n = [side_n, side_n]
    f = []
    m = sectors + 1
    for j in range(sectors):
        f += [[j, m + j, j + 1], [j + 1, m + j, m + j + 1]]
    for sgn in (1.0, -1.0):                                   # caps
        base = sum(len(x) for x in v)
        v.append(np.r_[[[0.0, 0.0, sgn * half_height]], np.c_[radius * c, radius * s, np.full_like(c, sgn * half_height)]])
        n.append(np.tile([0.0, 0.0, sgn], (m + 1, 1)))
        for j in range(sectors):
            f.append([base, base + 1 + j, base + 2 + j] if sgn > 0 else [base, base + 2 + j, base + 1 + j])
    return np.concatenate(v).astype(np.float32), np.array(f, np.int32), np.concatenate(n).astype(np.float32)

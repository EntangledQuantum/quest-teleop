"""The "go back to the robot" cue, built from ordinary scene nodes so the client needs no special logic.

While a `ToolTeleop` is LOST the client shows:
* a bright see-through shell around the tool, where the robot really is (drawn on top of everything);
* a small marker where the hand currently asks the tool to be;
* a line between the two;
* a text asking the operator to move back.
"""
from __future__ import annotations

import numpy as np

from . import protocol as P
from .scene import Scene, sphere_mesh
from .teleop import TeleopOut


class GoBackCue:
    def __init__(self, scene: Scene, name: str, tool_mesh: int | None = None, color=(1.0, 0.75, 0.1),
                 marker_radius: float = 0.012, shell_scale: float = 1.25, text_slot: int = 0,
                 text: str = "Robot cannot follow.\nMove your hand back to the highlighted tool."):
        self.scene, self.text_slot, self.text = scene, text_slot, text
        v, f, n = sphere_mesh(1.0, 10, 16)
        sphere = scene.add_mesh(v, f, n)
        hidden = P.F_UNLIT | P.F_ON_TOP | P.F_BLEND
        self.shell = scene.add_node(f"{name}/cue_shell", sphere if tool_mesh is None else tool_mesh,
                                    scale=(marker_radius * 2.5 if tool_mesh is None else shell_scale),
                                    color=(*color, 0.45), flags=hidden)
        self.marker = scene.add_node(f"{name}/cue_wish", sphere, scale=marker_radius, color=(*color, 0.9), flags=hidden)
        self.line = scene.add_tube(f"{name}/cue_line", np.zeros((2, 3)), radius=0.0025, color=(*color, 0.9), flags=hidden)
        self._on = False
        self._t = 0.0

    def update(self, dt: float, out: TeleopOut, tool_pos, tool_quat, server=None) -> None:
        sc = self.scene
        on = bool(out.cue)
        if on:
            self._t += dt
            pulse = 0.35 + 0.25 * np.sin(2 * np.pi * 1.5 * self._t)
            sc.set_pose(self.shell, tool_pos, tool_quat)
            sc.rgba[self.shell, 3] = int(255 * pulse)
            sc.set_pose(self.marker, out.wish_pos, out.wish_quat)
            sc.set_points(self.line, np.stack([out.wish_pos, np.asarray(tool_pos, float)]))
        if on != self._on:
            for n in (self.shell, self.marker, self.line):
                sc.set_visible(n, on)
            if server is not None:
                server.set_text(self.text_slot, self.text if on else "", anchor=1 + P.P_SCENE,
                                pos=np.asarray(tool_pos, float) + np.array([0.0, 0.0, 0.0]), size=0.018,
                                color=(1.0, 0.85, 0.2, 1.0))
            self._on, self._t = on, 0.0

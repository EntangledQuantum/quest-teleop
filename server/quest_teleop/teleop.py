"""Controller -> robot-tool mapping that is safe against moves the robot cannot follow. Simulator-agnostic.

`ToolTeleop` turns one controller into a tool-pose command:

* The operator holds the clutch (grip button by default) to move the tool. With `mode="clutch"` the tool follows the
  hand's motion relative to where the clutch was pressed (hand and tool need not coincide); with `mode="absolute"` the
  tool goes where the hand is, so the hand must first be brought to the tool.
* The command moves toward the hand's wish at a bounded speed and never leads the measured tool pose by more than
  `max_lead`, so the robot does not jump.
* If the wish gets further from the measured tool than the tether (sudden move, joint limit, out of reach, blocked by
  contact), tracking stops (state LOST): the robot holds, and `cue` tells the client to show where the tool really
  is. Tracking resumes when the hand's wish is back within the re-engage tolerance of the tool (or, in clutch mode,
  when the operator releases and presses the clutch again).

`RateGripper` integrates "close slowly while the trigger is held / open slowly while a button is held".
`ContactHaptics` turns a contact-force signal into controller vibration.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import protocol as P

IDLE, TRACKING, LOST = "idle", "tracking", "lost"


@dataclass
class TeleopCfg:
    mode: str = "clutch"               # "clutch" (relative) or "absolute" (hand = tool)
    max_speed: float = 0.25            # tool command speed limit [m/s]
    max_angular_speed: float = 2.0     # [rad/s]
    max_lead: float = 0.03             # the command never leads the measured tool by more than this [m]
    max_angular_lead: float = 0.35     # [rad]
    tether: float = 0.08               # wish further than this from the measured tool -> LOST [m]
    tether_angle: float = 0.9          # [rad]
    tether_time: float = 0.25          # ... for longer than this [s] (twice the tether: immediately)
    reengage: float = 0.03             # LOST -> TRACKING when the wish is back within this [m]
    reengage_angle: float = 0.45       # [rad]
    motion_scale: float = 1.0          # hand motion -> tool motion (on top of the client's scene scale)
    absolute_offset_pos: tuple = (0.0, 0.0, 0.0)        # tool pose in the hand (grip) frame, absolute mode
    absolute_offset_quat: tuple = (0.0, 0.0, 0.0, 1.0)


@dataclass
class TeleopOut:
    state: str
    cmd_pos: np.ndarray                # tool pose to command this step
    cmd_quat: np.ndarray
    wish_pos: np.ndarray               # where the hand asks the tool to be
    wish_quat: np.ndarray
    lag: float                         # |wish - measured| [m]
    lag_angle: float                   # [rad]
    cue: bool                          # show the "go back" cue
    engaged_edge: bool                 # became TRACKING this step
    lost_edge: bool                    # became LOST this step


class ToolTeleop:
    def __init__(self, cfg: TeleopCfg | None = None):
        self.cfg = cfg or TeleopCfg()
        self.state = IDLE
        self._clutch = False
        self._hand0 = self._tool0 = None
        self._cmd_pos = self._cmd_quat = None
        self._over = 0.0

    def reset(self) -> None:
        self.state, self._clutch, self._hand0, self._cmd_pos, self._over = IDLE, False, None, None, 0.0

    def _wish(self, hand_pos, hand_quat):
        c = self.cfg
        if c.mode == "absolute":
            return (hand_pos + P.quat_rotate(hand_quat, np.asarray(c.absolute_offset_pos, float)),
                    P.quat_mul(hand_quat, np.asarray(c.absolute_offset_quat, float)))
        (hp0, hq0), (tp0, tq0) = self._hand0, self._tool0
        return (tp0 + c.motion_scale * (hand_pos - hp0),
                P.quat_mul(P.quat_mul(hand_quat, P.quat_conj(hq0)), tq0))

    def update(self, dt: float, hand, clutch: bool, tool_pos, tool_quat, reachable: bool = True) -> TeleopOut:
        """hand: (pos, quat) of the controller in scene coordinates, or None when it is not tracked.
        tool_pos / tool_quat: the measured tool pose. reachable: False when the simulator knows the last command was
        outside what the robot can do (IK residual, joint limit); it then counts like an exceeded tether.
        Returns the command for this step."""
        c = self.cfg
        tool_pos, tool_quat = np.asarray(tool_pos, float), np.asarray(tool_quat, float)
        if self._cmd_pos is None:
            self._cmd_pos, self._cmd_quat = tool_pos.copy(), tool_quat.copy()
        prev = self.state
        pressed = clutch and not self._clutch
        self._clutch = bool(clutch)
        if hand is None or not clutch:
            self.state = IDLE
            self._over = 0.0
            return TeleopOut(IDLE, self._cmd_pos, self._cmd_quat, tool_pos, tool_quat, 0.0, 0.0, False, False, False)
        hand_pos, hand_quat = np.asarray(hand[0], float), np.asarray(hand[1], float)
        if c.mode == "clutch" and (pressed or self._hand0 is None or prev == IDLE):
            self._hand0, self._tool0 = (hand_pos.copy(), hand_quat.copy()), (tool_pos.copy(), tool_quat.copy())
            self._cmd_pos, self._cmd_quat = tool_pos.copy(), tool_quat.copy()
            self.state = TRACKING
        wish_pos, wish_quat = self._wish(hand_pos, hand_quat)
        lag = float(np.linalg.norm(wish_pos - tool_pos))
        lag_a = P.quat_angle(wish_quat, tool_quat)
        if self.state == IDLE:                                   # absolute mode, clutch held: engage when docked
            self.state = LOST
        if self.state == LOST:
            if lag < c.reengage and lag_a < c.reengage_angle:
                self.state = TRACKING
                self._cmd_pos, self._cmd_quat = tool_pos.copy(), tool_quat.copy()
                self._over = 0.0
        elif self.state == TRACKING:
            over = lag > c.tether or lag_a > c.tether_angle or not reachable
            self._over = self._over + dt if over else 0.0
            if self._over > c.tether_time or lag > 2 * c.tether or lag_a > 2 * c.tether_angle:
                self.state = LOST
        if self.state == TRACKING:
            # toward the wish at bounded speed
            d = wish_pos - self._cmd_pos
            n = float(np.linalg.norm(d))
            self._cmd_pos = self._cmd_pos + d * min(1.0, c.max_speed * dt / max(n, 1e-12))
            rv = P.quat_to_rotvec(P.quat_mul(wish_quat, P.quat_conj(self._cmd_quat)))
            a = float(np.linalg.norm(rv))
            self._cmd_quat = P.quat_mul(P.quat_from_rotvec(rv * min(1.0, c.max_angular_speed * dt / max(a, 1e-12))),
                                        self._cmd_quat)
            # bounded lead over the measured tool
            d = self._cmd_pos - tool_pos
            n = float(np.linalg.norm(d))
            if n > c.max_lead:
                self._cmd_pos = tool_pos + d * (c.max_lead / n)
            rv = P.quat_to_rotvec(P.quat_mul(self._cmd_quat, P.quat_conj(tool_quat)))
            a = float(np.linalg.norm(rv))
            if a > c.max_angular_lead:
                self._cmd_quat = P.quat_mul(P.quat_from_rotvec(rv * (c.max_angular_lead / a)), tool_quat)
            self._cmd_quat = self._cmd_quat / np.linalg.norm(self._cmd_quat)
        else:                                                    # LOST: hold where the tool is
            self._cmd_pos, self._cmd_quat = tool_pos.copy(), tool_quat.copy()
        return TeleopOut(self.state, self._cmd_pos.copy(), self._cmd_quat.copy(), wish_pos, wish_quat, lag, lag_a,
                         self.state == LOST, self.state == TRACKING and prev != TRACKING,
                         self.state == LOST and prev == TRACKING)


class RateGripper:
    """Gripper closure 0 (open) .. 1 (closed), moved at a bounded rate: the trigger closes (analogue: deeper =
    faster), a button opens."""

    def __init__(self, close_time: float = 2.0, open_time: float = 1.5, closure: float = 0.0):
        self.close_rate, self.open_rate, self.closure = 1.0 / close_time, 1.0 / open_time, float(closure)

    def update(self, dt: float, trigger: float, open_button: bool) -> float:
        rate = self.close_rate * float(np.clip((trigger - 0.05) / 0.95, 0.0, 1.0))
        if open_button:
            rate = -self.open_rate
        self.closure = float(np.clip(self.closure + rate * dt, 0.0, 1.0))
        return self.closure


class ContactHaptics:
    """Force [N] on a tool -> vibration. A new contact gives a short strong tick; while the contact lasts the
    amplitude follows the force. Call `update` every step; it returns (amplitude 0..1, duration [s]) or None."""

    def __init__(self, f_min: float = 0.3, f_max: float = 15.0, tick: float = 0.8, floor: float = 0.08,
                 gain: float = 0.6, min_interval: float = 0.012):
        self.f_min, self.f_max, self.tick, self.floor, self.gain, self.min_interval = f_min, f_max, tick, floor, gain, min_interval
        self._in_contact = False
        self._since = 1e9

    def update(self, dt: float, force: float):
        self._since += dt
        touching = force > self.f_min
        out = None
        if touching and not self._in_contact:
            out = (self.tick, 0.03)
            self._since = 0.0
        elif touching and self._since >= self.min_interval:
            a = self.floor + self.gain * min(1.0, (force - self.f_min) / (self.f_max - self.f_min))
            out = (float(a), max(2.5 * dt, 0.03))
            self._since = 0.0
        self._in_contact = touching
        return out

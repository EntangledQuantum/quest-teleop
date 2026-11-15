"""The control panel the headset shows: what is running, its state, and the actions the operator can trigger.

The server describes the panel; the client lays it out (wrist card, menu) in its own style and sends back the id of an
action when the operator presses its button. Nothing in it is task specific.

    panel = Panel(title="Pick and place", subtitle="simulator A, config B")
    panel.status("recording", "REC 00:12  take 3")
    panel.rows = [Row("RUN", "session-01", wrist=True), Row("SIM SPEED", "0.55x", style="caution")]
    panel.actions = [Action(1, "Stop recording", hint="$Rs", style="primary", active=True),
                     Action(2, "Reset the scene", hint="hold", style="danger", confirm=True)]
    panel.legend = [("$R GRIP", "move the gripper"), ("$R TRIGGER", "close slowly")]
    server.set_panel(panel)
    for action_id in server.pop_actions(): ...

Role tokens in any string are replaced on the headset, so the text stays right when the operator swaps hands:
$R / $L = "Right" / "Left" controller as held, $Rp / $Rs / $Lp / $Ls = that controller's primary / secondary button
(A, B, X, Y).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .protocol import pack_str

STYLES = {"normal": 0, "accent": 1, "caution": 2, "failure": 3}
STATUS = {"none": 0, "recording": 1, "caution": 2, "ok": 3}
ACTION_STYLES = {"normal": 0, "primary": 1, "danger": 2}


@dataclass
class Row:
    label: str
    value: str
    style: str = "normal"
    wrist: bool = False                # also on the wrist card


@dataclass
class Action:
    id: int
    label: str
    hint: str = ""                     # the shortcut, shown at the button's right edge
    style: str = "normal"
    enabled: bool = True
    confirm: bool = False              # the operator must hold the trigger on it
    active: bool = False               # drawn as switched on


@dataclass
class Panel:
    title: str = ""
    subtitle: str = ""
    host: str = ""                     # filled in by the server (computer name and address) when empty
    status_style: str = "none"
    status_text: str = ""
    rows: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    legend: list = field(default_factory=list)      # (key, text)

    def status(self, style: str, text: str) -> None:
        self.status_style, self.status_text = style, text

    def pack(self) -> bytes:
        out = [pack_str(self.title), pack_str(self.subtitle), pack_str(self.host),
               struct.pack("<B", STATUS[self.status_style]), pack_str(self.status_text), struct.pack("<B", len(self.rows))]
        for r in self.rows:
            out += [struct.pack("<B", STYLES[r.style] | (0x80 if r.wrist else 0)), pack_str(r.label), pack_str(r.value)]
        out.append(struct.pack("<B", len(self.actions)))
        for a in self.actions:
            flags = (1 if a.enabled else 0) | (2 if a.confirm else 0) | (4 if a.active else 0)
            out += [struct.pack("<HBB", a.id, ACTION_STYLES[a.style], flags), pack_str(a.label), pack_str(a.hint)]
        out.append(struct.pack("<B", len(self.legend)))
        for k, t in self.legend:
            out += [pack_str(k), pack_str(t)]
        return b"".join(out)

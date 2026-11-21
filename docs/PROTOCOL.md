# Quest Teleop protocol, version 1

Reference implementations: `server/quest_teleop/protocol.py` (Python) and `client/core/protocol.h` (C++).
All integers and floats are little-endian. Positions are metres, quaternions are `x y z w`, strings are
`u16 length` + UTF-8 bytes.

## Channels

| Channel | Port | Carries |
|---|---|---|
| TCP | 47800 (server listens) | handshake, scene definition (meshes, nodes), text cues, bye |
| UDP | 47800 (server), any (client) | controller input (client to server), state frames (server to client), discovery |
| UDP broadcast | 47801 | server beacons (optional way to find the server) |

The reliable channel only carries things that are sent once. Everything that repeats every frame goes over UDP as
absolute values, so a lost datagram never corrupts later frames and nothing is ever retransmitted late.

**TCP-only mode.** A client can set `HELLO.flags bit 0`; both sides then wrap their datagrams in `DGRAM` messages on
the TCP connection. This is for links that only forward TCP, e.g. USB with `adb reverse tcp:47800 tcp:47800`.

## Finding the server

1. The client broadcasts `DISCOVER` to `255.255.255.255:47800` once per second; the server answers `OFFER` to the
   sender's address.
2. The server also broadcasts `OFFER` to port 47801 once per second.
3. Or the client is given the address (headset: `adb shell am start ... --es server 192.168.1.20`).

## Session

```
client                                server
  | -- TCP connect ------------------> |
  | -- HELLO ------------------------> |
  | <------------------------ WELCOME  |   session id
  | <-- SCENE_BEGIN, MESH*, NODE*, SCENE_END   (full model, once)
  | -- SCENE_READY ------------------> |
  | == INPUT (UDP, every display frame) => |   poses, buttons, ack of the newest complete state frame
  | <= STATE (UDP, every sim step) ======= |   what changed since the acked frame, haptics, latency echo
  | <-- TEXT (when a cue changes)      |
```

The server learns the client's UDP address from its first `INPUT` datagram (matched by session id). A new
`SCENE_BEGIN` at any time replaces the scene.

## Frames and coordinate spaces

* **Scene space**: the simulator's world frame. `SCENE_BEGIN` says which axis is up and gives the bounds.
* **Stage space**: the room (OpenXR `STAGE`: y up, origin on the floor).
* **Placement**: where the client draws the scene in the room; the user changes it in the app (move, turn, scale).
  `p_stage = place_pos + place_scale * rotate(place_quat, p_scene)`.

Controller poses in `INPUT` are in stage space, together with the current placement, so the server can convert them
to scene space (`InputFrame.hand_in_scene`). A scene drawn at twice its size therefore halves the hand motion in
simulator units.

## Datagrams

Header (8 bytes): `u16 magic = 0x5451 ("QT")`, `u8 version = 1`, `u8 type`, `u32 session`.

| Type | Name | Payload |
|---|---|---|
| 1 | DISCOVER | none (session 0) |
| 2 | OFFER | `u16 tcp_port`, `str server_name` |
| 3 | INPUT | below (292 bytes with the header) |
| 4 | STATE | below (at most 1200 bytes with the header) |

### INPUT (client to server, every display frame)

```
u32 seq                 u64 client_time_us          u32 ack_frame     u16 ack_scene
u16 flags               bit0 edit mode (user is placing the scene: ignore the controls)
                        bit1 session focused (headset worn)   bit2 passthrough on
u32 latency_us          the client's last measured echo latency (0 = none yet)
f32 place_pos[3]        f32 place_quat[4]           f32 place_scale
f32 head_pos[3]         f32 head_quat[4]
2 x hand (0 = left, 1 = right), 100 bytes each:
    u16 flags           bit0 active  bit1 grip pose valid  bit2 aim pose valid  bit3 tracked
    u16 buttons         bit0 primary (X / A)  bit1 secondary (Y / B)  bit2 thumbstick click  bit3 menu
                        bit4 trigger touch  bit5 thumbstick touch  bit6 primary touch  bit7 secondary touch
                        bit8 thumbrest touch
    f32 trigger         f32 squeeze                 f32 stick[2]
    f32 grip_pos[3]     f32 grip_quat[4]            f32 aim_pos[3]    f32 aim_quat[4]
    f32 velocity[3]     f32 angular_velocity[3]     (grip, stage space)
```

`ack_frame` is the newest state frame of scene `ack_scene` of which the client received every fragment before any
fragment of a newer frame.

### STATE (server to client, every simulation step)

A frame is split into 1 to 255 datagrams. Each datagram is decodable alone.

```
u16 scene_id            u32 frame                   u8 frag_index     u8 frag_count
u64 server_time_us
u32 echo_input_seq      u64 echo_client_time_us     the INPUT sample this state results from
u32 echo_hold_us        time between receiving that input and sending this frame (simulation + wait)
2 x haptic (left, right): u16 seq, u8 amplitude (0..255), u8 reserved, u16 duration_ms, u16 frequency_hz
chunks until the end of the datagram
```

Chunk header: `u8 type`, `u8 reserved`, `u16 node`.

| Type | Name | Body |
|---|---|---|
| 1 | POSE | `u16 pos[3]`, `i16 quat3[3]`, `i16 dropped` |
| 2 | VERTS | `u16 first`, `u16 count`, `count x u16[3]` |
| 3 | APPEARANCE | `u8 rgba[4]`, `u16 node_flags`, `u16 reserved` |

* **Position quantisation**: `u16 = round((p - bounds_min) / (bounds_max - bounds_min) * 65535)` per axis. For nodes
  whose parent is the scene the bounds are the scene bounds (a 2 m scene gives 0.03 mm steps); for nodes attached
  to the head, a hand or the stage the bounds are -2 m .. +2 m.
* **Rotation quantisation** ("smallest three"): the quaternion is sign-flipped so that its largest-magnitude
  component is positive; that component's index (0..3 in xyzw order) is `dropped`; the other three, in xyzw order,
  are stored as `round(c * 32767 * sqrt(2))`. Error below 1e-4 rad.
* **VERTS** carries a run of a DEFORM node's vertices or a TUBE node's points.

**What a frame contains (the delta scheme).** The server remembers, for every pose, every vertex and every
appearance, the frame in which its quantised value last changed. A frame for a client contains every item that
changed after the frame that client last acknowledged. So:

* things that do not move cost nothing;
* a lost datagram is repaired by the next frame automatically (the ack does not advance, so the items are sent
  again with their current values), without retransmission timers;
* values are always absolute, never differences, so there is no error accumulation and no dependency chain.

A client drops datagrams of frames older than the newest frame it has seen, and applies everything else
immediately (also from incomplete frames).

**Haptics.** The haptic fields are state, not events: they travel in every datagram. The client plays a vibration
when `seq` differs from the last one it played. Amplitude 0 stops the vibration. Quest 2 controllers ignore the
frequency.

**Latency echo.** `now - echo_client_time_us` on the client is the time from sampling the controllers to having the
resulting simulator state in hand; the client reports it back in `INPUT.latency_us`. It excludes the display's own
scan-out (about one refresh).

## TCP messages

Framing: `u32 length`, `u16 type`, payload; `length` counts the type and the payload.

| Type | Name | Direction | Payload |
|---|---|---|---|
| 1 | HELLO | C to S | `u16 magic`, `u16 version`, `u16 flags` (bit0 TCP only), `f32 display_hz`, `str client_name`, `str device` |
| 2 | WELCOME | S to C | `u16 version`, `u16 flags`, `u32 session`, `u16 udp_port`, `f32 state_hz`, `str server_name` |
| 3 | SCENE_BEGIN | S to C | `u16 scene_id`, `u8 up_axis` (0 x, 1 y, 2 z), `u8 reserved`, `f32 bounds_min[3]`, `f32 bounds_max[3]`, `f32 focus_center[3]`, `f32 focus_radius`, `f32 view_dir[3]`, `u16 mesh_count`, `u16 node_count`, `str name` |
| 4 | MESH | S to C | below |
| 5 | NODE | S to C | below |
| 6 | SCENE_END | S to C | `u16 scene_id` |
| 7 | SCENE_READY | C to S | `u16 scene_id` |
| 8 | TEXT | S to C | `u8 slot`, `u8 anchor`, `u16 reserved`, `u8 rgba[4]`, `f32 size`, `f32 pos[3]`, `u32 ttl_ms`, `str text` |
| 9 | DGRAM | both | one datagram including its header (TCP-only mode) |
| 10 | PING | C to S | `u64 t` |
| 11 | PONG | S to C | `u64 t` (echo), `u64 server_time_us` |
| 12 | BYE | both | `str reason` (optional) |
| 13 | PANEL | S to C | below |
| 14 | ACTION | C to S | `u16 action_id`: the operator pressed that action's button |

`focus_center` / `focus_radius` describe the region the operator works in; the client uses them for the first
placement (the region is put in front of the user at a comfortable size). `view_dir` is the horizontal direction,
in scene coordinates, the operator should look along at the start (zero: no preference).

### MESH

```
u16 mesh_id             u16 flags       bit0 normals  bit1 vertex colours  bit2 32-bit indices  bit3 double sided
u32 vertex_count        u32 index_count
f32 aabb_min[3]         f32 aabb_max[3]
u32 raw_size
zlib-deflated block of raw_size bytes:
    u16 position[3] x vertex_count          quantised inside the AABB
    i8  normal[4]   x vertex_count          if bit0 (xyz * 127, 4th byte unused)
    u8  rgba[4]     x vertex_count          if bit1
    u16 or u32 index x index_count          triangles
```

Without normals the client shades with the triangle normals.

### NODE

```
u16 node_id             u8 kind         1 MESH (rigid)  2 DEFORM (streamed vertices)  3 TUBE (streamed polyline)
u8 parent               0 scene  1 stage  2 head  3 left grip  4 right grip  5 left aim  6 right aim
u16 mesh_id (0xFFFF none)
u16 flags               bit0 visible  bit1 blend (use alpha)  bit2 unlit  bit3 on top (no depth test)  bit4 flat
                        bit5 highlight (accent rim)
u8 rgba[4]              multiplies the mesh colours
f32 pos[3]              f32 quat[4]     f32 scale[3]
f32 param               TUBE: radius
u16 point_count         TUBE: number of points, followed by f32[3] x point_count
str name
```

Node ids are dense, 0 .. node_count - 1, and messages arrive in id order. Nodes attached to a hand or the head move
with it on the client without any network delay: use them for cues that must stick to the controller.

### TEXT

`anchor` 0: head-locked panel 1 m in front of the eyes, `pos` is the offset on it. `anchor` 1 + parent: at `pos` in
that frame, turned toward the viewer. `size` is the line height in metres. `ttl_ms` 0: until the slot is replaced;
an empty string clears the slot. Lines are separated by `\n`.

### PANEL

The server's control panel: what is running, its state, and the actions the operator can trigger. The client lays it
out in its own style (a card on the wrist, a menu) and answers with ACTION. Send it again whenever it changes.

```
str title               str subtitle            str host ("computer name  address")
u8 status_style         0 none  1 recording  2 caution  3 ok          str status_text
u8 row_count, each:     u8 style (0 normal, 1 accent, 2 caution, 3 failure; bit 7: also on the wrist card),
                        str label, str value
u8 action_count, each:  u16 id, u8 style (0 normal, 1 primary, 2 danger),
                        u8 flags (bit 0 enabled, bit 1 hold to confirm, bit 2 active), str label, str hint
u8 legend_count, each:  str key, str text
```

Role tokens in any of these strings are replaced by the client, so the text stays right when the user swaps hands
in the settings: `$R` / `$L` = "Right" / "Left" (the controller in that role), `$Rp` `$Rs` `$Lp` `$Ls` = that
controller's primary / secondary button (A, B, X, Y). With "left-handed" on, the client also swaps the two hands in
INPUT and the two haptic channels, so the server needs no special handling.

Node flag bit 5 (`F_HIGHLIGHT`, 32) draws a node with an accent rim (e.g. the tool that is following the hand).

## Cues are ordinary nodes

The protocol has no special cue messages. The "go back to the robot" cue (`server/quest_teleop/cues.py`) is three
nodes that the server shows and hides (a see-through shell at the robot's real tool pose, a marker at the pose the
hand asks for, a line between them) plus one TEXT. Any other cue can be built the same way.

## Size of the traffic

* INPUT: 292 bytes x display rate (72 Hz: 168 kbit/s).
* STATE: 56 bytes of headers per datagram, 18 bytes per moving rigid body, 6 bytes per moving vertex or tube point,
  8 extra bytes per run of vertices.

# Measurements

This page is [`docs/MEASUREMENTS.md`](https://github.com/EntangledQuantum/quest-teleop/blob/main/docs/MEASUREMENTS.md) from the repository.

"Latency" is the protocol's echo latency: from the moment the client samples the controllers to the moment it holds
the simulator state that resulted from that sample. The client measures it and reports it back every frame; the
status page splits it into its parts. It includes the wait for the next simulation step and the step itself, and
excludes the display's own scan-out (about one refresh, 13.9 ms at 72 Hz).

## Loopback, bundled demo (no headset)

`quest_teleop.probe` against `quest_teleop.demo`. Scene: 17 nodes, 7 meshes, one 196-vertex cloth, one 40-point
rope. Definition: 10 kB, loaded in 1 ms. Client input at 72 Hz.

| Case | State rate | Down | Up | Datagrams / frame | Latency median / p95 / max | Server hold median |
|---|---|---|---|---|---|---|
| right hand moving, UDP | 60 Hz | 0.23 Mbit/s | 168 kbit/s | 1.07 | 8.4 / 14.6 / 14.9 ms | 8.2 ms |
| cloth and rope settling, hands still | 60 Hz | 0.32 Mbit/s | 168 kbit/s | 1.01 | 8.4 / 14.7 / 15.3 ms | 8.3 ms |
| right hand moving, UDP | 120 Hz | 0.28 Mbit/s | 168 kbit/s | 1.00 | 8.0 / 12.6 / 13.4 ms | 7.9 ms |
| right hand moving, TCP-only mode | 60 Hz | 0.35 Mbit/s | 168 kbit/s | 1.12 | 6.4 / 12.6 / 16.2 ms | 6.2 ms |

No frame was lost in any run. Almost all of the latency is the server hold: an input waits on average half a
simulation period for the next step, plus the step time. The network and the encoding add about 0.1 ms on loopback.
With 30 % of the state datagrams dropped on purpose the client's state still converges to the server's, without
retransmission (`tests/test_loopback.py`).

## Loopback, a larger scene

A robot arm with a gripper, rigid parts, two deformable meshes and a 268-node rope from a GPU physics simulator:
40 nodes, 33 meshes, 81,901 triangles, 2,051 streamed points.

| | |
|---|---|
| Scene definition | 689 kB on the wire, decoded by the Python probe in 5 ms |
| State frame while things move | 3.6 kB in 3.7 datagrams (1.5 Mbit/s at 50 Hz), with a 0.05 mm vertex dead band |
| Server cost per frame (quantise, diff, encode, send) plus the application's own logic | 0.8 ms median |

## Quest 2

Quest 2, system software v209 (Android 14), 72 Hz, passthrough on. The headset app ran on the first install: OpenXR
session, 1440 x 1584 sRGB swapchains with 4x MSAA, Touch controller bindings, passthrough layer; 72 / 72 frames per
second with no stale frames, application GPU time 6.4 ms (before shadows were added; not re-measured since).

| Scene | Link | Latency median / p95 / max | Down | Up |
|---|---|---|---|---|
| demo, 60 Hz | USB (`adb reverse`, TCP tunnel) | 13.9 / 27.9 / 28.2 ms | 0.35-0.6 Mbit/s moving | 168 kbit/s |
| demo, 60 Hz | Wi-Fi (UDP), computer also on Wi-Fi | 14.1 / 28.2 / 30-42 ms | same | 168 kbit/s |
| larger scene, simulator at about half real time (26 steps/s) | USB | 56 / 56-70 ms | 0.1-0.6 Mbit/s | 168 kbit/s |
| larger scene, same | Wi-Fi (UDP) | 56 / 69 ms | 0.1 Mbit/s idle | 168 kbit/s |

Latency on the headset is quantised to display frames (13.9 ms). With a slow simulator the latency is the simulator
step plus one to two display frames. Wi-Fi costs nothing at the median; now and then a frame arrives one or two
display frames late.

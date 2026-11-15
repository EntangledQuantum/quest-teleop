# FAQ

## Which headsets work?

It is built and measured on a **Meta Quest 2**. The app is plain OpenXR with Touch controller bindings, and its
manifest lists Quest 2, Quest Pro, Quest 3 and Quest 3S as supported devices; only the Quest 2 has been measured
([Measurements](measurements.md)).

## Do I need Unity, Unreal, Android Studio or Gradle?

No. The headset app is about 1 MB of C++ (OpenXR, EGL, OpenGL ES 3, one `NativeActivity`, no Java), built by two
shell scripts with the NDK, CMake and the Android build tools.

## Does it need Quest Link, Air Link or a PC VR runtime?

No. The app runs standalone on the headset and talks to your Python program over the local network or a USB
cable. Turn "Link auto connect" off so the headset does not start Link when the cable is plugged in.

## Is the Python library tied to a simulator or robot?

No. It streams meshes, poses, deforming meshes and polylines that you set every step, and gives you controller
poses and buttons. The demo uses a small numpy mock world; any simulator or robot driver that runs in, or can talk
to, a Python process works.

## Which operating systems are supported?

The build scripts (`android/setup_toolchain.sh`, `android/build_apk.sh`) and the desktop client are for Linux. The
Python library uses only the standard library and numpy; these pages assume Linux and do not cover other systems.

## How do I install the Python library?

From GitHub: `pip install git+https://github.com/EntangledQuantum/quest-teleop`
([Install](install.md#1-the-python-library)).

## Wi-Fi or USB?

Both work. On a Quest 2 the median latency is the same (about 14 ms with the demo); Wi-Fi occasionally delivers a
frame one or two display frames late. USB (`adb reverse`) needs no firewall change and no shared network.

## How much bandwidth does it use?

Controller input is 292 bytes per display frame (168 kbit/s at 72 Hz). The state stream sends only what changed:
the demo uses 0.2 to 0.6 Mbit/s; a scene with 82,000 triangles and 2,000 streamed points about 1.5 Mbit/s at 50 Hz.

## Can several headsets connect at once?

Several clients can connect and all of them see the scene. The input of one of them drives the simulator: of those
that are sending input, the one connected the longest (`TeleopServer.controller()`).

## Can I write my own client or server?

Yes. The [protocol](protocol.md) is documented byte by byte, with reference implementations in Python
(`server/quest_teleop/protocol.py`) and C++ (`client/core/protocol.h`), and a headless Python client
(`quest_teleop.probe`).

## Does it support hand tracking?

No. It uses the Touch controllers; nothing needs hand tracking or standing.

## What is recorded, and where?

Tool poses, gripper openings and states, controllers, time, and any per-step data you add, one `.npz` per take in
`~/teleop_recordings` (or `$TELEOP_DATA_DIR`). See [Recording takes](recording.md).

## Is anything sent to the internet?

No. The server, the status page and the headset app talk only on your local network or USB cable. The status page
loads nothing from the internet. Only `android/setup_toolchain.sh` downloads, from `dl.google.com` and
`github.com/KhronosGroup`.

## Licence?

MIT. Third-party parts: the Khronos OpenXR loader (Apache-2.0 / MIT, downloaded at build time and shipped in the
APK), the Android NDK's `native_app_glue` (Apache-2.0), zlib, and the fonts Host Grotesk and Martian Mono (SIL Open
Font License 1.1).

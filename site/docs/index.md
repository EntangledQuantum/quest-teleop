# Quest Teleop documentation

Quest Teleop lets you teleoperate a simulator or a robot from a Meta Quest headset without a game engine. The
headset app uses standard OpenXR, so it targets Meta Quest headsets in general; it is tested on a Quest 2, and notes
that only apply to the Quest 2 say so. Quest Teleop has four parts:

* **Headset app** (`client/quest`): native OpenXR + OpenGL ES 3 in C++, one `NativeActivity`, no Java, no Gradle.
  It receives a 3D scene from a server on your network, draws it in the room, sends both controllers back every
  display frame, plays vibration, and shows the server's cues, status and buttons.
* **Python library** (`quest_teleop`, numpy only): stream any 3D scene, read the controllers, send haptics and text
  cues, show a control panel with your own actions, map a hand to a robot tool safely, record takes, and serve a
  local status page.
* **Desktop client** (`client/desktop`): the same C++ core in a window, driven by mouse and keyboard, to try a server
  without a headset.
* **Demo** (`quest-teleop-demo`): two grippers, a cube, a rope and a cloth, using every feature above.

## Pages

Read them in this order the first time.

1. [Install](install.md): the Python library with pip; the Android toolchain, building and installing the APK; the
   desktop client.
2. [Headset setup](quest-setup.md): developer mode, USB debugging, the udev rule on Linux, firewall ports, USB link
   with `adb reverse`.
3. [Quick start](quick-start.md): run the demo, connect the headset or the desktop client, the controls.
4. [Use it with your own simulator](library.md): the server API, streaming a scene, input, haptics, cues, the panel
   and actions, safe hand-to-tool mapping.
5. [Recording takes](recording.md): `TakeRecorder`, the file layout, the arrays, loading takes.
6. [Status page and trajectory viewer](status-page.md): the local web page, its data callback and endpoints.
7. [Protocol](protocol.md): the wire format, for writing another client or server.
8. [Measurements](measurements.md): latency and bandwidth on loopback and on a Quest 2.
9. [Troubleshooting](troubleshooting.md): the headset does not appear in adb, the app does not find the server,
   and other problems.
10. [FAQ](faq.md).

## For AI agents

Every page is published twice: as HTML (`install.html`) and as plain Markdown (`install.md`) at the same path.
[`/llms.txt`](../llms.txt) lists all pages with their Markdown links, and [`/llms-full.txt`](../llms-full.txt) has
the whole documentation in one file. Steps that need a person (putting on the headset, tapping "Allow", enabling
developer mode in the phone app) or root (udev rule, firewall) are marked as such in the pages.

## Source

<https://github.com/EntangledQuantum/quest-teleop>, MIT licence. These pages document version 0.2.0.

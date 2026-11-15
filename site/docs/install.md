# Install

There are up to three things to install: the Python library (always), the headset app (once per headset), and the
desktop client (optional, for trying a server without a headset).

## Requirements

| What | Needed for |
|---|---|
| Python 3.10 or newer, pip | the library, the demo, the probe |
| numpy | the library (installed automatically) |
| git | cloning the repository to build the headset app or the desktop client |
| Linux on x86-64 | the build scripts (`android/setup_toolchain.sh` downloads the Linux Android tools) |
| cmake 3.18 or newer, make, curl, unzip, zip | building the APK |
| a Java runtime, version 17 or newer | `sdkmanager`, `apksigner` and `keytool` while building the APK |
| about 2.5 GB of disk | the Android SDK tools, the NDK and the OpenXR loader |
| a Meta Quest headset (tested on a Quest 2) in developer mode | running the app |
| a USB-C data cable | installing the app on the headset |

On Debian or Ubuntu, for example:

```bash
sudo apt install git python3-pip cmake make curl unzip zip openjdk-17-jdk-headless
```

## 1. The Python library

```bash
pip install git+https://github.com/EntangledQuantum/quest-teleop
```

Or from a clone:

```bash
git clone https://github.com/EntangledQuantum/quest-teleop.git
cd quest-teleop
pip install .            # or: pip install -e .   (editable, for working on the library)
```

This installs the `quest_teleop` package (its only dependency is numpy) and two commands:

| Command | What it does |
|---|---|
| `quest-teleop-demo` | the bundled demo server with a mock world, the status page and recording ([Quick start](quick-start.md)) |
| `quest-teleop-probe` | a headless Python client: checks a server and measures latency and bandwidth |

Check it:

```bash
python -c "import quest_teleop; print(quest_teleop.__version__)"     # 0.2.0
quest-teleop-demo --help
```

## 2. The headset app

The app is built from the repository with two scripts. No Android Studio, no Gradle, no Java sources: NDK clang
through CMake builds one shared library, then `aapt2`, `zip`, `zipalign` and `apksigner` package and sign it with a
debug key.

### 2.1 Install the Android toolchain (once)

```bash
git clone https://github.com/EntangledQuantum/quest-teleop.git     # if you have not cloned it yet
cd quest-teleop
android/setup_toolchain.sh
```

It installs into `~/android-sdk` (set `ANDROID_HOME` to choose another folder), about 2.4 GB:

| Component | Version |
|---|---|
| Android command-line tools | 11076708 |
| platform-tools (`adb`) | latest |
| build-tools (`aapt2`, `zipalign`, `apksigner`) | 34.0.0 |
| platform (`android.jar`) | android-32 (the APK targets API 32; its minimum is API 29) |
| NDK | 27.2.12479018 (r27c) |
| Khronos OpenXR loader for Android | 1.1.65 |

It downloads only from `dl.google.com` and `github.com/KhronosGroup`. **Running it accepts the Android SDK licence
agreements** (`sdkmanager --licenses`); an agent should tell the user this before running it. Running it again is
safe: finished parts are skipped.

At the end it prints where `adb` is: `~/android-sdk/platform-tools/adb`. The other pages write plain `adb`; either
put that folder on your `PATH` or use the full path:

```bash
export PATH="$HOME/android-sdk/platform-tools:$PATH"
```

### 2.2 Build the APK

```bash
android/build_apk.sh            # builds build/quest-teleop.apk (about 1 MB)
```

The APK is signed with the standard Android debug key in `~/.android/debug.keystore` (created with `keytool` if it
is missing). That is all a sideloaded developer build needs.

### 2.3 Install it on the headset

The headset must be in developer mode, connected by USB, and listed by `adb devices` as `device`: see
[Headset setup](quest-setup.md) first. Then:

```bash
android/build_apk.sh install    # builds, runs `adb install -r build/quest-teleop.apk`, and starts the app
```

To start the app with a fixed server address instead of finding the server by itself:

```bash
SERVER=192.168.1.20 android/build_apk.sh install
```

An APK that is already built installs with plain adb:

```bash
adb install -r build/quest-teleop.apk
```

In the headset the app is under **Library**, filter **Unknown sources**; its label there is **Quest Teleop**. Its package name is
`com.entangledquantum.questteleop`.

## 3. The desktop client (optional)

The same C++ core as the headset app, in an X11 window. Needs a C++17 compiler and the X11, EGL, OpenGL ES and zlib
development files:

```bash
sudo apt install g++ cmake make libx11-dev libegl-dev libgles-dev zlib1g-dev     # Debian / Ubuntu
cmake -S client -B build/desktop && cmake --build build/desktop -j
build/desktop/qt_desktop                     # finds the server by broadcast
build/desktop/qt_desktop --server 127.0.0.1  # or: a given address, HOST[:PORT]
```

Options: `--server HOST[:PORT]`, `--tcp-only` (tunnel everything through TCP), `--size WxH`. The controls are on the
[Quick start](quick-start.md#4-desktop-client-controls) page.

## 4. Run the tests (optional)

In a clone:

```bash
pip install pytest
python -m pytest -q
```

They cover protocol round trips, the server and the probe over loopback, recovery from packet loss, recording and
the status page.

## Uninstall

```bash
pip uninstall quest-teleop
adb uninstall com.entangledquantum.questteleop
```

The toolchain is the folder `~/android-sdk` (or your `ANDROID_HOME`); delete it by hand only if nothing else uses
it.

Next: [Headset setup](quest-setup.md).

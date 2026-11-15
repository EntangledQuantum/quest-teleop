# Troubleshooting

Problems by symptom. The app's log is `adb logcat -s quest-teleop`; the server prints what it does with the prefix
`[quest-teleop]`.

## The headset does not appear in `adb devices`

* **Nothing listed.** Put the headset on: there is a **"USB detected"** notification to tap and allow. Until it is
  accepted the headset is not visible to adb. If there is no notification, try another cable (many USB-C cables only
  charge) or another port, and check that developer mode is on ([Headset setup](quest-setup.md#1-developer-mode-once-person)).
* **`unauthorized`.** Accept **"Allow USB debugging"** in the headset, with "Always allow from this computer" ticked.
  If the prompt does not come back: `adb kill-server`, replug, put the headset on.
* **`no permissions` (Linux).** Add the udev rule for vendor id `2833`
  ([Headset setup](quest-setup.md#udev-rule-on-linux-root)), then replug.
* **The headset starts Quest Link when plugged in.** Turn "Link auto connect" (or "Turn on Link") off.
* **`adb: command not found`.** It is in `~/android-sdk/platform-tools/`; add that to `PATH`.

## Building the APK fails

* **`missing ...: run android/setup_toolchain.sh`.** The toolchain is not installed, or it is in a different
  folder: set `ANDROID_HOME` to the same value for both scripts.
* **`sdkmanager` or `apksigner` fails with a Java error.** Install a Java runtime, version 17 or newer.
* **CMake cannot find a build program.** Install `make`.

## The app does not find the server (Wi-Fi)

The app shows "Looking for a server on this network". Check, in this order:

1. The server is running and printed `listening on 0.0.0.0:47800`.
2. The computer's firewall allows **TCP 47800 and UDP 47800-47801** ([Headset setup](quest-setup.md#5-network-wi-fi)).
   This is the most common cause.
3. Headset and computer are on the same network: not a guest network, no client / AP isolation, no VPN on the
   computer that captures the local traffic.
4. Some routers drop broadcasts. Give the address explicitly:
   `adb shell am start -n com.entangledquantum.questteleop/android.app.NativeActivity --es server <computer's address>`
   (the server prints its addresses at start).
5. Or use the cable: `adb reverse tcp:47800 tcp:47800` ([USB link](quest-setup.md#6-network-usb-cable-instead-of-wi-fi)).

## The app does not find the server (USB)

* Run `adb reverse tcp:47800 tcp:47800` again: it is lost when the cable is replugged or the adb server restarts.
  `adb reverse --list` shows the active forwards.
* If the server uses another port, forward that port: `adb reverse tcp:<port> tcp:<port>`, and start the app with
  `--es server 127.0.0.1:<port>`.

## Connected, but nothing moves

* `get_input()` returns `None` while the headset is not worn (the app sends no input without focus) and for 0.5 s
  after the last sample.
* While the headset menu is open, `InputFrame.edit_mode` is `True`: your code should ignore the controls then (the
  demo does). Close the menu with the left menu button.
* `Hand.active` is `False` while a controller is not tracked (asleep, out of view).
* Positions outside the scene bounds are clamped: make `bounds_min` / `bounds_max` contain everything that moves.
* Call `publish()` every step; nothing is sent otherwise.

## The scene is too big, too small or in the wrong place

Give `focus_center`, `focus_radius` (and `view_dir`) to `Scene`: the headset uses them to place and size the working
area. In the headset, open the menu and use the grips and sticks to place and scale it, or press "Reset placement".

## High latency

* Pass the input to `publish(echo=inp)` and call `note_timing`, then look at the status page's latency breakdown.
* "Wait for the step" and "simulator step" are your loop: with a slow simulator the latency is about the step time
  plus one to two display frames ([Measurements](measurements.md)).
* On Wi-Fi, use 5 GHz and keep the computer on a cable to the router if you can, or use the USB link.

## The status page does not open

* It listens on `127.0.0.1:8800` by default: open it on the same computer. For another machine, start it with
  `host="0.0.0.0"` (it has no login).
* Port 8800 already in use: `StatusPage(srv, port=8801)` or `quest-teleop-demo --status-port 8801`.
* The Recordings section is empty: pass `recordings_dir=rec.root`.

## The desktop client does not start

* `no X display: use --offscreen`: it needs an X11 display (`DISPLAY` set).
* Build errors about X11, EGL or zlib: install the development packages
  ([Install](install.md#3-the-desktop-client-optional)).

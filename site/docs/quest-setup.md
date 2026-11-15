# Headset setup

What the headset needs before the app can be installed and reach your computer. These steps are written for and
tested on a **Quest 2**. Other Meta Quest headsets use the same developer mode, USB debugging and network setup;
menu names can differ between headsets and system versions. Steps marked **(person)** need
someone with the headset or the phone; steps marked **(root)** need `sudo` on the computer.

## 1. Developer mode (once) (person)

1. In the **Meta Horizon** phone app, logged in with the headset's account, create or join a developer organisation
   at <https://developers.meta.com/horizon/manage/organizations/create/> and verify the account.
2. In the phone app: **Menu**, **Devices**, the headset, **Headset settings**, **Developer mode**: on.
3. Reboot the headset.

## 2. Connect by USB (person)

1. Connect the headset to the computer with a USB-C **data** cable (some cables only charge) and put the headset on.
2. A **"USB detected"** notification appears. Tap it and **allow** access. Until this is accepted the headset does
   not show up in `adb devices` at all.
3. An **"Allow USB debugging"** prompt appears. Tick **"Always allow from this computer"** and accept.
4. Turn **"Link auto connect"** off (on some system versions it is called **"Turn on Link"**). Otherwise the headset
   tries to start Quest Link when the cable is plugged in, instead of staying in USB debugging.

## 3. Check that adb sees the headset

```bash
adb devices          # adb is ~/android-sdk/platform-tools/adb after android/setup_toolchain.sh
```

| `adb devices` shows | Meaning and fix |
|---|---|
| `1WMHH... device` | ready |
| nothing | the cable is charge-only, or "USB detected" was not allowed: replug and tap the notification in the headset |
| `unauthorized` | accept "Allow USB debugging" in the headset (put it on) |
| `no permissions` (Linux) | add the udev rule below, then replug |

### udev rule on Linux (root)

Gives your user access to the headset's USB device (Meta's USB vendor id `2833`):

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="2833", MODE="0666", GROUP="plugdev"' | sudo tee /etc/udev/rules.d/51-oculus.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then unplug and replug the cable and run `adb devices` again.

## 4. Install the app

```bash
android/build_apk.sh install
```

See [Install](install.md#2-the-headset-app). The app is in the headset under **Library**, filter **Unknown
sources**, labelled **Quest Teleop**.

## 5. Network: Wi-Fi

The app finds the server by itself: it broadcasts a discovery request and listens for the server's beacons.

* Put the headset and the computer on the **same router**. Use 5 GHz for the headset if you can. A guest network or
  "client isolation" / "AP isolation" blocks the headset from reaching the computer.
* Open these ports in the computer's firewall **(root)**:

| Port | Protocol | Used for |
|---|---|---|
| 47800 | TCP | handshake, scene, panel, text cues |
| 47800 | UDP | controller input, state frames, discovery |
| 47801 | UDP | server beacons (the server announces itself once per second) |

With ufw:

```bash
sudo ufw allow 47800/tcp && sudo ufw allow 47800:47801/udp
```

With firewalld:

```bash
sudo firewall-cmd --add-port=47800/tcp --add-port=47800-47801/udp
# to keep it after a reboot:
sudo firewall-cmd --permanent --add-port=47800/tcp --add-port=47800-47801/udp && sudo firewall-cmd --reload
```

If the server runs on another port (`TeleopServer(port=...)`, `quest-teleop-demo --port ...`), open that port for
TCP and UDP instead of 47800; the beacon port stays 47801.

## 6. Network: USB cable instead of Wi-Fi

With the headset connected by USB:

```bash
adb reverse tcp:47800 tcp:47800
```

The app then reaches the server on its own loopback address (it tries `127.0.0.1` first) and tunnels everything,
including the datagrams, through that one TCP connection. No firewall change is needed. Run the command again after
the cable is replugged or the adb server restarts. On a Quest 2 the median latency over USB and over Wi-Fi is the
same ([Measurements](measurements.md)).

## 7. Start and stop the app

From the headset: **Library**, **Unknown sources**, then the app labelled **Quest Teleop**. From the computer:

```bash
adb shell am start -n com.entangledquantum.questteleop/android.app.NativeActivity
adb shell am force-stop com.entangledquantum.questteleop
```

Launch options, added to `am start` as `--es NAME VALUE`:

| Option | Example | Effect |
|---|---|---|
| `server` | `--es server 192.168.1.20` or `--es server 192.168.1.20:47800` | connect to this address instead of discovering the server |
| `hz` | `--es hz 90` | display refresh rate (default: the system's, 72 Hz on a Quest 2) |
| `passthrough` | `--es passthrough 0` | start without passthrough (a floor grid is drawn instead). On a Quest 2 passthrough is grey-scale |

The app's log:

```bash
adb logcat -s quest-teleop
```

Next: [Quick start](quick-start.md).

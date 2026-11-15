#!/usr/bin/env bash
# Installs everything needed to build the Quest APK from the command line (no Android Studio, no Gradle):
#   Android command-line tools, platform-tools (adb), build-tools (aapt2, zipalign, apksigner), one platform
#   (android.jar), the NDK, and the Khronos OpenXR loader for Android.
# Sources: dl.google.com (Google) and github.com/KhronosGroup (Khronos) only.
# Running this accepts the Android SDK licence agreements (sdkmanager --licenses).
#
#   ANDROID_HOME   install prefix (default: ~/android-sdk)
set -euo pipefail

ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
CMDLINE_ZIP="commandlinetools-linux-11076708_latest.zip"
BUILD_TOOLS="34.0.0"
PLATFORM="android-32"            # Quest 2 system software is Android 12L (API 32)
NDK="27.2.12479018"              # r27c (LTS)
OPENXR="1.1.65"

mkdir -p "$ANDROID_HOME"
cd "$ANDROID_HOME"

if [ ! -x cmdline-tools/latest/bin/sdkmanager ]; then
    curl -fL --retry 3 -o "$CMDLINE_ZIP" "https://dl.google.com/android/repository/$CMDLINE_ZIP"
    rm -rf cmdline-tools/tmp && mkdir -p cmdline-tools/tmp
    unzip -q "$CMDLINE_ZIP" -d cmdline-tools/tmp
    rm -rf cmdline-tools/latest && mv cmdline-tools/tmp/cmdline-tools cmdline-tools/latest
    rm -rf cmdline-tools/tmp "$CMDLINE_ZIP"
fi
SDKM="cmdline-tools/latest/bin/sdkmanager --sdk_root=$ANDROID_HOME"
yes | $SDKM --licenses > /dev/null || true
$SDKM "platform-tools" "build-tools;$BUILD_TOOLS" "platforms;$PLATFORM" "ndk;$NDK" | grep -v "^\[" || true

if [ ! -f "openxr/$OPENXR/jni/arm64-v8a/libopenxr_loader.so" ]; then
    mkdir -p "openxr/$OPENXR"
    curl -fL --retry 3 -o "openxr/loader.aar" \
        "https://github.com/KhronosGroup/OpenXR-SDK-Source/releases/download/release-$OPENXR/openxr_loader_for_android-$OPENXR.aar"
    unzip -q -o "openxr/loader.aar" -d "openxr/$OPENXR"
    rm "openxr/loader.aar"
fi

echo "ANDROID_HOME=$ANDROID_HOME"
echo "adb:      $ANDROID_HOME/platform-tools/adb"
echo "NDK:      $ANDROID_HOME/ndk/$NDK"
echo "OpenXR:   $ANDROID_HOME/openxr/$OPENXR"

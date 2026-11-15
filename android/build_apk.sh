#!/usr/bin/env bash
# Builds build/quest-teleop.apk from the command line: NDK + CMake for the native library, then aapt2, zipalign and
# apksigner. No Gradle, no Java sources (the app is a NativeActivity).
#
#   android/setup_toolchain.sh      # once
#   android/build_apk.sh            # -> build/quest-teleop.apk
#   android/build_apk.sh install    # also: adb install -r, and start the app
#
# Signing uses the standard Android debug key (~/.android/debug.keystore, created if missing). That is all a
# sideloaded developer build needs.
set -euo pipefail
cd "$(dirname "$0")/.."

ANDROID_HOME="${ANDROID_HOME:-$HOME/android-sdk}"
BUILD_TOOLS="$ANDROID_HOME/build-tools/34.0.0"
PLATFORM_JAR="$ANDROID_HOME/platforms/android-32/android.jar"
NDK="$ANDROID_HOME/ndk/27.2.12479018"
OPENXR_DIR="$ANDROID_HOME/openxr/1.1.65"
ABI=arm64-v8a
PACKAGE=com.entangledquantum.questteleop
KEYSTORE="${KEYSTORE:-$HOME/.android/debug.keystore}"

for f in "$BUILD_TOOLS/aapt2" "$PLATFORM_JAR" "$NDK/build/cmake/android.toolchain.cmake" "$OPENXR_DIR/jni/$ABI/libopenxr_loader.so"; do
    [ -e "$f" ] || { echo "missing $f: run android/setup_toolchain.sh" >&2; exit 1; }
done

# 1. native library
cmake -S client -B build/android \
    -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" \
    -DANDROID_ABI=$ABI -DANDROID_PLATFORM=android-29 -DANDROID_STL=c++_static \
    -DOPENXR_DIR="$OPENXR_DIR" -DCMAKE_BUILD_TYPE=Release > /dev/null
cmake --build build/android -j "$(nproc)"

# 2. package: manifest (+ framework resources for the theme reference), then the two native libraries
STAGE=build/apk
rm -rf "$STAGE"
mkdir -p "$STAGE/lib/$ABI"
cp build/android/libquestteleop.so "$OPENXR_DIR/jni/$ABI/libopenxr_loader.so" "$STAGE/lib/$ABI/"
"$BUILD_TOOLS/aapt2" link -o "$STAGE/unaligned.apk" --manifest client/quest/AndroidManifest.xml -I "$PLATFORM_JAR"
(cd "$STAGE" && zip -q -r unaligned.apk lib)

# 3. align and sign
"$BUILD_TOOLS/zipalign" -f -p 4 "$STAGE/unaligned.apk" "$STAGE/aligned.apk"
if [ ! -f "$KEYSTORE" ]; then
    mkdir -p "$(dirname "$KEYSTORE")"
    keytool -genkeypair -keystore "$KEYSTORE" -storepass android -keypass android -alias androiddebugkey \
        -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Android Debug,O=Android,C=US" 2> /dev/null
fi
"$BUILD_TOOLS/apksigner" sign --ks "$KEYSTORE" --ks-pass pass:android --key-pass pass:android \
    --ks-key-alias androiddebugkey --out build/quest-teleop.apk "$STAGE/aligned.apk"
"$BUILD_TOOLS/apksigner" verify build/quest-teleop.apk
echo "built build/quest-teleop.apk ($(du -h build/quest-teleop.apk | cut -f1))"

if [ "${1:-}" = "install" ]; then
    ADB="$ANDROID_HOME/platform-tools/adb"
    "$ADB" install -r build/quest-teleop.apk
    "$ADB" shell am start -n "$PACKAGE/android.app.NativeActivity" ${SERVER:+--es server "$SERVER"}
fi

#!/usr/bin/env bash
# Build the USBdisplay receiver APK with only the local SDK (no Gradle, offline).
# Toolchain proven in 2.5: aapt2 + javac(--release 8) + d8 + zipalign + apksigner.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
SDK="${ANDROID_SDK:-/opt/android-sdk}"
BT_VERSION="${ANDROID_BUILD_TOOLS_VERSION:-37.0.0}"
BT="$SDK/build-tools/$BT_VERSION"
PLAT="${ANDROID_PLATFORM_JAR:-$SDK/platforms/android-33/android.jar}"
PKG="com.usbdisplay.receiver"
VERSION="0.95-Beta"
VERSION_CODE=95
BUILD="$ROOT/build"
APK_UNSIGNED="$BUILD/app-unsigned.apk"
APK_OUT="${1:-$ROOT/../android/USBdisplay-$VERSION.apk}"
KEYSTORE="${USBDISPLAY_KEYSTORE:-$ROOT/debug.keystore}"

for t in "$BT/aapt2" "$BT/d8" "$BT/zipalign" "$BT/apksigner" "$PLAT"; do
  test -e "$t" || { echo "missing: $t"; exit 1; }
done
command -v javac keytool >/dev/null || { echo "missing javac/keytool"; exit 1; }

rm -rf "$BUILD"
mkdir -p "$BUILD/classes" "$BUILD/dex"

if [ ! -f "$KEYSTORE" ]; then
  keytool -genkeypair -keystore "$KEYSTORE" -alias androiddebugkey \
    -keyalg RSA -keysize 2048 -validity 10950 \
    -storepass android -keypass android \
    -dname "CN=USBDisplay Debug, OU=dev, O=usbdisplay, L=lab, ST=lab, C=US" >/dev/null 2>&1
  echo "generated $KEYSTORE"
fi

# 1. icon resources + manifest -> base apk (UI itself is programmatic).
"$BT/aapt2" compile --dir "$ROOT/res" -o "$BUILD/resources.zip"
"$BT/aapt2" link -o "$APK_UNSIGNED" \
  -I "$PLAT" \
  --min-sdk-version 23 --target-sdk-version 33 \
  --version-code "$VERSION_CODE" --version-name "$VERSION" \
  --manifest "$ROOT/AndroidManifest.xml" \
  -R "$BUILD/resources.zip"

# 2. java -> classes
javac -Xlint:-options -encoding UTF-8 --release 8 -cp "$PLAT" -d "$BUILD/classes" \
  "$ROOT/src/com/usbdisplay/receiver/MainActivity.java" \
  "$ROOT/src/com/usbdisplay/receiver/UdspParser.java"

# 3. classes -> dex
"$BT/d8" --release --lib "$PLAT" --output "$BUILD/dex" "$BUILD/classes/com/usbdisplay/receiver/"*.class

# 4. dex into apk, align, sign
cp "$APK_UNSIGNED" "$BUILD/app-aligned.apk"
(cd "$BUILD/dex" && zip -q -X "$BUILD/app-aligned.apk" classes.dex)
"$BT/zipalign" -f 4 "$BUILD/app-aligned.apk" "$BUILD/app-zaligned.apk" >/dev/null
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass pass:android --key-pass pass:android \
  --out "$APK_OUT" "$BUILD/app-zaligned.apk" >/dev/null
"$BT/apksigner" verify "$APK_OUT" && echo "signed OK: $APK_OUT"
ls -l "$APK_OUT"

#!/usr/bin/env bash
# Build the agent-media companion APK without Gradle.
#
# Inherited from spikes/mediasession/build.sh, which is the one thing from the
# spike worth keeping. Deliberately toolchain-light: the app uses only platform
# APIs (no AndroidX, no org.json even), so there is nothing to resolve from
# Maven and no AGP to install -- red5 sits around 90% disk, and a Gradle/AGP
# setup would be several GB against a few hundred MB for this.
set -euo pipefail

SDK="${ANDROID_HOME:-$HOME/android-sdk}"
BT="$SDK/build-tools/35.0.0"
PLATFORM="$SDK/platforms/android-35/android.jar"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
KEYSTORE="${COMPANION_KEYSTORE:-$HERE/debug.keystore}"
APK="$OUT/agent-media-companion.apk"

for tool in "$BT/aapt2" "$BT/d8" "$BT/zipalign" "$BT/apksigner"; do
    [ -x "$tool" ] || { echo "missing $tool (set ANDROID_HOME)" >&2; exit 1; }
done

rm -rf "$OUT"; mkdir -p "$OUT/gen" "$OUT/classes"

# 1. resources -> R.java + a linked APK with no code yet
"$BT/aapt2" compile --dir "$HERE/res" -o "$OUT/res.zip"
# The build stamp is the commit this APK was built from, plus a mark when the
# tree was dirty. It is read back at runtime and published in /state, because
# "is the phone running the build I just made?" was answerable only by
# inference — and inference got it wrong on 2026-08-15, costing a round trip
# arguing with a fix that was never installed. Every install here is a sideload
# and a tap; the readout should say what landed.
STAMP="$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)"
git -C "$HERE" diff --quiet HEAD -- "$HERE" 2>/dev/null || STAMP="$STAMP+dirty"

"$BT/aapt2" link -o "$OUT/base.apk" \
    -I "$PLATFORM" \
    --manifest "$HERE/AndroidManifest.xml" \
    --java "$OUT/gen" \
    --version-name "$STAMP" --replace-version \
    --min-sdk-version 30 --target-sdk-version 35 \
    "$OUT/res.zip"

# 2. java -> classes
javac -source 17 -target 17 -nowarn -Xlint:-options \
    -classpath "$PLATFORM" \
    -d "$OUT/classes" \
    $(find "$HERE/src" "$OUT/gen" -name '*.java')

# 3. classes -> dex
"$BT/d8" --lib "$PLATFORM" --min-api 30 --output "$OUT" \
    $(find "$OUT/classes" -name '*.class')

# 4. dex into the apk, align, sign
cp "$OUT/base.apk" "$OUT/unsigned.apk"
(cd "$OUT" && zip -q unsigned.apk classes.dex)
"$BT/zipalign" -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"

# Keep this keystore: Android refuses to upgrade an installed app whose new APK
# is signed by a different key, and the only way out on the phone is uninstall
# (losing granted permissions) -- awkward when every install is a sideload.
if [ ! -f "$KEYSTORE" ]; then
    keytool -genkeypair -keystore "$KEYSTORE" -alias companion \
        -storepass android -keypass android \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=agent-media companion" >/dev/null 2>&1
fi

"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass pass:android \
    --key-pass pass:android --out "$APK" "$OUT/aligned.apk"

echo "built: $APK"

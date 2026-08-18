#!/usr/bin/env bash
# Build the MediaPlayer speed spike APK without Gradle.
#
# Built the same way as the companion -- aapt2, javac, d8 -- on purpose: the
# whole point of the spike is to find out whether that toolchain can stay. A
# spike built with Gradle would have answered a different question, and would
# have installed the several GB this project does not have room for.
set -euo pipefail

SDK="${ANDROID_HOME:-$HOME/android-sdk}"
BT="$SDK/build-tools/35.0.0"
PLATFORM="$SDK/platforms/android-35/android.jar"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
KEYSTORE="$HERE/debug.keystore"

rm -rf "$OUT"; mkdir -p "$OUT/gen" "$OUT/classes"

# 1. resources -> R.java + a linked APK with no code yet
"$BT/aapt2" compile --dir "$HERE/res" -o "$OUT/res.zip"
"$BT/aapt2" link -o "$OUT/base.apk" \
    -I "$PLATFORM" \
    --manifest "$HERE/AndroidManifest.xml" \
    --java "$OUT/gen" \
    --min-sdk-version 31 --target-sdk-version 35 \
    "$OUT/res.zip"

# 2. java -> classes
javac -source 17 -target 17 -nowarn \
    -classpath "$PLATFORM" \
    -d "$OUT/classes" \
    $(find "$HERE/src" "$OUT/gen" -name '*.java')

# 3. classes -> dex
"$BT/d8" --lib "$PLATFORM" --output "$OUT" \
    $(find "$OUT/classes" -name '*.class')

# 4. dex into the apk, align, sign
cp "$OUT/base.apk" "$OUT/unsigned.apk"
(cd "$OUT" && zip -q unsigned.apk classes.dex)
"$BT/zipalign" -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"

if [ ! -f "$KEYSTORE" ]; then
    keytool -genkeypair -keystore "$KEYSTORE" -alias spike \
        -storepass android -keypass android \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=agent-media spike" >/dev/null 2>&1
fi

"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass pass:android \
    --key-pass pass:android --out "$OUT/mediaplayer-speed-spike.apk" "$OUT/aligned.apk"

echo "built: $OUT/mediaplayer-speed-spike.apk"

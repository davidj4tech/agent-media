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

# The version *code* is what the package installer compares, and it was left at
# the manifest's 1 for every build ever made. `adb install -r` does not care, so
# nothing showed it -- until the tap path was the only one left (no adb without
# Wireless debugging, and no Wireless debugging away from a wifi worth trusting)
# and Files answered "this version is already installed" for an APK that was
# nine commits newer. The whole fallback deploy route was closed.
#
# Minutes since the epoch: monotonic, so every build installs over the last one;
# never colliding, so a rebuild of a dirty tree installs too, which is the loop
# this is for. It is an install ordinal and nothing else -- the identity of a
# build is the commit in the version NAME, which is what /state reports back.
VERSION_CODE="$(( $(date +%s) / 60 ))"

"$BT/aapt2" link -o "$OUT/base.apk" \
    -I "$PLATFORM" \
    --manifest "$HERE/AndroidManifest.xml" \
    --java "$OUT/gen" \
    --version-name "$STAMP" --version-code "$VERSION_CODE" --replace-version \
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
    # Generating one is right for a first build and a trap for every other:
    # the keystore is gitignored, so a build in a throwaway git worktree finds
    # no key, makes a fresh one, and produces an APK that every phone with the
    # app already on it refuses with "App not installed" -- hours later, in
    # someone else's hands, saying nothing about signatures. Say it here, where
    # the fix (COMPANION_KEYSTORE, or build in the main checkout) is one line.
    echo "build.sh: no keystore at $KEYSTORE -- generating a NEW signing key." >&2
    echo "  An APK signed with it CANNOT update an existing install: Android" >&2
    echo "  refuses a new key, and the only way out on the phone is uninstall." >&2
    echo "  Building in a git worktree? The keystore is gitignored, so it is" >&2
    echo "  not here. Point COMPANION_KEYSTORE at the real one instead:" >&2
    echo "    COMPANION_KEYSTORE=<main-checkout>/android/companion/debug.keystore" >&2
    keytool -genkeypair -keystore "$KEYSTORE" -alias companion \
        -storepass android -keypass android \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -dname "CN=agent-media companion" >/dev/null 2>&1
fi

"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass pass:android \
    --key-pass pass:android --out "$APK" "$OUT/aligned.apk"

# The signer, every time. Two APKs with the same name and the same DN can be
# signed by different keys -- that is exactly the case this build hit, and the
# digest is the only thing that tells them apart.
SIGNER="$("$BT/apksigner" verify --print-certs "$APK" 2>/dev/null \
    | sed -n 's/.*SHA-256 digest: //p' | head -1)"
echo "built: $APK ($STAMP, versionCode $VERSION_CODE, signer ${SIGNER:0:12})"

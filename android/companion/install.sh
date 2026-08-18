#!/usr/bin/env bash
# install.sh -- put the companion APK on a device. Builds first, so what lands
# is always what the tree says.
#
#   ./install.sh ftv          # adb over the tailnet: unattended install+upgrade
#   ./install.sh p8a          # scp to ~/storage/downloads; David taps it in Files
#   ./install.sh ftv --launch # ... and start MainActivity afterwards
#   ./install.sh ftv --no-build
#
# Why the two targets differ: p8a's adbd binds wlan0 only, so red5 cannot reach
# it and every phone install is a sideload by hand. ftv runs adbd on tcp 5555
# (Settings -> Developer options -> ADB/network debugging; red5's ~/.android/
# adbkey is authorised on the TV), so the TV can be updated without leaving the
# desk. If adb says `unauthorized`, the TV is showing the RSA dialog -- accept
# it there with "always allow", then re-run.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APK="$HERE/build/agent-media-companion.apk"
TARGET="${1:-}"
shift || true

BUILD=1
LAUNCH=0
for arg in "$@"; do
    case "$arg" in
        --no-build) BUILD=0 ;;
        --launch)   LAUNCH=1 ;;
        *) echo "install.sh: unknown arg '$arg'" >&2; exit 2 ;;
    esac
done

case "$TARGET" in
    ftv|p8a) ;;
    *) sed -n '2,17p' "$0"; exit 2 ;;
esac

[ "$BUILD" -eq 1 ] && "$HERE/build.sh"
[ -f "$APK" ] || { echo "install.sh: no APK at $APK (build first)" >&2; exit 1; }

case "$TARGET" in
p8a)
    # ~/storage/downloads is the Files app's Download folder. termux-open
    # --chooser does not reliably raise the installer dialog on p8a, so the
    # last step stays manual on purpose.
    scp "$APK" p8a:storage/downloads/
    echo "copied to p8a:~/storage/downloads/ -- open it from Files to install"
    ;;
ftv)
    adb connect ftv:5555 >/dev/null || true
    state="$(adb devices | awk '$1 == "ftv:5555" { print $2 }')"
    case "$state" in
        device) ;;
        unauthorized)
            echo "install.sh: ftv:5555 is unauthorized -- accept the ADB dialog on the TV screen (always allow), then re-run" >&2
            exit 1 ;;
        *)
            echo "install.sh: ftv:5555 not connected (adbd off? enable ADB debugging in Developer options)" >&2
            exit 1 ;;
    esac
    # -r keeps app data and granted permissions across the upgrade; the debug
    # keystore in build.sh is what makes that possible at all.
    adb -s ftv:5555 install -r "$APK"
    [ "$LAUNCH" -eq 1 ] && adb -s ftv:5555 shell am start -n net.agentmedia.companion/.MainActivity
    ;;
esac

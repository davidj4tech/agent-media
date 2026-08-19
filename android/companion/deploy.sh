#!/usr/bin/env bash
# Build, install and restart the companion on p8a, without stealing the screen.
#
# Every install used to be a sideload and a tap; since 2026-08-19 the phone's
# own adb is paired to its own adbd over loopback, so this runs from red5. See
# the adb-shell-via-self-pairing note.
#
# The one thing worth getting right here is the last step. Restarting the app
# by launching MainActivity works and is wrong: MainActivity is the diagnostic
# screen, so every deploy jumps in front of whatever David is doing — eight
# times in one evening, before he asked for it to stop. WakeActivity is the
# revive door call_guard already uses: Theme.NoDisplay, finishes in onCreate,
# starts the service and gets out of the way.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PHONE="${COMPANION_PHONE:-p8a}"
REMOTE="\$HOME/storage/downloads/agent-media-companion.apk"

"$HERE/test/run.sh" >/dev/null
echo "tests ok"

"$HERE/build.sh" | tail -1

scp -q "$HERE/build/agent-media-companion.apk" "$PHONE:storage/downloads/agent-media-companion.apk"

# shellcheck disable=SC2029  # $REMOTE is meant to expand on the phone
ssh "$PHONE" "adb install -r $REMOTE | tail -1
adb shell am force-stop net.agentmedia.companion
sleep 1
adb shell am start -n net.agentmedia.companion/.WakeActivity >/dev/null
sleep 3
curl -s -m 5 127.0.0.1:8770/log | head -3"

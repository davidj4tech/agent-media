#!/usr/bin/env bash
# Host-side tests for the companion app's non-Android code.
#
# Json, MpvIpc, MpvState and FocusPolicy import nothing from android.*, so they compile and
# run under a plain JDK against a fake mpv on a loopback port. That is the only
# fast feedback loop this project has: p8a has no adb, so every device test is
# a sideload and a squint at the phone screen.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$(dirname "$HERE")"
OUT="$HERE/build"

rm -rf "$OUT"; mkdir -p "$OUT"

javac -nowarn -d "$OUT" \
    "$APP/src/net/agentmedia/companion/Json.java" \
    "$APP/src/net/agentmedia/companion/MpvIpc.java" \
    "$APP/src/net/agentmedia/companion/MpvState.java" \
    "$APP/src/net/agentmedia/companion/FocusPolicy.java" \
    "$APP/src/net/agentmedia/companion/StatusServer.java" \
    "$HERE/net/agentmedia/companion/FakeMpv.java" \
    "$HERE/net/agentmedia/companion/IpcTest.java" \
    "$HERE/net/agentmedia/companion/FocusTest.java" \
    "$HERE/net/agentmedia/companion/StatusTest.java"

java -cp "$OUT" net.agentmedia.companion.IpcTest
java -cp "$OUT" net.agentmedia.companion.FocusTest
java -cp "$OUT" net.agentmedia.companion.StatusTest

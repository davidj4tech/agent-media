#!/usr/bin/env bash
# Host-side tests for the companion app's non-Android code.
#
# Json, MpvIpc, MpvState and the two focus policies import nothing from android.*,
# so they compile and
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
    "$APP/src/net/agentmedia/companion/SpeechPolicy.java" \
    "$APP/src/net/agentmedia/companion/FrontChannel.java" \
    "$APP/src/net/agentmedia/companion/StatusServer.java" \
    "$APP/src/net/agentmedia/companion/ButtonPolicy.java" \
    "$APP/src/net/agentmedia/companion/ExitReason.java" \
    "$APP/src/net/agentmedia/companion/Marquee.java" \
    "$APP/src/net/agentmedia/companion/BargeIn.java" \
    "$APP/src/net/agentmedia/companion/Server.java" \
    "$APP/src/net/agentmedia/companion/MpvServer.java" \
    "$APP/src/net/agentmedia/companion/MicSteady.java" \
    "$APP/src/net/agentmedia/companion/Loopback.java" \
    "$APP/src/net/agentmedia/companion/ShareRequest.java" \
    "$APP/src/net/agentmedia/companion/AskRequest.java" \
    "$APP/src/net/agentmedia/companion/RecentList.java" \
    "$APP/src/net/agentmedia/companion/RecentRows.java" \
    "$APP/src/net/agentmedia/companion/Channels.java" \
    "$APP/src/net/agentmedia/companion/Chapters.java" \
    "$APP/src/net/agentmedia/companion/ClipCache.java" \
    "$APP/src/net/agentmedia/companion/HoldRate.java" \
    "$APP/src/net/agentmedia/companion/DictationHold.java" \
    "$APP/src/net/agentmedia/companion/BookHold.java" \
    "$APP/src/net/agentmedia/companion/CardText.java" \
    "$APP/src/net/agentmedia/companion/Health.java" \
    "$APP/src/net/agentmedia/companion/Style.java" \
    "$HERE/net/agentmedia/companion/FakeMpv.java" \
    "$HERE/net/agentmedia/companion/IpcTest.java" \
    "$HERE/net/agentmedia/companion/FocusTest.java" \
    "$HERE/net/agentmedia/companion/FrontTest.java" \
    "$HERE/net/agentmedia/companion/StatusTest.java" \
    "$HERE/net/agentmedia/companion/ExitTest.java" \
    "$HERE/net/agentmedia/companion/MarqueeTest.java" \
    "$HERE/net/agentmedia/companion/StateTest.java" \
    "$HERE/net/agentmedia/companion/BargeInTest.java" \
    "$HERE/net/agentmedia/companion/ServerTest.java" \
    "$HERE/net/agentmedia/companion/MpvServerTest.java" \
    "$HERE/net/agentmedia/companion/MicSteadyTest.java" \
    "$HERE/net/agentmedia/companion/ShareTest.java" \
    "$HERE/net/agentmedia/companion/AskTest.java" \
    "$HERE/net/agentmedia/companion/DictationTest.java" \
    "$HERE/net/agentmedia/companion/BookHoldTest.java" \
    "$HERE/net/agentmedia/companion/CardTextTest.java" \
    "$HERE/net/agentmedia/companion/HealthTest.java" \
    "$HERE/net/agentmedia/companion/RecentTest.java" \
    "$HERE/net/agentmedia/companion/RecentRowsTest.java" \
    "$HERE/net/agentmedia/companion/ChannelsTest.java" \
    "$HERE/net/agentmedia/companion/ClipCacheTest.java" \
    "$HERE/net/agentmedia/companion/HoldRateTest.java"

java -cp "$OUT" net.agentmedia.companion.IpcTest
java -cp "$OUT" net.agentmedia.companion.FocusTest
java -cp "$OUT" net.agentmedia.companion.FrontTest
java -cp "$OUT" net.agentmedia.companion.StatusTest
java -cp "$OUT" net.agentmedia.companion.ExitTest
java -cp "$OUT" net.agentmedia.companion.MarqueeTest
java -cp "$OUT" net.agentmedia.companion.StateTest
java -cp "$OUT" net.agentmedia.companion.BargeInTest
java -cp "$OUT" net.agentmedia.companion.ServerTest
java -cp "$OUT" net.agentmedia.companion.ShareTest
java -cp "$OUT" net.agentmedia.companion.AskTest
java -cp "$OUT" net.agentmedia.companion.DictationTest
java -cp "$OUT" net.agentmedia.companion.BookHoldTest
java -cp "$OUT" net.agentmedia.companion.CardTextTest
java -cp "$OUT" net.agentmedia.companion.HealthTest
java -cp "$OUT" net.agentmedia.companion.RecentTest
java -cp "$OUT" net.agentmedia.companion.RecentRowsTest
java -cp "$OUT" net.agentmedia.companion.ChannelsTest
java -cp "$OUT" net.agentmedia.companion.ClipCacheTest
java -cp "$OUT" net.agentmedia.companion.HoldRateTest
java -cp "$OUT" net.agentmedia.companion.MpvServerTest
java -cp "$OUT" net.agentmedia.companion.MicSteadyTest

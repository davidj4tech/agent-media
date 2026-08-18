#!/usr/bin/env bash
# Host-side tests for the spike's non-Android code.
#
# Measure and Readout import nothing from android.*, so they run under a plain
# JDK. Same bargain as the companion's test/run.sh: p8a has no adb, so every
# device run costs a sideload and a tap, and anything that can be wrong on the
# build host should be found there.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$(dirname "$HERE")"
OUT="$HERE/build"

rm -rf "$OUT"; mkdir -p "$OUT"

javac -nowarn -d "$OUT" \
    "$APP/src/net/agentmedia/speedspike/Measure.java" \
    "$APP/src/net/agentmedia/speedspike/Readout.java" \
    "$HERE/net/agentmedia/speedspike/MeasureTest.java" \
    "$HERE/net/agentmedia/speedspike/ReadoutTest.java"

java -cp "$OUT" net.agentmedia.speedspike.MeasureTest
java -cp "$OUT" net.agentmedia.speedspike.ReadoutTest

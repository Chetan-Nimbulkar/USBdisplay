#!/usr/bin/env bash
# Host-side unit tests for the pure-Java UdspParser (no Android APIs needed).
# Uses system JUnit 3.8 (/usr/share/java/junit.jar). No network, no Gradle.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
JUNIT=/usr/share/java/junit.jar
test -f "$JUNIT" || { echo "missing $JUNIT"; exit 1; }
OUT="$ROOT/test-out"
rm -rf "$OUT"
mkdir -p "$OUT"
javac -Xlint:-options -encoding UTF-8 --release 8 -cp "$JUNIT" -d "$OUT" \
  "$ROOT/src/com/usbdisplay/receiver/UdspParser.java" \
  "$ROOT/test/com/usbdisplay/receiver/UdspParserTest.java"
/usr/lib/jvm/java-11-openjdk-amd64/bin/java -cp "$OUT:$JUNIT" junit.textui.TestRunner com.usbdisplay.receiver.UdspParserTest

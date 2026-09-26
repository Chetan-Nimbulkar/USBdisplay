#!/usr/bin/env bash
# Phase 4.1 Diagnostic Experiments
# Run with: ./experiments/phase41_diagnostics.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERIAL="${SERIAL:-d9a9eec8}"
PORT="${PORT:-18958}"

cleanup() {
    pkill -f "USBdisplay.*compositor gnome" 2>/dev/null
    pkill -f "gnome_capture" 2>/dev/null
    adb -s "$SERIAL" reverse --remove tcp:18958 2>/dev/null
    adb -s "$SERIAL" shell am force-stop com.usbdisplay.receiver 2>/dev/null
}

wait_for() { # timeout label cmd...
  local timeout="$1" label="$2"; shift 2
  local i=0
  while [ "$i" -lt "$timeout" ]; do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1; i=$((i + 1))
  done
  return 1
}

run_test() {
    local name="$1"
    local duration="${2:-60}"
    
    echo "=== $1 ==="
    rm -f "/tmp/phase41_$1.log"
    setsid nohup ./USBdisplay --compositor gnome --resolution 800x600 --fps 30 --refresh 30 --debug \
        > "/tmp/phase41_$1.log" 2>&1 < /dev/null &
    local pid=$!
    
    sleep 10  # startup
    
    if [[ "$1" == "TEST_B" ]]; then
        echo "WAIT FOR FIRST FRAME + IDR, THEN STOP ALL ACTIVITY"
        sleep "$duration"
    else
        sleep "$duration"
    fi
    
    wait $pid 2>/dev/null || true
    echo "--- $1 METRICS ---"
    grep -E "stats|RX: frames=" "/tmp/phase41_$1.log" | tail -5
    adb -s d9a9eec8 logcat -d -s USBDISP 2>/dev/null | grep -E "RX: frames=|first IDR|first frame" | tail -3
}

cleanup() {
    pkill -f "USBdisplay.*compositor gnome" 2>/dev/null
    pkill -f "gnome_capture" 2>/dev/null
    adb -s "$SERIAL" reverse --remove tcp:18958 2>/dev/null
    adb -s "$SERIAL" shell am force-stop com.usbdisplay.receiver 2>/dev/null
}

trap cleanup EXIT

# Main
cd /home/kali/Projects/USBdisplay
cleanup
sleep 2

adb devices | grep -q d9a9eec8 || { echo "No device"; exit 1; }

echo "=== PHASE 4.1 DIAGNOSTICS ==="
echo "Boot ID: $(cat /proc/sys/kernel/random/boot_id)"
echo "Uptime: $(awk '{print int($1)}' /proc/uptime)s"

# TEST A: Active Desktop (60s)
run_test "TEST_A" 60

# TEST B: Static Desktop (120s) - CRITICAL
echo "=== TEST B: Static Desktop ==="
echo "1. Approve ScreenCast prompt if shown"
./USBdisplay --compositor gnome --resolution 800x600 --fps 30 --refresh 30 --debug > /tmp/test_b.log 2>&1 &
sleep 15  # wait for first frame + IDR
echo "NOW: STOP ALL ACTIVITY. No mouse, no clock, no video. Wait 120s."
sleep 120
# kill handled by trap

# TEST C: Controlled damage bursts
# (Run after TEST B completes)

# TEST D: Startup timing
# Clean start, measure T0-T6

echo "All tests complete. Analyze logs in /tmp/*.log and adb logcat."
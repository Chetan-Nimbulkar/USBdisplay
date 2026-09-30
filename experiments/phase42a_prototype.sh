#!/usr/bin/env bash
# Phase 4.2A Prototype Test Runner

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERIAL="${SERIAL:-d9a9eec8}"
PORT="${PORT:-18958}"
RESULTS_DIR="$ROOT/docs/evidence/phase42a"

mkdir -p "$ROOT/docs/evidence/phase42a"

cleanup() {
    echo "Cleaning up..."
    pkill -f "phase42a_prototype.py" 2>/dev/null || true
    pkill -f "phase35_holder" 2>/dev/null || true
    adb -s "$SERIAL" reverse --remove tcp:18958 2>/dev/null
    adb -s "$SERIAL" shell am force-stop com.usbdisplay.receiver 2>/dev/null
    pkill -f "phase42a_prototype.py" 2>/dev/null
    pkill -f "phase35_holder" 2>/dev/null
    sleep 2
}

trap cleanup EXIT

log() {
    echo "[$(date +%H:%M:%S)] $*"
}

run_test() {
    local name="$1"
    local duration="$2"
    local logfile="/tmp/phase42a_${1}.log"

    echo "=== $name ==="
    rm -f "/tmp/phase42a_${1}.log"

    # Clean up any previous runs
    pkill -f "phase42a_prototype.py" 2>/dev/null || true
    sleep 2

    # Start the prototype
    setsid nohup python3 /home/kali/Projects/USBdisplay/experiments/phase42a_prototype.py \
        --test "$1" --duration "$2" \
        > "/tmp/phase42a_${1}.log" 2>&1 &
    local pid=$!

    # Wait for startup
    sleep 10

    # Check if process is still running
    if ! kill -0 $! 2>/dev/null; then
        echo "FAIL: Process died early"
        return 1
    fi

    # Wait for test duration
    sleep "$2"

    # Check if still running
    if kill -0 $! 2>/dev/null; then
        kill $! 2>/dev/null
        wait $! 2>/dev/null || true
    fi

    echo "=== $name complete ==="
    return 0
}

main() {
    local SERIAL="${SERIAL:-d9a9eec8}"

    # Setup
    adb -s "$SERIAL" reverse tcp:18958 tcp:18958 2>/dev/null
    adb -s "$1" shell "am force-stop com.usbdisplay.receiver; am start -n com.usbdisplay.receiver/.MainActivity --ez autoconnect true" 2>&1 | head -1
    adb -s "$1" logcat -c

    case "$1" in
        A)
            echo "=== TEST A: Active Desktop (60s) ==="
            log "Starting TEST A: Active Desktop (60s)"
            # Start with activity
            gnome-calculator -e "1+1" > /dev/null 2>&1 &
            sleep 2
            python3 /home/kali/Projects/USBdisplay/experiments/phase42a_prototype.py --test A --duration 60
            ;;
        B)
            log "Starting TEST B: Static Desktop (120s)"
            echo "IMPORTANT: After first frame appears, STOP ALL ACTIVITY (no mouse, keyboard, video)"
            python3 /home/kali/Projects/USBdisplay/experiments/phase42a_prototype.py --test B --duration 120
            ;;
        C)
            log "Starting TEST C: Controlled Damage Bursts"
            # This would need manual intervention
            echo "Manual test - run bursts manually"
            ;;
        D)
            log "Starting TEST D: Startup Timing"
            python3 /home/kali/Projects/USBdisplay/experiments/phase42a_prototype.py --test D --duration 30
            ;;
        E)
            log "Starting TEST E: 5-min Stability"
            python3 /home/kali/Projects/USBdisplay/experiments/phase42a_prototype.py --test E --duration 300
            ;;
        *)
            echo "Usage: $0 {A|B|C|D|E}"
            exit 1
            ;;
    esac
}

main "$@"
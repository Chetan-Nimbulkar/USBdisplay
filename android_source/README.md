# Android receiver source

## Architecture

```
adb reverse → 127.0.0.1:18958 (device)
   ↓ Socket (worker thread, never UI thread)
UdspParser.java (streaming: coalesced/split reads, length-before-alloc, CRC32-IEEE via java.util.zip)
   ↓ persistent H.264 via MediaCodec, with MJPEG fallback
Full-parent SurfaceView → physical tablet display
```

Files: `AndroidManifest.xml` (INTERNET, minSdk 23, targetSdk 33),
`src/com/usbdisplay/receiver/MainActivity.java` (UI + worker + stats),
`src/com/usbdisplay/receiver/UdspParser.java` (protocol mirror of `server/src/frame.rs`).
UI is programmatic; res/ contains only launcher icons. No Gradle is required.

Transport note: the app is the TCP **client**, so it uses `adb reverse tcp:PORT tcp:PORT`
(device connects → host `serve` listens). Phase 2.4's `adb forward` proved the same bytes
host→device with toybox `nc`; the direction flips here because a real app endpoint now exists.
Framing bytes are identical; see `docs/PROTOCOL.md` (unchanged).

The activity snapshots and locks the current physical orientation for each USBDisplay session. On EOF, Disconnect, host failure, or Activity destruction it releases that temporary lock and exits, restoring the user’s normal Android orientation policy.

## Build (offline, no Gradle)

Release identity: `versionName=0.95-Beta`, `versionCode=95`.

`./android_source/build-android.sh` — pins Java 11 (`build-tools 30.0.3` d8/R8 NPE-crashes on newer JVMs;
apksigner 30.0.3 needs space-separated `--ks-pass pass:…`). Steps: aapt2 link → javac
(`--release 8`) → d8 → zip into apk → zipalign → apksigner (auto-generated
`android_source/debug.keystore`, gitignored, regenerates). Default output: `android/USBdisplay-0.95-Beta.apk`. An optional first argument selects another output path.

For upgrade-compatible release builds, set `USBDISPLAY_KEYSTORE` to the existing
external signing-key path. Signing material must not be committed to the project.

Tested: SDK `/usr/lib/android-sdk`, build-tools 30.0.3, platform android-33.

## Install / run (tested on SM-T355Y, Android 11, armeabi-v7a)

```bash
adb -s SERIAL install -r android/USBdisplay-0.95-Beta.apk
adb -s SERIAL reverse tcp:18958 tcp:18958
# host: serve the known-good 2.4 stream (450 real MJPEG frames @30fps)
server/target/release/usbdisplay-server serve --port=18958 \
  --frames-dir=experiments/frames --fps=30 --count=450
adb -s SERIAL shell "am start -n com.usbdisplay.receiver/.MainActivity"
# tap Connect (button bounds via `uiautomator dump`: [0,188][1024,262] → tap 512 225)
adb -s SERIAL shell "input tap 512 225"
adb -s SERIAL logcat -d -s USBDISP:D   # stats every 60 frames
```

## 2.5 result (2026-09-23)

450/450 received **and rendered**, 800×600 MJPEG, fps 29.8→30.0, gaps=0 crc=0
badlen=0 badcodec=0 decode_errors=0, bitrate ≈1528 kbps, EOF clean
(`truncated_tail=0` equivalent: stream ended normally). First frame rendered 800×600
~35 ms after connect. Evidence: `docs/evidence/usbdisplay_2_5_end.png` (on-screen
counters) + `docs/evidence/usbdisplay_2_5_log.txt`. Full numbers in `docs/MEASUREMENTS.md`.

## Known limitations

- Java parser mirrors `frame.rs` but has no on-device unit tests (Rust side: 13/13).
- Renders synchronously in the network thread (TCP backpressure); no latest-only drop policy yet.
- Mid-stream disconnect/reconnect untested; reconnect = press Connect (new stream, SEQ restarts).
- Audio and touch input are not implemented.
- Stale-activity note: an early tap during rotation produced a confusing 0-frame state;
  force-stop + fresh launch + settled-landscape tap is the reliable procedure (scripted above).

# USBDisplay engineering handover

Last verified: 2026-09-24  
Primary working directory: `/home/chetan/Projects/usbdisplay`  
Current development host: Omarchy/Arch Linux, Hyprland Wayland  
Intended receiving editor environment: GNOME Wayland, likely Debian

## 1. Purpose of this handover

USBDisplay turns a USB-connected Android tablet into a second Wayland display. The stable reference flow is the current Omarchy/Arch/Hyprland implementation. GNOME support has been added behind a separate compositor backend but still needs live validation and refinement on a real GNOME Wayland session.

The next editor should improve and validate GNOME without regressing the working Hyprland path or weakening the shared transport and device-safety guarantees.

## 2. Non-negotiable product requirements

Preserve these requirements unless the owner explicitly changes them:

- Persistent H.264 capture is the default. Do not return to per-frame capture.
- Default target is 30 FPS with low host CPU/RAM use and low USB bandwidth.
- The mouse pointer must remain visible. Do not solve cursor defects by disabling it.
- Capture and TCP writing stay separate. The dedicated socket-writer thread must remain the sole owner of the receiver connection.
- The encoded queue stays bounded to limit memory and latency.
- Do not drop encoded H.264 access units from the middle of a dependency chain. Backpressure is intentional.
- A session is locked to one ADB serial and one physical USB path until failure or teardown.
- Never silently move a running session to another Android device or USB port.
- All ADB operations must remain scoped with `adb -s SERIAL`.
- Android-system modification is out of scope. APK install/uninstall, ADB commands, and logcat are allowed.
- Keep Hyprland and GNOME implementations behind compositor backends. Do not introduce GNOME-specific branches into the shared transport.
- KDE should remain detected but fail clearly until a real KWin backend exists.

## 3. Important repository-state warning

The Git history is not yet a safe representation of the working application.

At handover time:

- Most implementation files and directories are untracked.
- `docs/` has been removed from Git's index and is ignored, but remains locally available.
- `logs/` is ignored.
- The README has local content and still contains old paths.
- The root launcher is currently named `USBdisplay` with an uppercase `USB`.
- Do not run `git clean -fd`, `git reset --hard`, or discard untracked files. Doing so can delete the working implementation and APK.
- Inspect `git status --short` before every Git operation.
- Make a deliberate snapshot/commit of the intended source files before undertaking broad refactors.

The README is intentionally awaiting a later edit. Its examples still say `./scripts/usbdisplay` and `release/logs/`; those paths are obsolete.

## 4. Current project structure

~~~text
usbdisplay/
├── .gitignore
├── USBdisplay                 root launcher; executable
├── README.md                  stale launch/log paths; edit later
├── handover.md                this document
├── android/
│   ├── app-debug.apk          authoritative current receiver
│   ├── USBdisplay.png         supplied full-size application artwork
│   ├── debug.keystore
│   └── backups/               known receiver APK rollback points
├── bridge/
│   ├── __init__.py
│   ├── server.py              CLI, session orchestration, logging, teardown
│   ├── compositors.py         detection and Hyprland/GNOME/KDE backends
│   ├── gnome_capture.py       portal + PipeWire + GStreamer helper
│   ├── device.py              ADB discovery, selection, USB-path lease
│   ├── producers.py           persistent H.264/MJPEG producers and AU parser
│   ├── transport.py           bounded queue and dedicated socket writer
│   └── protocol.py            UDSP v1 packet encoder
├── docs/                      local design notes; ignored by Git
├── host/                      older inactive Rust prototype; keep it
│   ├── Cargo.toml
│   └── src/
├── logs/                      runtime logs; ignored and retention-bounded
├── target/                    Rust build output; retain as requested
├── tests/                     Python unit tests
└── tools/                     currently empty
~~~

### Active versus inactive code

The active application is the root launcher plus the Python `bridge/` and bundled APK.

`host/` is an older Rust experiment. It attempts direct PipeWire frame capture and collects benchmark data, but its output layer does not implement the current Android/ADB transport. Nothing in `USBdisplay` calls it. Keep it present, but do not confuse it with the production host bridge.

The root `Cargo.toml`, `Cargo.lock`, and `target/` belong to that Rust workspace.

## 5. How the active flow works

The root launcher resolves its own directory and executes:

~~~text
python <project>/bridge/server.py <arguments>
~~~

The end-to-end flow is:

~~~text
CLI and compositor detection
        |
        v
one compositor backend
        |
        +-- Hyprland: hyprctl output + wf-recorder/libx264
        |
        +-- GNOME: ScreenCast portal virtual source
        |           -> Mutter RecordVirtual internally
        |           -> PipeWire -> GStreamer/x264
        |
        +-- KDE: explicit not-implemented failure
        |
        v
persistent Annex-B H.264 access units
        |
        v
bounded LatestFrameQueue (default depth 2)
        |
        v
dedicated SocketWriter thread
        |
        v
UDSP v1 framing + TCP sendall()
        |
        v
adb reverse tcp:18958 tcp:18958
        |
        v
Android receiver -> MediaCodec AVC -> SurfaceView
~~~

### Session startup order

`bridge/server.py` currently performs this order:

1. Parse and validate CLI arguments.
2. Create a timestamped root-level `logs/usbdisplay-*.log`.
3. Detect the active Wayland compositor from `XDG_CURRENT_DESKTOP`.
4. Instantiate and validate only that backend.
5. Discover Android devices.
6. Select exactly one online device, optionally constrained by serial and USB path.
7. Acquire a non-blocking file lock for the serial plus physical USB identity.
8. Call the backend's `prepare_output()`.
9. Verify the receiver package, or install the bundled APK with `--install`.
10. Configure ADB reverse for port 18958 on the leased serial.
11. Start the host TCP listener/writer thread.
12. Force-stop and launch the receiver with autoconnect and stream dimensions.
13. Wait up to 10 seconds for the receiver connection while revalidating the lease.
14. Create and start the compositor-specific capture producer.
15. Revalidate the physical device once per second and report statistics every five seconds.
16. On failure or signal: stop capture, stop the socket, remove ADB reverse, stop the APK, clean up the virtual output, and release the lease.

Keep this failure ordering deterministic. A partial startup must still remove anything it created.

### Device safety

`bridge/device.py` parses `adb devices -l` and records:

- serial
- online state
- physical `usb:` path
- model/product
- transport ID

`DeviceLease` rejects wireless ADB because no physical USB path is available. It locks `$XDG_RUNTIME_DIR/usbdisplay/<serial>-<usb-path>.lock` and re-runs discovery throughout the session. If the selected tablet disconnects or moves ports, the session fails; it never selects a replacement.

### Transport and memory behavior

`SocketWriter` is the only thread that accepts and writes the TCP connection. It enables `TCP_NODELAY` and uses `sendall()`.

`LatestFrameQueue` defaults to two encoded frames:

- H.264: waits for space and records backpressure time. It does not discard dependency-chain frames.
- MJPEG: removes queued frames and retains the newest because every JPEG is independently decodable.

Do not replace H.264 behavior with a generic “latest frame wins” queue unless the stream is redesigned around recovery IDRs and decoder resynchronization.

### UDSP v1 wire format

All integers are little-endian:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 4 | ASCII `UDSP` |
| 4 | 4 | sequence number, u32 |
| 8 | 8 | PTS in milliseconds, u64 |
| 16 | 1 | codec: 1 MJPEG, 2 H.264 |
| 17 | 1 | flags; currently zero |
| 18 | 4 | payload length; maximum 8 MiB |
| 22 | N | encoded payload |
| 22+N | 4 | CRC32 over bytes 4..21 and payload |

Each H.264 payload is one complete Annex-B access unit. `AnnexBAccessUnitParser` splits on Access Unit Delimiter NAL type 9. The encoder therefore must keep `aud=1`. Initial/recovery keyframes must carry SPS/PPS and IDR data; keep repeated headers enabled.

## 6. Hyprland reference implementation: do not regress

`HyprlandBackend` is the golden, live-tested compositor path.

### Output creation

- Query monitors with `hyprctl -j monitors all`.
- Create `USBDisplay` as a headless output only if it does not already exist.
- Configure `WIDTHxHEIGHT@REFRESH`, position `auto-right`, scale 1 through `hyprctl eval`.
- Verify the output appears.
- Remove it on teardown only when this process created it and `--keep-output` was not requested.

### Persistent H.264 command

The default producer runs one process for the whole session:

~~~text
wf-recorder
  -o USBDisplay
  -f pipe:1
  -m h264
  -c libx264
  -x yuv420p
  -r 30
  -b 0
  -p preset=ultrafast
  -p tune=zerolatency
  -p profile=baseline
  -p crf=23
  -p threads=2
  -p x264-params=keyint=30:min-keyint=30:scenecut=0:repeat-headers=1:aud=1
  -D
~~~

`-D` is used for constant-rate output. `--damage-aware` omits it. The persistent process writes raw Annex-B H.264 to stdout; stderr is drained separately so the encoder cannot deadlock.

Do not alter this command while working on GNOME unless a change is independently justified and re-tested on Hyprland.

MJPEG is only a Hyprland compatibility fallback using `grim`. The H.264 path is the performance target.

## 7. GNOME Wayland implementation

### Current status

The GNOME implementation exists in:

- `GnomeBackend` in `bridge/compositors.py`
- `GnomePortalH264Producer` in `bridge/producers.py`
- `bridge/gnome_capture.py`

Its command construction and shared integration are covered by unit tests. It has not been live-validated in the current Hyprland session. The owner reports that an earlier GNOME Wayland proof of concept successfully exposed a virtual desktop through the portal/Mutter RecordVirtual path. Treat the current helper as implemented but awaiting real GNOME integration testing.

### Automatic selection

`detect_compositor()`:

1. Rejects an explicit non-Wayland `XDG_SESSION_TYPE`.
2. Splits `XDG_CURRENT_DESKTOP` on colons and normalizes case.
3. Gives explicit desktop identity priority over a stale `HYPRLAND_INSTANCE_SIGNATURE`.
4. Selects `gnome` for a GNOME entry.
5. Fails closed for unknown compositors.

Preserve this behavior. Do not fall back to Hyprland commands on GNOME.

### Required GNOME behavior, exactly

The helper must create and own one portal session:

1. Connect to the user session bus.
2. Create a proxy for:
   - bus: `org.freedesktop.portal.Desktop`
   - object: `/org/freedesktop/portal/desktop`
   - interface: `org.freedesktop.portal.ScreenCast`
3. Verify `AvailableSourceTypes` includes bit 4, VIRTUAL.
4. Verify `AvailableCursorModes` includes bit 2, EMBEDDED.
5. Call `CreateSession` with unique `handle_token` and `session_handle_token` values.
6. Subscribe to each request object's `org.freedesktop.portal.Request.Response` signal before making the request. Keep the current early-response handling; a response can arrive before the returned object path is processed.
7. Call `SelectSources` with:
   - `types = 4`
   - `multiple = false`
   - `cursor_mode = 2`
8. Call `Start` and allow the GNOME permission UI to complete. A cancellation must become a clear error, not a silent fallback.
9. Require exactly one returned stream and retain its PipeWire node ID.
10. Call `OpenPipeWireRemote` and retrieve the real file descriptor through the returned Unix FD list.
11. Build one persistent GStreamer pipeline using that FD and node ID.
12. Send only raw H.264 bytes to stdout. All status and diagnostics must go to stderr.
13. On SIGINT, SIGTERM, EOS, or error:
    - set the GStreamer pipeline to NULL
    - close the portal session
    - close the PipeWire FD
    - exit nonzero on real failure

The portal session owns the GNOME virtual monitor. Closing it removes that monitor. `--keep-output` therefore cannot be honored on GNOME and currently logs a warning.

### Current GStreamer pipeline

Conceptually, the helper builds:

~~~text
pipewiresrc fd=<portal-fd> path=<node-id> do-timestamp=true
! video/x-raw,width=W,height=H,framerate=REFRESH/1
! videorate
! videoscale
! videoconvert
! video/x-raw,width=W,height=H,framerate=FPS/1,format=I420
! x264enc
    byte-stream=true
    aud=true
    tune=zerolatency
    speed-preset=ultrafast
    key-int-max=FPS
    pass=qual
    quantizer=CRF
    threads=2
    option-string="repeat-headers=1:scenecut=0:min-keyint=FPS"
! video/x-h264,stream-format=byte-stream,alignment=au,profile=baseline
! fdsink fd=1 sync=false
~~~

These properties are protocol requirements, not cosmetic choices:

- `byte-stream=true`: Android receives Annex-B, not AVC length prefixes.
- `aud=true` and `alignment=au`: host parser boundaries depend on complete access units.
- `repeat-headers=1`: lets MediaCodec configure/recover from SPS/PPS.
- one-second `key-int-max` and `min-keyint`: bounded recovery interval.
- `scenecut=0`: predictable GOP behavior.
- baseline, I420, ultrafast, zerolatency, two threads: compatibility and stable host resource use.
- `sync=false` on stdout sink: avoid adding sink pacing beyond the capture pipeline.

Do not add log messages to stdout in `gnome_capture.py`. A single text byte corrupts the H.264 stream.

### GNOME resolution caveat

The portal API does not expose a separate “set Mutter monitor mode” call in this implementation. Width, height, and refresh are requested through PipeWire/GStreamer caps. Confirm on real GNOME that Mutter creates/negotates the intended virtual desktop geometry, rather than merely scaling a differently sized portal stream.

Test at minimum:

- 800x600 at 30/30
- 1280x720 at 30/30
- a tablet-native even-numbered resolution
- cursor movement across the complete virtual desktop
- GNOME fractional scaling on and off
- cancellation of the portal chooser
- USB disconnect during capture
- Ctrl-C during the chooser and during active streaming
- repeated start/stop cycles with no surviving portal session or PipeWire process

### GNOME packages on Debian

Use the Debian equivalents, not Arch package names:

~~~bash
sudo apt install   adb   python3   python3-gi   python3-gst-1.0   gir1.2-gstreamer-1.0   gstreamer1.0-tools   gstreamer1.0-pipewire   gstreamer1.0-plugins-base   gstreamer1.0-plugins-ugly   xdg-desktop-portal   xdg-desktop-portal-gnome
~~~

Before running, verify:

~~~bash
echo "$XDG_SESSION_TYPE"
echo "$XDG_CURRENT_DESKTOP"
gst-inspect-1.0 pipewiresrc
gst-inspect-1.0 x264enc
python3 -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gio, GLib, Gst"
systemctl --user status xdg-desktop-portal.service
systemctl --user status xdg-desktop-portal-gnome.service
~~~

Expected session values are Wayland and GNOME. Do not run the bridge with `sudo`; it needs the logged-in user's D-Bus and PipeWire session.

### Debian launcher portability issue

The current `USBdisplay` launcher executes `python`. Debian may only provide `python3` unless `python-is-python3` is installed. A safe portability improvement should prefer `python3` while confirming that the same change works on Arch. Keep the script location and project-root resolution intact.

### Suggested first GNOME run

Connect exactly one USB-debugging tablet, then:

~~~bash
cd /home/chetan/Projects/usbdisplay
adb devices -l
./USBdisplay --compositor gnome --resolution 800x600 --fps 30 --refresh 30 --install --debug
~~~

Approve the GNOME ScreenCast prompt. In a second terminal:

~~~bash
journalctl --user -f   -u xdg-desktop-portal.service   -u xdg-desktop-portal-gnome.service
~~~

For receiver diagnostics:

~~~bash
adb -s <SERIAL> logcat -s USBDISP
~~~

After the explicit path works, test automatic selection without `--compositor gnome`.

## 8. Android receiver and APK history

### Critical source limitation

There is no Android Studio/Gradle source project in this repository. The current APK is the authoritative receiver artifact. It was inspected successfully with AAPT and JADX, but decompiled output is not committed source.

Do not casually replace or rebuild `android/app-debug.apk`. If Android changes are required:

1. Back up the exact current APK.
2. Prefer reconstructing a maintainable Android source project.
3. Preserve the package/activity/protocol contract.
4. Sign the replacement consistently.
5. Install only the APK; do not modify Android system components.
6. Test both H.264 and the MJPEG fallback.
7. Record the APK SHA-256 and decoder behavior.

### Current APK identity

- Path: `android/app-debug.apk`
- SHA-256: `6ea447ba0ad2dedd125dbfd2a7a608a4039e27ba59837411b9e2bb34c2b90fac`
- Package: `com.usbdisplay.receiver`
- Activity: `com.usbdisplay.receiver.MainActivity`
- App label: `USB Display`
- Version code/name: `1` / `1.0`
- Minimum SDK: 23
- Target SDK: 33
- Orientation: landscape
- Permission: INTERNET
- Fixed receiver endpoint: `127.0.0.1:18958`

### Confirmed APK changes made during this project

Binary comparison against the backups confirms:

1. The older stable receiver configured H.264 MediaCodec at a hard-coded 800x600.
2. The current receiver reads `stream_width` and `stream_height` integer extras from its launch intent, defaulting to 800x600.
3. It uses those values for `MediaFormat.createVideoFormat("video/avc", width, height)` and receiver resolution reporting.
4. `bridge/server.py` passes the selected CLI dimensions through those exact extras.
5. The supplied `android/USBdisplay.png` was converted into density-specific launcher icons and installed in the current APK.
6. The current DEX is identical to `app-debug-pre-logo-20260924.apk`; the final difference is the added logo/resources and APK metadata packaging.

Rollback artifacts:

- `app-debug-stable-20260924.apk` and `app-debug-pre-custom-resolution-20260924.apk` are byte-identical and predate dynamic resolution.
- `app-debug-pre-logo-20260924.apk` contains dynamic resolution but predates the final logo resources.

### Receiver behavior that must remain compatible

The app:

- Runs full-screen, keeps the screen on, and hides system UI.
- Uses a `SurfaceView`.
- Starts a worker about three seconds after launch when `autoconnect=true`.
- Connects to localhost port 18958 through ADB reverse with TCP no-delay.
- Parses fragmented/coalesced UDSP packets and resynchronizes on magic.
- Validates length, codec, CRC, and sequence continuity.
- Supports codec 1 MJPEG and codec 2 H.264.
- Uses hardware `MediaCodec` for AVC.
- Extracts SPS/PPS from Annex-B access units before configuring MediaCodec.
- Waits for an IDR before normal decode.
- Detects probable decoder stalls and restarts the codec.
- Renders decoder output directly to the Surface.
- Provides an on-screen diagnostic overlay; tapping the video toggles it.
- Logs receiver metrics with tag `USBDISP`.

### Android aspect-ratio caveat

Although MediaCodec dimensions are now dynamic, the current receiver's `surfaceChanged()` still sizes the rendering surface to a hard-coded 4:3 aspect ratio. A 16:9 stream can therefore be letterboxed or scaled into a 4:3 surface. Fix this in a maintainable source project by basing the surface aspect ratio on `stream_width/stream_height` or decoded output format. Do not remove dynamic MediaCodec sizing while fixing the view layout.

## 9. Test coverage and live evidence

### Unit tests

The command:

~~~bash
cd /home/chetan/Projects/usbdisplay
python -m unittest discover -s tests -v
~~~

passed all 31 tests on 2026-09-24.

Coverage includes:

- Hyprland/GNOME/KDE detection and explicit override
- rejection of X11 and unknown sessions
- stale Hyprland environment variable precedence
- ADB device parsing and serial/USB selection
- refusal to choose ambiguously among multiple devices
- log age/size retention
- clean shutdown handling
- H.264 Annex-B/AUD parsing and IDR detection
- exact producer command construction for Hyprland and GNOME
- UDSP layout and CRC
- resolution CLI parsing and Android intent extras
- MJPEG latest-frame policy
- lossless H.264 backpressure and clean cancellation

Add tests before changing shared contracts. At minimum, keep all 31 passing.

### Hyprland live results

The current host environment at verification time was:

- Wayland
- `XDG_CURRENT_DESKTOP=Hyprland`
- Python 3.14.7
- ADB 37.0.0
- wf-recorder 0.6.0
- GStreamer 1.28.6

Evidence in `logs/` includes:

- A completed 30 FPS, 800x600 session sending 4,896 frames over about 164 seconds at approximately 1.20 Mbps average, zero host drops, and queue maximum 1/2.
- A post-restructure completed 30 FPS session sending 856 frames at approximately 1.16 Mbps average, zero drops/backpressure, followed by correct temporary-output cleanup.
- User tests played a full-screen 720p YouTube video during longer runs.
- Earlier receiver observations reported zero sequence gaps, CRC failures, decode errors, or receiver drops during the stable run.
- The tested receiver was a Samsung SM-T355Y on physical USB path `usb:1-1` during the recorded sessions.

Older log lines mention `release/logs/` because they predate the directory restructure. New runs correctly use root `logs/`.

### GNOME evidence boundary (updated 2026-09-25, Kali GNOME Wayland host)

Live-validated on: GNOME Shell 50.4 / mutter 50.4-1 / xdg-desktop-portal
1.22.1+ds-1 / xdg-desktop-portal-gnome 50.0-1 / PipeWire 1.6.8 /
GStreamer 1.28.4, tablet SM-T355Y on usb:1-1.

- Bare `./USBdisplay` auto-detects GNOME; portal approval required once per
  fresh chooser (system requirement, persists briefly across runs).
- Three 5-minute runs rendered cleanly on the tablet (e.g. 11279/11280 and
  11369/11370 @29.7–29.8fps, zero gaps/CRC/decode/dropped errors).
- Required fix found live: pipewiresrc caps must stay unconstrained
  (Mutter rejects fixed pins with EINVAL at PLAYING); geometry/rate enforced
  downstream. See `source_caps()` plus its unit test.
- Teardown hardening live-verified: SIGINT path fully clean; `kill -9` drill
  passes via helper parent-watchdog; stale adb-reverse cleared on next start;
  GNOME cleanup reaps exact-argv helpers (also caught 1 real leftover).
- Known open items: Meta-0 emission droughts (source goes quiet for minutes
  despite damage; identical symptom class as earlier investigations), and one
  unproven orphan path when main exits spontaneously during drought (fenced by
  reaper + watchdog, mechanism not yet isolated).
- Earlier owner-confirmed portal/Mutter virtual-display proof of concept
  stands alongside the above.

Record further GNOME runs in `logs/` with versions as above.

## 10. Known gaps and risks

- KDE is detected but intentionally unimplemented.
- GNOME supports H.264 only; MJPEG is rejected.
- GNOME ignores `--damage-aware` and warns.
- GNOME cannot retain a portal monitor after session close; `--keep-output` is ignored.
- GNOME live geometry, cursor, teardown, and repeated-session behavior still need validation.
- Portal implementations or older GNOME releases may not advertise VIRTUAL source type.
- The CLI accepts CRF 0..51 globally, while the GNOME backend rejects values above 50.
- The launcher uses `python` instead of the more portable `python3`.
- The Android rendering surface is still hard-coded to 4:3.
- Android source code is absent.
- Hardware VA-API encoding is disabled: the Intel `iHD` driver reproducibly aborted during teardown on the current laptop. Software x264 is the stable baseline.
- The README has obsolete launcher and log paths.
- The latest incomplete log may lack normal teardown if its process was interrupted; do not treat it as a benchmark result.
- `target/` is about 1.4 GB and belongs to the inactive Rust experiment, but the owner explicitly requested that it remain.

## 11. Safe rules for extending GNOME

When changing GNOME:

- Prefer edits to `GnomeBackend`, `GnomePortalH264Producer`, and `gnome_capture.py`.
- Keep shared `server.py` changes minimal and compositor-neutral.
- Do not modify `HyprlandBackend` or `H264Producer.command()` as collateral cleanup.
- Do not merge portal D-Bus work into the main orchestrator thread.
- Keep portal/GStreamer diagnostic output on stderr.
- Keep encoded video only on the helper's stdout.
- Preserve AUD-aligned Annex-B output and repeated SPS/PPS.
- Preserve embedded cursor mode.
- Preserve the bounded queue and dedicated writer.
- Preserve serial plus physical-port locking.
- Preserve cleanup on every exception path.
- Fail with a clear message if VIRTUAL or EMBEDDED cursor support is absent.
- Never silently fall back to capturing the physical primary monitor.
- Never silently select another Android device.
- Add GNOME-specific unit tests without weakening existing assertions.

After every shared-code change:

1. Run all unit tests.
2. Run `bash -n ./USBdisplay`.
3. Run `./USBdisplay --help`.
4. Run a GNOME session.
5. Transfer the changed tree back to the original Omarchy/Hyprland laptop.
6. Re-run a minimum three-minute Hyprland test at 800x600@30 while playing moving video.
7. Confirm cursor movement, no frozen duplicate cursor, no persistent pixel corruption, no black screen, and correct output removal.
8. Compare host FPS/bitrate/drop/backpressure logs.
9. Check `adb -s SERIAL logcat -s USBDISP` for gaps, CRC, decode errors, drops, decoder restarts, and output pacing.
10. Disconnect USB during a run and confirm the session stops rather than migrating.

## 12. Acceptance criteria for GNOME handback

GNOME support is ready to hand back only when all of the following are true:

- Auto-detection chooses GNOME on GNOME Wayland.
- No Hyprland command is executed.
- The portal exposes one virtual desktop, not the physical primary monitor.
- The chosen resolution is the actual usable virtual desktop geometry.
- The cursor is visible and remains live.
- Persistent H.264 runs at a stable target near 30 FPS.
- USB bandwidth is comparable to the software-x264 Hyprland baseline for similar content.
- Host RAM does not grow over a five-minute run.
- Host CPU remains reasonable and no encoder subprocesses survive teardown.
- Android shows no black screen, frozen cursor copy, accumulating stale pixels, CRC errors, sequence gaps, or decode failures.
- Ctrl-C, portal cancellation, Android disconnect, and receiver failure all clean up correctly.
- A second run works without logging out of GNOME.
- All 31 existing tests plus new GNOME tests pass.
- The unchanged Hyprland reference still passes the three-minute regression test.

## 13. Useful commands

~~~bash
cd /home/chetan/Projects/usbdisplay

# Current structure and repository safety
git status --short
find . -maxdepth 2 -type f | sort

# Unit tests
python -m unittest discover -s tests -v

# Launcher checks
bash -n ./USBdisplay
./USBdisplay --help

# Device identity
adb devices -l

# Stable/default run
./USBdisplay --resolution 800x600 --fps 30 --refresh 30

# Explicit GNOME diagnostics
./USBdisplay --compositor gnome --resolution 800x600 --fps 30 --refresh 30 --debug

# Install the bundled receiver, then run
./USBdisplay --install --resolution 800x600

# Pin a specific device and physical port
./USBdisplay --device-serial SERIAL --usb-path USB_PATH

# Receiver logs
adb -s SERIAL logcat -s USBDISP

# Confirm no stale reverse remains
adb -s SERIAL reverse --list

# Inspect capture elements
gst-inspect-1.0 pipewiresrc
gst-inspect-1.0 x264enc
~~~

## 14. Final guidance

Treat the Hyprland implementation, shared transport, UDSP protocol, USB device lease, and current APK as compatibility contracts. GNOME work should supply the same persistent H.264 access-unit stream through the existing producer interface and let the established queue, writer, framing, ADB, and Android layers continue unchanged.

The most valuable immediate work on GNOME is empirical: confirm portal virtual-monitor geometry, embedded cursor behavior, PipeWire negotiation, clean teardown, and repeated-session reliability. Make narrowly scoped changes based on those observations, then regression-test on the original Hyprland laptop before declaring the flow stable.

# USBDisplay

Low-latency Android second display for Wayland over a USB cable.

Current beta release: **0.95-Beta**.

## Current status

The host bridge detects the active Wayland compositor and selects a backend:

- Hyprland creates a headless output with `hyprctl` and captures it through one
  persistent `wf-recorder`/libx264 process.
- GNOME requests a virtual monitor from the ScreenCast portal. A native
  PipeWire helper negotiates the requested Meta-0 size, caches desktop and SPA
  cursor metadata independently, composites at the host's 30 Hz clock, and
  feeds the existing x264/Annex-B transport path.
- KDE is detected explicitly, but its KWin backend is not implemented yet.

Persistent H.264 capture is the default. MJPEG remains a Hyprland compatibility
fallback.

The session is pinned to one ADB serial and its physical USB path. Every ADB
operation is serial-scoped, and the lease is held through teardown. If that
device disconnects or moves ports, the session fails instead of switching to
another connected device.

## Run

Connect the tablet with USB debugging enabled, then run:

```bash
./USBdisplay
```

Before starting a session, run the read-only compatibility check:

```bash
./USBdisplay --check
```

It reports the detected backend, required host tools, selected physical USB
device, receiver installation, panel size, current orientation, and recommended
landscape and portrait stream modes. It does not create a virtual display or
launch the Android receiver. Audio routing is not supported in v0.95-Beta.

By default, USBDisplay reads the tablet's physical panel size and current
orientation, preserves its aspect ratio, avoids upscaling, and caps the long
edge at 1280 pixels. For example, a 1200×1920 tablet uses 1280×800 when it is
landscape and 800×1280 when it is portrait. Force portrait stream geometry with:

```bash
./USBdisplay --vertical
```

An exact expert override remains available and disables automatic sizing:

```bash
./USBdisplay --resolution 1280x720
```

`--vertical` cannot be combined with an explicit resolution. Resolution is
locked for the full session; a compositor size renegotiation terminates the
session safely instead of changing dimensions mid-stream.

The default profile captures at 30 FPS. A lower-rate compatibility profile is
available with `--fps 24 --refresh 24`.

When more than one Android device is connected, explicitly identify the target:

```bash
adb devices -l
./USBdisplay --device-serial SERIAL --usb-path USB_PATH
```

Use the current `serial` and `usb:` value reported by the first command. The
physical path can change when the cable is moved to another laptop port.

Useful options:

- `--version`: print the USBDisplay release version.
- `--install`: reinstall the bundled `android/USBdisplay-0.95-Beta.apk` receiver.
- `--check`: run the read-only host/device compatibility preflight.
- `--vertical`: automatically select the tablet's portrait resolution.
- `--resolution WIDTHxHEIGHT`: bypass automatic sizing with an exact size.
- `--compositor auto|hyprland|gnome|kde`: override automatic detection.
- `--codec mjpeg`: use independent JPEG frames via `grim`.
- `--damage-aware`: let the H.264 capture pause on unchanged frames.
- `--fps 24 --refresh 24`: use the lower-rate compatibility profile.
- `--h264-crf 23`: tune H.264 quality/bandwidth; lower is higher quality.
- `--keep-output`: retain a Hyprland output after teardown; GNOME portal outputs
  are always session-scoped.

Common requirements are Python 3, `android-tools`, and USB debugging.

Hyprland additionally needs `wf-recorder`; MJPEG mode needs `grim`. GNOME needs
`xdg-desktop-portal`, `xdg-desktop-portal-gnome`, `python-gobject`,
`gst-plugin-pipewire`, `gst-plugins-base`, and `gst-plugins-ugly`. On the first
GNOME run, approve the system ScreenCast prompt for the virtual monitor.

Build the native GNOME helper once after cloning or after changing its source:

```bash
./tools/build_gnome_capture_native.sh
```

Building it requires a C compiler, `pkg-config`, GStreamer development headers,
and PipeWire development headers. Packaged releases should build and install
this helper as part of the package rather than compiling it at application
startup.

Each run writes a timestamped file under `logs/`. Logs older than five
days are removed, and the oldest remaining logs are removed whenever the
folder exceeds 30 MiB.

## Capture support

Hyprland and GNOME Wayland are selected automatically from
`XDG_CURRENT_DESKTOP`. Unknown compositors fail clearly instead of running
Hyprland commands accidentally. The KDE/KWin portal backend is the next planned
compositor implementation.

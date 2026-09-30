# USBDisplay

Low-latency Android second display for Wayland over a USB cable.

## Current status

The host bridge detects the active Wayland compositor and selects a backend:

- Hyprland creates a headless output with `hyprctl` and captures it through one
  persistent `wf-recorder`/libx264 process.
- GNOME requests a virtual monitor from the ScreenCast portal. The GNOME portal
  delegates that request to Mutter `RecordVirtual`; a persistent
  PipeWire/GStreamer pipeline encodes its stream with x264.
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
./scripts/usbdisplay
```

The default resolution is 800×600. The bundled receiver accepts the resolution
chosen at launch, for example:

```bash
./scripts/usbdisplay --resolution 1280x720
```

The default profile captures at 30 FPS. A lower-rate compatibility profile is
available with `--fps 24 --refresh 24`.

When more than one Android device is connected, explicitly identify the target:

```bash
adb devices -l
./scripts/usbdisplay --device-serial SERIAL --usb-path USB_PATH
```

Use the current `serial` and `usb:` value reported by the first command. The
physical path can change when the cable is moved to another laptop port.

Useful options:

- `--install`: reinstall the bundled receiver APK.
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

Each run writes a timestamped file under `release/logs/`. Logs older than five
days are removed, and the oldest remaining logs are removed whenever the
folder exceeds 30 MiB.

## Capture support

Hyprland and GNOME Wayland are selected automatically from
`XDG_CURRENT_DESKTOP`. Unknown compositors fail clearly instead of running
Hyprland commands accidentally. The KDE/KWin portal backend is the next planned
compositor implementation.

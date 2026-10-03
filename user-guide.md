# USBDisplay User Guide

USBDisplay turns an Android tablet into a second display for a Wayland desktop over USB.

## Before you start

You need:

- A supported Wayland session: Hyprland or GNOME.
- An Android tablet with USB debugging enabled.
- A USB data cable.
- The USBDisplay receiver installed on the tablet.

KDE Plasma is detected, but its KWin streaming backend is not available in this release. Audio is not routed to the tablet in v0.95-Beta.

## Check compatibility

Connect the tablet, approve its USB debugging prompt, and run:

```bash
./USBdisplay --check
```

The check reports the desktop backend, required host tools, connected tablet, physical USB path, panel size, current orientation, receiver installation, and recommended portrait and landscape modes. It does not start a display session.

If more than one Android device is connected, select one explicitly:

```bash
./USBdisplay --check --device-serial SERIAL --usb-path USB_PATH
```

Use the values shown by `adb devices -l`.

## Start USBDisplay

For normal automatic sizing:

```bash
./USBdisplay
```

USBDisplay reads the tablet panel and starting orientation, preserves its aspect ratio, avoids upscaling, and selects a suitable resolution with a maximum long edge of 1280 pixels.

To install or update the bundled receiver before starting:

```bash
./USBdisplay --install
```

## Choose a resolution

Force the automatic portrait mode:

```bash
./USBdisplay --vertical
```

Use an exact resolution instead of automatic sizing:

```bash
./USBdisplay --resolution 1280x800
```

The width and height must be even. An exact resolution cannot be combined with `--vertical`.

The selected resolution remains fixed until the session ends. If the tablet is physically rotated during a session, the picture keeps its original aspect ratio. Android may show black bars rather than stretching the image. Stop and restart USBDisplay after rotating if you want a new full-screen orientation.

## Stop USBDisplay

Press `Ctrl+C` in the host terminal, or show the tablet overlay and tap **Disconnect**.

USBDisplay then closes the stream, removes its temporary virtual display, releases the Android decoder, restores the tablet orientation policy, and releases the selected USB device.

## Common problems

- **No device found:** unlock the tablet, reconnect the cable, and approve the USB debugging prompt.
- **Multiple devices connected:** use `--device-serial` and `--usb-path`.
- **Receiver not installed:** run `./USBdisplay --install`.
- **Wrong orientation:** rotate the tablet before starting, then restart USBDisplay.
- **Custom resolution looks distorted or has bars:** use the automatic mode shown by `./USBdisplay --check`.
- **Session stops after moving the cable:** restart USBDisplay; the physical USB port is intentionally locked for each session.
- **GNOME permission prompt:** approve the ScreenCast request for the virtual display.

Detailed diagnostic logs are stored in `logs/`. Logs older than five days are removed automatically, and total retained log size is limited to 30 MiB.


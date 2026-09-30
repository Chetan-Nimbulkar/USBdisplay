# USBdisplay

USBdisplay turns an Android tablet into a **USB-connected second display for Linux**.

It streams the Linux desktop to an Android device over USB using ADB and a custom TCP/UDSP transport.

## Usage

### 1. Install the Android receiver

Install:

```text
android/USBdisplay-0.7.0-beta.apk
```

on the Android tablet.

### 2. Connect the tablet

Enable **USB debugging** and connect the tablet to the Linux host.

Verify the connection:

```bash
adb devices
```

### 3. Launch USBdisplay

From the repository directory:

```bash
cd /path/to/USBdisplay
./USBdisplay
```

USBdisplay handles the host-side streaming and communicates with the Android receiver over USB.

## Status

**0.7.0 Beta**

Experimental software, currently developed and tested primarily with Linux/GNOME/Wayland and the Samsung Galaxy Tab A 2015.

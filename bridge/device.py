"""ADB device discovery and exclusive session leases."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Iterable


@dataclass(frozen=True)
class AndroidDevice:
    serial: str
    state: str
    usb_path: str | None
    model: str | None
    product: str | None
    transport_id: str | None

    @property
    def label(self) -> str:
        parts = [self.serial]
        if self.usb_path:
            parts.append(f"usb:{self.usb_path}")
        if self.model:
            parts.append(self.model)
        return " ".join(parts)


def parse_adb_devices(output: str) -> list[AndroidDevice]:
    devices: list[AndroidDevice] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices attached"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        serial, state = fields[:2]
        properties: dict[str, str] = {}
        for field in fields[2:]:
            if ":" in field:
                key, value = field.split(":", 1)
                properties[key] = value
        devices.append(
            AndroidDevice(
                serial=serial,
                state=state,
                usb_path=properties.get("usb"),
                model=properties.get("model"),
                product=properties.get("product"),
                transport_id=properties.get("transport_id"),
            )
        )
    return devices


def select_device(
    devices: Iterable[AndroidDevice],
    serial: str | None = None,
    usb_path: str | None = None,
) -> AndroidDevice:
    usb_path = usb_path.removeprefix("usb:") if usb_path else None
    available = [device for device in devices if device.state == "device"]
    matches = [
        device
        for device in available
        if (serial is None or device.serial == serial)
        and (usb_path is None or device.usb_path == usb_path)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        wanted = ""
        if serial:
            wanted += f" serial={serial}"
        if usb_path:
            wanted += f" usb={usb_path}"
        raise RuntimeError(f"no connected Android device matches{wanted}")
    labels = ", ".join(device.label for device in matches)
    raise RuntimeError(
        "multiple Android devices are connected; select one with "
        f"--device-serial or --usb-path: {labels}"
    )


def discover_devices() -> list[AndroidDevice]:
    result = subprocess.run(
        ["adb", "devices", "-l"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    return parse_adb_devices(result.stdout)


class DeviceLease:
    """Lock one ADB serial and physical USB path for the session lifetime."""

    def __init__(self, device: AndroidDevice) -> None:
        if not device.usb_path:
            raise RuntimeError(
                f"device {device.serial} has no physical USB path; wireless ADB is unsupported"
            )
        self.device = device
        self._lock_file: IO[str] | None = None

    def __enter__(self) -> DeviceLease:
        runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        lock_dir = runtime_dir / "usbdisplay"
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe_identity = re.sub(
            r"[^A-Za-z0-9_.-]", "_", f"{self.device.serial}-{self.device.usb_path}"
        )
        lock_file = (lock_dir / f"{safe_identity}.lock").open("w", encoding="utf-8")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock_file.close()
            raise RuntimeError(f"device is already leased: {self.device.label}") from error
        lock_file.write(f"pid={os.getpid()}\n{self.device.label}\n")
        lock_file.flush()
        self._lock_file = lock_file
        self.validate()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self._lock_file is not None:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    def adb_command(self, *args: str) -> list[str]:
        return ["adb", "-s", self.device.serial, *args]

    def run(
        self,
        *args: str,
        check: bool = True,
        capture: bool = False,
        timeout: float = 15,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.adb_command(*args),
            check=check,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
        )

    def validate(self) -> None:
        matches = [
            current
            for current in discover_devices()
            if current.serial == self.device.serial and current.state == "device"
        ]
        if len(matches) != 1 or matches[0].usb_path != self.device.usb_path:
            raise RuntimeError(
                "leased Android device disconnected or moved from physical USB port: "
                + self.device.label
            )

    def configure_reverse(self, port: int) -> None:
        self.validate()
        # Drop any stale mapping left by an unclean death before claiming the
        # port. Safe: the caller holds this device's exclusive lease, so no
        # other USBDisplay session can be using it.
        self.run("reverse", "--remove", f"tcp:{port}", check=False, timeout=5)
        self.run("reverse", f"tcp:{port}", f"tcp:{port}")

    def remove_reverse(self, port: int) -> None:
        self.run("reverse", "--remove", f"tcp:{port}", check=False, timeout=5)

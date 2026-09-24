import subprocess
import unittest
from unittest import mock

from bridge.device import AndroidDevice, DeviceLease, parse_adb_devices, select_device


ADB_OUTPUT = """List of devices attached
d9a9eec8               device usb:1-1 product:gt58lte model:SM_T355Y device:gt58lte transport_id:1
emulator-5554\tdevice product:sdk model:Pixel_Test device:generic transport_id:2
offline-one             offline usb:2-4 transport_id:3
"""


class DeviceSelectionTests(unittest.TestCase):
    def test_parses_physical_usb_identity(self) -> None:
        devices = parse_adb_devices(ADB_OUTPUT)
        self.assertEqual(len(devices), 3)
        self.assertEqual(devices[0].serial, "d9a9eec8")
        self.assertEqual(devices[0].usb_path, "1-1")
        self.assertEqual(devices[0].model, "SM_T355Y")

    def test_selects_by_serial_and_usb_path(self) -> None:
        selected = select_device(
            parse_adb_devices(ADB_OUTPUT),
            serial="d9a9eec8",
            usb_path="usb:1-1",
        )
        self.assertEqual(selected.serial, "d9a9eec8")

    def test_does_not_pick_between_multiple_devices(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "multiple Android devices"):
            select_device(parse_adb_devices(ADB_OUTPUT))

    def test_ignores_offline_device(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no connected Android device"):
            select_device(parse_adb_devices(ADB_OUTPUT), serial="offline-one")


class ReverseSweepTests(unittest.TestCase):
    def test_configure_reverse_clears_stale_mapping_first(self) -> None:
        device = AndroidDevice(
            serial="d9a9eec8",
            state="device",
            usb_path="1-1",
            model="SM_T355Y",
            product="gt58lte",
            transport_id="1",
        )
        lease = DeviceLease(device)
        calls: list[list[str]] = []

        def fake_run(cmd: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            argv = list(cmd)  # type: ignore[arg-type]
            calls.append(argv)
            if argv[:2] == ["adb", "devices"]:
                return subprocess.CompletedProcess(argv, 0, stdout=ADB_OUTPUT, stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch("bridge.device.subprocess.run", side_effect=fake_run):
            lease.configure_reverse(18958)
        reverse_calls = [c for c in calls if "reverse" in c]
        self.assertEqual(len(reverse_calls), 2)
        self.assertIn("--remove", reverse_calls[0])
        self.assertNotIn("--remove", reverse_calls[1])
        self.assertEqual(reverse_calls[1][-2:], ["tcp:18958", "tcp:18958"])


if __name__ == "__main__":
    unittest.main()

import argparse
import unittest

from bridge.server import (
    compatible_resolution,
    launch_receiver,
    orientation_name,
    parse_args,
    parse_physical_size,
    parse_resolution,
    read_device_rotation,
    resolve_auto_resolution,
    stop_receiver,
    validate_args,
)


class FakeLease:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.activity_results: list[str] = []

    def run(self, *args: str, **kwargs: object) -> object:
        self.calls.append((args, kwargs))
        if args == ("shell", "wm", "size"):
            return argparse.Namespace(stdout="Physical size: 1200x1920\n")
        if args == ("shell", "dumpsys", "input"):
            return argparse.Namespace(stdout="  SurfaceOrientation: 1\n")
        if args == ("shell", "dumpsys", "activity", "activities"):
            output = self.activity_results.pop(0) if self.activity_results else ""
            return argparse.Namespace(stdout=output)
        return argparse.Namespace(stdout="")


class ResolutionTests(unittest.TestCase):
    def test_check_flag_is_read_only_mode(self) -> None:
        args = parse_args(["--check"])

        self.assertTrue(args.check)
        self.assertEqual((args.width, args.height), (None, None))

    def test_orientation_names_cover_android_rotations(self) -> None:
        self.assertEqual(orientation_name(0), "portrait")
        self.assertEqual(orientation_name(1), "landscape")
        self.assertEqual(orientation_name(2), "reverse portrait")
        self.assertEqual(orientation_name(3), "reverse landscape")

    def test_parses_resolution_option(self) -> None:
        args = parse_args(["--resolution", "1280x720"])

        self.assertEqual((args.width, args.height), (1280, 720))

    def test_keeps_legacy_width_and_height_options(self) -> None:
        args = parse_args(["--width", "1024", "--height", "768"])

        self.assertEqual((args.width, args.height), (1024, 768))

    def test_default_resolution_is_deferred_until_device_is_locked(self) -> None:
        args = parse_args([])

        self.assertEqual((args.width, args.height), (None, None))

    def test_selects_landscape_resolution_from_tablet_panel(self) -> None:
        args = parse_args([])
        lease = FakeLease()

        resolve_auto_resolution(args, lease, 1)  # type: ignore[arg-type]

        self.assertEqual((args.width, args.height), (1280, 800))
        self.assertEqual(lease.calls[0][0], ("shell", "wm", "size"))

    def test_current_portrait_orientation_selects_portrait_by_default(self) -> None:
        args = parse_args([])
        lease = FakeLease()

        resolve_auto_resolution(args, lease, 0)  # type: ignore[arg-type]

        self.assertEqual((args.width, args.height), (800, 1280))

    def test_reads_current_android_surface_orientation(self) -> None:
        lease = FakeLease()

        self.assertEqual(read_device_rotation(lease), 1)  # type: ignore[arg-type]

    def test_selects_portrait_resolution_from_tablet_panel(self) -> None:
        args = parse_args(["--vertical"])
        lease = FakeLease()

        resolve_auto_resolution(args, lease, 1)  # type: ignore[arg-type]

        self.assertEqual((args.width, args.height), (800, 1280))

    def test_explicit_resolution_overrides_tablet_panel(self) -> None:
        args = parse_args(["--resolution", "1024x600"])
        lease = FakeLease()

        resolve_auto_resolution(args, lease, 1)  # type: ignore[arg-type]

        self.assertEqual((args.width, args.height), (1024, 600))
        self.assertEqual(lease.calls, [])

    def test_rejects_vertical_with_explicit_resolution(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--vertical", "--resolution", "800x1280"])

    def test_parses_physical_size_before_android_override(self) -> None:
        size = parse_physical_size(
            "Physical size: 1200x1920\nOverride size: 800x1280\n"
        )

        self.assertEqual(size, (1200, 1920))

    def test_auto_resolution_never_upscales_small_panels(self) -> None:
        self.assertEqual(
            compatible_resolution(600, 1024, vertical=False),
            (1024, 600),
        )

    def test_rejects_mixed_resolution_options(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--resolution", "1280x720", "--width", "800"])

    def test_rejects_invalid_resolution_text(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_resolution("1280-by-720")

    def test_rejects_odd_dimensions(self) -> None:
        args = parse_args(["--resolution", "801x600"])

        with self.assertRaises(SystemExit):
            validate_args(args)

    def test_receiver_eof_cleanup_removes_cached_package_process(self) -> None:
        lease = FakeLease()

        stop_receiver(lease)  # type: ignore[arg-type]

        commands = [call[0] for call in lease.calls]
        self.assertIn(("shell", "am", "force-stop", "com.usbdisplay.receiver"), commands)

    def test_passes_dimensions_to_receiver(self) -> None:
        lease = FakeLease()

        launch_receiver(lease, 1280, 720, 1)  # type: ignore[arg-type]

        launch_args = lease.calls[1][0]
        self.assertIn("stream_width", launch_args)
        self.assertEqual(launch_args[launch_args.index("stream_width") + 1], "1280")
        self.assertIn("stream_height", launch_args)
        self.assertEqual(launch_args[launch_args.index("stream_height") + 1], "720")
        self.assertIn("session_rotation", launch_args)
        self.assertEqual(launch_args[launch_args.index("session_rotation") + 1], "1")


if __name__ == "__main__":
    unittest.main()

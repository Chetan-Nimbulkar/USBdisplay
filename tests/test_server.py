import argparse
import unittest

from bridge.server import launch_receiver, parse_args, parse_resolution, validate_args


class FakeLease:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run(self, *args: str, **kwargs: object) -> None:
        self.calls.append((args, kwargs))


class ResolutionTests(unittest.TestCase):
    def test_parses_resolution_option(self) -> None:
        args = parse_args(["--resolution", "1280x720"])

        self.assertEqual((args.width, args.height), (1280, 720))

    def test_keeps_legacy_width_and_height_options(self) -> None:
        args = parse_args(["--width", "1024", "--height", "768"])

        self.assertEqual((args.width, args.height), (1024, 768))

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

    def test_passes_dimensions_to_receiver(self) -> None:
        lease = FakeLease()

        launch_receiver(lease, 1280, 720)  # type: ignore[arg-type]

        launch_args = lease.calls[1][0]
        self.assertIn("stream_width", launch_args)
        self.assertEqual(launch_args[launch_args.index("stream_width") + 1], "1280")
        self.assertIn("stream_height", launch_args)
        self.assertEqual(launch_args[launch_args.index("stream_height") + 1], "720")


if __name__ == "__main__":
    unittest.main()

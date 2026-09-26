import unittest
import threading
from pathlib import Path

from bridge.producers import AnnexBAccessUnitParser, _start_codes, access_unit_is_keyframe
from bridge.producers import GnomePortalH264Producer, H264Producer
from bridge.gnome_capture import source_caps
from bridge.transport import LatestFrameQueue


START = b"\x00\x00\x00\x01"
AUD = START + b"\x09\xf0"
IDR_ACCESS_UNIT = AUD + START + b"\x67sps" + START + b"\x68pps" + START + b"\x65idr"
P_ACCESS_UNIT = AUD + START + b"\x41predicted"


class AnnexBParserTests(unittest.TestCase):
    def test_splits_access_units_across_arbitrary_chunks(self) -> None:
        parser = AnnexBAccessUnitParser()
        stream = IDR_ACCESS_UNIT + P_ACCESS_UNIT
        emitted = []
        emitted.extend(parser.feed(stream[:3]))
        emitted.extend(parser.feed(stream[3:11]))
        emitted.extend(parser.feed(stream[11:29]))
        emitted.extend(parser.feed(stream[29:]))

        self.assertEqual(emitted, [IDR_ACCESS_UNIT])
        self.assertEqual(parser.finish(), P_ACCESS_UNIT)

    def test_detects_idr_keyframe(self) -> None:
        self.assertTrue(access_unit_is_keyframe(IDR_ACCESS_UNIT))
        self.assertFalse(access_unit_is_keyframe(P_ACCESS_UNIT))

    def test_finds_three_and_four_byte_start_codes(self) -> None:
        payload = b"\x00\x00\x01\x09\xf0\x00\x00\x00\x01\x65idr"

        self.assertEqual(
            _start_codes(payload),
            [(0, 3, 9), (5, 9, 5)],
        )

    def test_h264_recorder_writes_to_stdout_pipe(self) -> None:
        producer = H264Producer(
            LatestFrameQueue(2),
            threading.Event(),
            output="USBDisplay",
            fps=30,
            crf=23,
            constant_fps=True,
        )

        command = producer.command()
        self.assertEqual(command[command.index("-f") + 1], "pipe:1")

    def test_gnome_capture_command_passes_stream_settings(self) -> None:
        producer = GnomePortalH264Producer(
            LatestFrameQueue(2),
            threading.Event(),
            helper=Path("/opt/usbdisplay/gnome_capture.py"),
            width=1280,
            height=720,
            fps=30,
            refresh=60,
            crf=23,
            python="/usr/bin/python",
        )

        self.assertEqual(
            producer.command(),
            [
                "/usr/bin/python",
                "/opt/usbdisplay/gnome_capture.py",
                "--width",
                "1280",
                "--height",
                "720",
                "--fps",
                "30",
                "--refresh",
                "60",
                "--crf",
                "23",
                "--cursor-mode",
                "auto",
            ],
        )


    def test_gnome_source_caps_stay_unconstrained(self) -> None:
        # Mutter rejects fixed size/framerate pins on the virtual node
        # (EINVAL at PLAYING); geometry/rate are enforced downstream.
        self.assertEqual(source_caps(), "video/x-raw")


if __name__ == "__main__":
    unittest.main()

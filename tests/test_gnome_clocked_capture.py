from __future__ import annotations

import argparse
import inspect
import unittest

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from bridge.gnome_capture import (
    cursor_mode_to_value,
    CadenceMetrics,
    LatestFrameCache,
    build_output_pipeline,
    build_source_pipeline,
    source_caps,
)


class GnomeClockedCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        Gst.init(None)

    @staticmethod
    def args() -> argparse.Namespace:
        return argparse.Namespace(width=800, height=600, fps=30, crf=23)

    def test_cache_retains_only_latest_application_owned_frame(self) -> None:
        cache = LatestFrameCache()
        first = cache.replace(bytes(b"first"), 100)
        second = cache.replace(bytes(b"second"), 200)

        self.assertEqual(first.generation, 1)
        self.assertEqual(second.generation, 2)
        self.assertEqual(cache.snapshot(), second)
        self.assertTrue(cache.wait(0))

    def test_cursor_modes_match_xdg_portal_bitmask(self) -> None:
        self.assertEqual(cursor_mode_to_value("hidden"), 1)
        self.assertEqual(cursor_mode_to_value("embedded"), 2)
        self.assertEqual(cursor_mode_to_value("metadata"), 4)
        self.assertEqual(cursor_mode_to_value("auto"), 2)

    def test_cadence_metrics_distinguish_new_and_repeated_frames(self) -> None:
        metrics = CadenceMetrics()
        metrics.add_source(1_000_000_000)
        metrics.add_output(1_005_000_000, repeated=False)
        metrics.add_output(1_038_500_000, repeated=True)

        source, output, new, repeated, max_gap, startup = metrics.snapshot()
        self.assertEqual((source, output, new, repeated), (1, 2, 1, 1))
        self.assertAlmostEqual(max_gap, 33.5)
        self.assertAlmostEqual(startup, 5.0)

    def test_source_and_output_are_independent_pipelines(self) -> None:
        source = build_source_pipeline(0, 1, self.args())
        output = build_output_pipeline(self.args())
        self.assertIsNot(source, output)
        self.assertIsNotNone(source.get_by_name("source_sink"))
        appsrc = output.get_by_name("output_source")
        self.assertIsNotNone(appsrc)
        self.assertTrue(appsrc.get_property("is-live"))
        self.assertTrue(appsrc.get_property("block"))
        self.assertEqual(appsrc.get_property("format"), Gst.Format.TIME)

    def test_pipewire_caps_remain_unconstrained_and_videorate_is_absent(self) -> None:
        self.assertEqual(source_caps(), "video/x-raw")
        source_text = inspect.getsource(build_source_pipeline)
        output_text = inspect.getsource(build_output_pipeline)
        self.assertNotIn("videorate", source_text)
        self.assertNotIn("videorate", output_text)
        self.assertIn("appsink", source_text)
        self.assertIn("appsrc", output_text)

    def test_known_good_encoder_settings_and_annex_b_sink_are_preserved(self) -> None:
        output_text = inspect.getsource(build_output_pipeline)
        for setting in (
            "byte-stream=true",
            "aud=true",
            "tune=zerolatency",
            "speed-preset=ultrafast",
            "threads=1",
            "repeat-headers=1",
            "alignment=au",
            "profile=baseline",
            "h264parse config-interval=-1",
            "fdsink fd=1 sync=false",
        ):
            self.assertIn(setting, output_text)


if __name__ == "__main__":
    unittest.main()

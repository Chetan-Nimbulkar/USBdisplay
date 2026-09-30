#!/usr/bin/env python3
"""Phase 4.2A Prototype: Clock-driven frame repetition.

Two separate GStreamer pipelines connected via Python frame cache:

SOURCE PIPELINE (damage-driven):
pipewiresrc -> appsink -> FrameCache

OUTPUT PIPELINE (clock-driven):
appsrc (30 FPS clock) -> fakesink
"""

import sys
import os
import threading
import time
import signal
import argparse
import logging

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GLib, GObject, GstApp

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.frame_cache import FrameCache

LOG = logging.getLogger("phase42a")


class FrameCache:
    """Thread-safe single-frame cache with generation counter."""

    def __init__(self):
        self._lock = threading.Lock()
        self._latest_buffer = None
        self._generation = 0

    def push(self, buffer):
        """Store a new frame, replacing the previous one."""
        with self._lock:
            if self._latest_buffer is not None:
                self._latest_buffer.unref()
            buffer.ref()
            self._latest_buffer = buffer
            self._generation += 1

    def get_latest(self):
        """Get the latest frame with a new reference."""
        with self._lock:
            if self._latest_buffer is not None:
                self._latest_buffer.ref()
                return self._latest_buffer
        return None

    def get_latest_with_generation(self):
        """Get latest frame with its generation counter."""
        with self._lock:
            if self._latest_buffer is not None:
                self._latest_buffer.ref()
                return self._latest_buffer, self._generation
        return None, 0

    def clear(self):
        """Clear the cache."""
        with self._lock:
            if self._latest_buffer is not None:
                self._latest_buffer.unref()
                self._latest_buffer = None
                self._generation = 0


class ClockDrivenSource:
    """Clock-driven appsrc that pushes frames at 30 FPS."""

    FPS = 30
    FRAME_DURATION_NS = 33333333  # 33,333,333 ns = 1/30 second

    def __init__(self, frame_cache, fps=30):
        self.frame_cache = frame_cache
        self.fps = fps
        self.frame_duration_ns = 1_000_000_000 // 30  # 33,333,333 ns
        self._appsrc = None
        self._pipeline = None
        self._running = False
        self._timer_id = None
        self._first_frame = True
        self._next_pts = 0
        self._frame_count = 0
        self._lock = threading.Lock()
        self._running = False
        self._timer_id = None
        self._appsrc = None
        self._pipeline = None
        self._first_frame = True
        self._next_pts = 0
        self._frame_count = 0
        self._lock = threading.Lock()
        self._running = False
        self._timer_id = None
        self._appsrc = None
        self._pipeline = None
        self._first_frame = True
        self._next_pts = 0
        self._frame_count = 0
        self._lock = threading.Lock()
        self._running = False
        self._timer_id = None
        self._appsrc = None
        self._pipeline = None
        self._first_frame = True
        self._next_pts = 0
        self._frame_count = 0

    def create_pipeline(self):
        """Create the output pipeline: appsrc -> fakesink."""
        self._pipeline = Gst.parse_launch(
            "appsrc name=clocksrc is-live=true do-timestamp=true block=true "
            "format=time "
            "! video/x-raw,format=I420,width=800,height=600,framerate=30/1 "
            "! fakesink sync=true"
        )
        if not self._pipeline:
            raise RuntimeError("Failed to create pipeline")
        self._appsrc = self._pipeline.get_by_name("source")
        if not self._appsrc:
            # Find appsrc by iterating elements
            it = self._pipeline.iterate_elements()
            while True:
                ret, element = self._pipeline.iterate_elements().next()
                if not ret:
                    break
                if isinstance(element, GstApp.AppSrc):
                    self._appsrc = element
                    break
        if not self._appsrc:
            raise RuntimeError("Failed to find appsrc in pipeline")
        self._appsrc.set_property("is-live", True)
        self._appsrc.set_property("do-timestamp", True)
        self._appsrc.set_property("block", True)
        self._appsrc.set_property("format", Gst.Format.TIME)
        caps = Gst.Caps.from_string("video/x-raw,format=I420,width=800,height=600,framerate=30/1")
        self._appsrc.set_property("caps", Gst.Caps.from_string("video/x-raw,format=I420,width=800,height=600,framerate=30/1"))
        self._appsrc.connect("need-data", self._on_need_data)

    def _on_need_data(self, appsrc, length):
        """Callback when appsrc needs more data."""
        self._push_frame()

    def _push_frame(self):
        """Push a frame to appsrc."""
        buffer = self.frame_cache.get_latest()
        if buffer is None:
            return

        with self._lock:
            if self._first_frame:
                pts = 0
                self._first_frame = False
            else:
                pts = self._next_pts

            buffer = self.frame_cache.get_latest()
            if buffer is None:
                return

            buffer.pts = self._next_pts
            buffer.duration = self.frame_duration_ns

            ret = self._appsrc.emit("push-buffer", buffer)
            if ret != Gst.FlowReturn.OK:
                LOG.warning("Failed to push buffer: %s", ret)
                return

            self._frame_count += 1
            self._last_pts = self._next_pts
            self._next_pts += self.frame_duration_ns
            self._frame_count += 1

    def start(self):
        """Start the clock-driven source."""
        self._pipeline.set_state(Gst.State.PLAYING)
        self._running = True
        self._first_frame = True
        self._next_pts = 0
        self._frame_count = 0
        self._timer_id = GLib.timeout_add(33, self._timer_callback)

    def _timer_callback(self):
        """Timer callback to push frames at 30 FPS."""
        if not self._running:
            return GLib.SOURCE_REMOVE
        self._push_frame()
        return True

    def _push_frame(self):
        """Push a frame to appsrc."""
        buffer = self.frame_cache.get_latest()
        if buffer is None:
            return

        with self._lock:
            if self._first_frame:
                pts = 0
                self._first_frame = False
            else:
                pts = self._next_pts

            buffer = self.frame_cache.get_latest()
            if buffer is None:
                return

            buffer.pts = self._next_pts
            buffer.duration = self.frame_duration_ns

            ret = self._appsrc.emit("push-buffer", buffer)
            if ret != Gst.FlowReturn.OK:
                LOG.warning("Failed to push buffer: %s", ret)
                return

            self._frame_count += 1
            self._last_pts = self._next_pts
            self._next_pts += self.frame_duration_ns
            self._frame_count += 1

    def start(self):
        """Start the clock-driven source."""
        self._pipeline.set_state(Gst.State.PLAYING)
        self._running = True
        self._first_frame = True
        self._next_pts = 0
        self._frame_count = 0
        self._timer_id = GLib.timeout_add(33, self._timer_callback)

    def _timer_callback(self):
        """Timer callback to push frames at 30 FPS."""
        if not self._running:
            return GLib.SOURCE_REMOVE
        self._push_frame()
        return True

    def stop(self):
        """Stop the clock-driven source."""
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
        if self._timer_id:
            GLib.source_remove(self._timer_id)
        self._running = False


class FrameCache:
    """Thread-safe single-frame cache with generation counter."""

    def __init__(self):
        self._lock = threading.Lock()
        self._latest_buffer = None
        self._generation = 0

    def push(self, buffer):
        """Store a new frame, replacing the previous one."""
        with self._lock:
            if self._latest_buffer is not None:
                self._latest_buffer.unref()
            buffer.ref()
            self._latest_buffer = buffer
            self._generation += 1

    def get_latest(self):
        """Get the latest frame with a new reference."""
        with self._lock:
            if self._latest_buffer is not None:
                self._latest_buffer.ref()
                return self._latest_buffer
        return None

    def get_latest_with_generation(self):
        """Get latest frame with its generation counter."""
        with self._lock:
            if self._latest_buffer is not None:
                self._latest_buffer.ref()
                return self._latest_buffer, self._generation
        return None, 0

    def clear(self):
        """Clear the cache."""
        with self._lock:
            if self._latest_buffer is not None:
                self._latest_buffer.unref()
                self._latest_buffer = None
                self._generation = 0


class ClockDrivenSource:
    """Clock-driven appsrc that pushes frames at 30 FPS."""

    FPS = 30
    FRAME_DURATION_NS = 33333333  # 33,333,333 ns = 1/30 second

    def __init__(self, frame_cache, fps=30):
        self.frame_cache = frame_cache
        self.fps = fps
        self.frame_duration_ns = 1_000_000_000 // 30  # 33,333,333 ns
        self._appsrc = None
        self._pipeline = None
        self._running = False
        self._timer_id = None
        self._first_frame = True
        self._next_pts = 0
        self._frame_count = 0
        self._lock = threading.Lock()
        self._running = False
        self._timer_id = None
        self._appsrc = None
        self._pipeline = None
        self._first_frame = True
        self._next_pts = 0
        self._frame_count = 0
        self._lock = threading.Lock()
        self._running = False
        self._timer_id = None
        self._appsrc = None
        self._pipeline = None
        self._first_frame = True
        self._next_pts = 0
        self._frame_count = 0

    def create_pipeline(self):
        """Create the output pipeline: appsrc -> fakesink."""
        self._pipeline = Gst.parse_launch(
            "appsrc name=clocksrc is-live=true do-timestamp=true block=true "
            "format=time "
            "! video/x-raw,format=I420,width=800,height=600,framerate=30/1 "
            "! fakesink sync=true"
        )
        if not self._pipeline:
            raise RuntimeError("Failed to create pipeline")
        self._appsrc = self._pipeline.get_by_name("source")
        if not self._appsrc:
            # Find appsrc by iterating elements
            it = self._pipeline.iterate_elements()
            while True:
                ret, element = self._pipeline.iterate_elements().next()
                if not ret:
                    break
                if isinstance(element, GstApp.AppSrc):
                    self._appsrc = element
                    break
        if not self._appsrc:
            raise RuntimeError("Failed to find appsrc in pipeline")
        self._appsrc.set_property("is-live", True)
        self._appsrc.set_property("do-timestamp", True)
        self._appsrc.set_property("block", True)
        self._appsrc.set_property("format", Gst.Format.TIME)
        caps = Gst.Caps.from_string("video/x-raw,format=I420,width=800,height=600,framerate=30/1")
        self._appsrc.set_property("caps", Gst.Caps.from_string("video/x-raw,format=I420,width=800,height=600,framerate=30/1"))
        self._appsrc.connect("need-data", self._on_need_data)

    def _on_need_data(self, appsrc, length):
        """Callback when appsrc needs more data."""
        self._push_frame()

    def _push_frame(self):
        """Push a frame to appsrc."""
        buffer = self.frame_cache.get_latest()
        if buffer is None:
            return

        with self._lock:
            if self._first_frame:
                pts = 0
                self._first_frame = False
            else:
                pts = self._next_pts

            buffer = self.frame_cache.get_latest()
            if buffer is None:
                return

            buffer.pts = self._next_pts
            buffer.duration = self.frame_duration_ns

            ret = self._appsrc.emit("push-buffer", buffer)
            if ret != Gst.FlowReturn.OK:
                LOG.warning("Failed to push buffer: %s", ret)
                return

            self._frame_count += 1
            self._last_pts = self._next_pts
            self._next_pts += self.frame_duration_ns
            self._frame_count += 1

    def start(self):
        """Start the clock-driven source."""
        self._pipeline.set_state(Gst.State.PLAYING)
        self._running = True
        self._first_frame = True
        self._next_pts = 0
        self._frame_count = 0
        self._timer_id = GLib.timeout_add(33, self._timer_callback)

    def _timer_callback(self):
        """Timer callback to push frames at 30 FPS."""
        if not self._running:
            return GLib.SOURCE_REMOVE
        self._push_frame()
        return True

    def _push_frame(self):
        """Push a frame to appsrc."""
        buffer = self.frame_cache.get_latest()
        if buffer is None:
            return

        with self._lock:
            if self._first_frame:
                pts = 0
                self._first_frame = False
            else:
                pts = self._next_pts

            buffer = self.frame_cache.get_latest()
            if buffer is None:
                return

            buffer.pts = self._next_pts
            buffer.duration = self.frame_duration_ns

            ret = self._appsrc.emit("push-buffer", buffer)
            if ret != Gst.FlowReturn.OK:
                LOG.warning("Failed to push buffer: %s", ret)
                return

            self._frame_count += 1
            self._last_pts = self._next_pts
            self._next_pts += self.frame_duration_ns
            self._frame_count += 1

    def start(self):
        """Start the clock-driven source."""
        self._pipeline.set_state(Gst.State.PLAYING)
        self._running = True
        self._first_frame = True
        self._next_pts = 0
        self._frame_count = 0
        self._timer_id = GLib.timeout_add(33, self._timer_callback)

    def _timer_callback(self):
        """Timer callback to push frames at 30 FPS."""
        if not self._running:
            return GLib.SOURCE_REMOVE
        self._push_frame()
        return True

    def stop(self):
        """Stop the clock-driven source."""
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
        if self._timer_id:
            GLib.source_remove(self._timer_id)
        self._running = False


def main():
    import argparse
    import signal

    parser = argparse.ArgumentParser(description="Phase 4.2A Prototype")
    parser.add_argument("--test", choices=["A", "B", "C", "D", "E"], default="A",
                        help="Test to run: A=active, B=static, C=bursts, D=startup, E=stability")
    parser.add_argument("--duration", type=int, default=60, help="Test duration in seconds")
    args = parser.parse_args()

    Gst.init(None)
    Gst.init(None)

    # Create frame cache
    frame_cache = FrameCache()

    # Create clock-driven source
    clock_source = ClockDrivenSource(FrameCache())
    clock_source.create_pipeline()
    clock_source.start()

    # Run for specified duration
    time.sleep(args.duration)

    # Cleanup
    clock_source.stop()


if __name__ == "__main__":
    import sys
    sys.exit(main())
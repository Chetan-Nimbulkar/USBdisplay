"""Clock-driven appsrc producer for 30 FPS frame output."""

import threading
import time
import logging
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GLib, GObject

LOG = logging.getLogger("usbdisplay.clock_driven_source")


class ClockDrivenSource:
    """Clock-driven appsrc that outputs frames at a fixed 30 FPS cadence.

    Pulls frames from a FrameCache and pushes them to an appsrc at a fixed
    30 FPS rate using Gst.SystemClock for timing.
    """

    FPS = 30
    FRAME_DURATION_NS = 33333333  # 33,333,333 ns = 1/30 second
    TARGET_INTERVAL_MS = 33  # milliseconds

    def __init__(self, frame_cache, fps=30):
        self.frame_cache = frame_cache
        self.fps = fps
        self.frame_duration_ns = 1_000_000_000 // 30  # 33,333,333 ns
        self._running = False
        self._appsrc = None
        self._pipeline = None
        self._main_loop = None
        self._timer_source_id = None
        self._last_pts = 0
        self._frame_count = 0
        self._first_frame = True
        self._running = False
        self._timer_id = None
        self._lock = threading.Lock()

    def create_appsrc(self):
        """Create and configure the appsrc element."""
        appsrc = Gst.ElementFactory.make("appsrc", "clock-driven-source")
        if not appsrc:
            raise RuntimeError("Failed to create appsrc element")

        # Configure appsrc for live, timestamped, blocking operation
        appsrc.set_property("is-live", True)
        appsrc.set_property("do-timestamp", True)
        appsrc.set_property("block", True)
        appsrc.set_property("format", Gst.Format.TIME)
        appsrc.set_property("emit-signals", True)
        appsrc.set_property("max-bytes", 0)
        appsrc.set_property("max-buffers", 0)
        appsrc.set_property("max-bytes", 0)
        appsrc.set_property("min-percent", 0)

        # Set caps for I420 800x600 @ 30fps
        caps = Gst.Caps.from_string(
            "video/x-raw,format=I420,width=800,height=600,framerate=30/1"
        )
        appsrc.set_property("caps", caps)

        # Connect signals
        appsrc.connect("need-data", self._on_need_data)
        appsrc.connect("enough-data", self._on_enough_data)

        return appsrc

    def _on_need_data(self, appsrc, length):
        """Callback when appsrc needs more data."""
        self._push_frame()

    def _on_enough_data(self, appsrc):
        """Callback when appsrc has enough data."""
        pass

    def _push_frame(self):
        """Get latest frame from cache and push to appsrc."""
        buffer = self.frame_cache.get_latest()
        if buffer is None:
            return

        # Set PTS and duration
        buffer.pts = self._next_pts
        buffer.duration = self.frame_duration_ns
        self._next_pts += self.frame_duration_ns

        ret = self._appsrc.emit("push-buffer", buffer)
        if ret != Gst.FlowReturn.OK:
            LOG.warning("Failed to push buffer: %s", ret)
        else:
            with self._lock:
                self._frame_count += 1
                self._last_pts = self._next_pts - self.frame_duration_ns

    def _push_loop(self):
        """Timer callback to push frames at 30 FPS."""
        if not self._running:
            return False  # Don't repeat

        self._push_frame()
        return True  # Continue timer

    def start(self):
        """Start the clock-driven source."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._frame_count = 0
            self._first_frame = True
            self._last_pts = 0

            # Create appsrc
            self._appsrc = self._create_appsrc()
            self._appsrc.set_state(Gst.State.PLAYING)

            # Start timer for 30 FPS (33ms)
            self._timer_id = GLib.timeout_add(
                33,  # ~30 FPS
                self._timer_callback
            )
            LOG.info("Clock-driven source started at 30 FPS")

    def _timer_callback(self):
        """Timer callback to push frames at 30 FPS."""
        if not self._running:
            return GLib.SOURCE_REMOVE

        self._push_frame()
        return True  # Continue timer

    def _push_frame(self):
        """Get frame from cache and push to appsrc."""
        buffer = self.frame_cache.get_latest()
        if buffer is None:
            return

        # Set timestamp
        if self._first_frame:
            pts = 0
            self._first_frame = False
        else:
            pts = self._next_pts

        buffer.pts = self._next_pts
        buffer.duration = 33333333  # 33.33 ms

        # Push buffer
        ret = self._appsrc.emit("push-buffer", buffer)
        if ret != Gst.FlowReturn.OK:
            LOG.warning("Failed to push buffer: %s", ret)
            return

        self._frame_count += 1
        self._last_pts = self._next_pts
        self._next_pts += 33333333  # 33,333,333 ns = 1/30 second

    def stop(self):
        """Stop the clock-driven source."""
        self._running = False
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        if self._appsrc:
            self._appsrc.set_state(Gst.State.NULL)
            self._appsrc = None

    def get_stats(self):
        """Return statistics."""
        return {
            "frames_pushed": self._frame_count,
            "last_pts": self._last_pts,
        }
"""Thread-safe single-frame cache for latest-frame retention."""

import threading
import logging
import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

LOG = logging.getLogger("usbdisplay.frame_cache")


class FrameCache:
    """Thread-safe single-frame cache retaining only the latest GstBuffer."""

    def __init__(self):
        self._lock = threading.Lock()
        self._latest_buffer = None
        self._generation = 0

    def push(self, buffer):
        """Store a new frame, replacing the previous one.

        Args:
            buffer: Gst.Buffer to store. A new reference is taken.
        """
        with self._lock:
            if self._latest_buffer is not None:
                self._latest_buffer.unref()
            buffer.ref()
            self._latest_buffer = buffer
            self._generation += 1
            LOG.debug("FrameCache: pushed frame gen=%d", self._generation)

    def get_latest(self):
        """Get the latest frame with a new reference.

        Returns:
            Gst.Buffer or None if no frame cached.
            Caller owns the returned reference and must unref.
        """
        with self._lock:
            if self._latest_buffer is not None:
                buffer = self._latest_buffer
                buffer.ref()
                return buffer
        return None

    def get_latest_with_generation(self):
        """Get the latest frame with its generation counter.

        Returns:
            tuple (Gst.Buffer or None, int generation)
            Buffer has a new reference that caller must unref.
        """
        with self._lock:
            if self._latest_buffer is not None:
                buffer = self._latest_buffer
                buffer.ref()
                return buffer, self._generation
        return None, 0

    def clear(self):
        """Clear the cache, releasing any held buffer."""
        with self._lock:
            if self._latest_buffer is not None:
                self._latest_buffer.unref()
                self._latest_buffer = None
                self._generation = 0


class FrameCache:
    """Thread-safe single-frame cache with generation tracking.

    Holds at most one Gst.Buffer. New frames replace the previous one.
    Thread-safe for concurrent access from appsink callback and appsrc thread.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._latest_buffer = None
        self._generation = 0

    def push(self, buffer):
        """Store a new frame, replacing any previous frame.

        Takes ownership of a reference to buffer.
        """
        with self._lock:
            if self._buffer is not None:
                self._buffer.unref()
            buffer.ref()
            self._buffer = buffer
            self._generation += 1

    def get_latest(self):
        """Get the latest frame with a new reference.

        Returns:
            Gst.Buffer or None. Caller must unref.
        """
        with self._lock:
            if self._buffer is not None:
                buf = self._buffer
                buf.ref()
                return buf
        return None

    def get_latest_with_generation(self):
        """Get latest buffer with its generation counter.

        Returns:
            (Gst.Buffer or None, int generation)
        """
        with self._lock:
            if self._buffer is not None:
                buf = self._buffer
                buf.ref()
                return buf, self._generation
            return None, 0

    def get_generation(self):
        """Get current generation counter."""
        with self._lock:
            return self._generation

    def clear(self):
        """Clear the cache, releasing any held buffer."""
        with self._lock:
            if self._buffer is not None:
                self._buffer.unref()
                self._buffer = None
                self._generation = 0
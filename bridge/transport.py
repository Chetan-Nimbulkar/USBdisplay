"""Bounded encoded-frame queue and dedicated socket writer."""

from __future__ import annotations

import errno
import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass

from .protocol import CODEC_H264, encode_frame

LOG = logging.getLogger("usbdisplay.transport")


@dataclass(frozen=True)
class EncodedFrame:
    payload: bytes
    pts_ms: int
    codec: int
    keyframe: bool = False
    flags: int = 0


class LatestFrameQueue:
    """Bounded queue with lossless H.264 backpressure and lossy MJPEG updates."""

    def __init__(self, capacity: int = 2) -> None:
        if capacity < 1:
            raise ValueError("queue capacity must be positive")
        self._queue: queue.Queue[EncodedFrame] = queue.Queue(capacity)
        self._lock = threading.Lock()
        self.dropped = 0
        self.blocked_puts = 0
        self.blocked_seconds = 0.0
        self.max_depth = 0

    @property
    def capacity(self) -> int:
        return self._queue.maxsize

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def put(
        self,
        frame: EncodedFrame,
        stop_event: threading.Event | None = None,
    ) -> bool:
        if frame.codec == CODEC_H264:
            return self._put_h264(frame, stop_event)

        with self._lock:
            if self._queue.full():
                while True:
                    try:
                        self._queue.get_nowait()
                        self.dropped += 1
                    except queue.Empty:
                        break
            self._queue.put_nowait(frame)
            self.max_depth = max(self.max_depth, self._queue.qsize())
            return True

    def _put_h264(
        self,
        frame: EncodedFrame,
        stop_event: threading.Event | None,
    ) -> bool:
        """Wait for capacity rather than corrupting an H.264 dependency chain."""
        blocked_at: float | None = None
        while stop_event is None or not stop_event.is_set():
            try:
                self._queue.put(frame, timeout=0.05)
                with self._lock:
                    self.max_depth = max(self.max_depth, self._queue.qsize())
                    if blocked_at is not None:
                        self.blocked_seconds += time.monotonic() - blocked_at
                return True
            except queue.Full:
                if blocked_at is None:
                    blocked_at = time.monotonic()
                    with self._lock:
                        self.blocked_puts += 1

        if blocked_at is not None:
            with self._lock:
                self.blocked_seconds += time.monotonic() - blocked_at
        return False

    def get(self, timeout: float) -> EncodedFrame:
        return self._queue.get(timeout=timeout)


class SocketWriter(threading.Thread):
    """The only thread allowed to own and write the receiver TCP connection."""

    def __init__(
        self,
        frames: LatestFrameQueue,
        stop: threading.Event,
        host: str,
        port: int,
    ) -> None:
        super().__init__(name="usbdisplay-socket-writer", daemon=True)
        self.frames = frames
        self.stop_event = stop
        self.host = host
        self.port = port
        self.ready = threading.Event()
        self.connected = threading.Event()
        self.failure: BaseException | None = None
        self.peer_disconnected = False
        self.terminal_reason: str | None = None
        self.frames_sent = 0
        self.bytes_sent = 0
        self.min_frame_interval_ms: float | None = None
        self.max_frame_interval_ms = 0.0
        self._last_frame_sent_at: float | None = None
        self._listener: socket.socket | None = None
        self._connection: socket.socket | None = None

    def stop(self) -> None:
        self.stop_event.set()
        for sock in (self._connection, self._listener):
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _is_peer_disconnect(error: OSError) -> bool:
        return isinstance(
            error,
            (BrokenPipeError, ConnectionResetError, ConnectionAbortedError),
        ) or error.errno in (errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED)

    def _record_send(self) -> None:
        now = time.monotonic()
        if self._last_frame_sent_at is not None:
            interval_ms = (now - self._last_frame_sent_at) * 1000
            if self.min_frame_interval_ms is None:
                self.min_frame_interval_ms = interval_ms
            else:
                self.min_frame_interval_ms = min(
                    self.min_frame_interval_ms,
                    interval_ms,
                )
            self.max_frame_interval_ms = max(self.max_frame_interval_ms, interval_ms)
        self._last_frame_sent_at = now

    def run(self) -> None:
        try:
            self._run()
        except OSError as error:
            if not self.stop_event.is_set() and self._is_peer_disconnect(error):
                self.peer_disconnected = True
                self.terminal_reason = "receiver disconnected"
                LOG.info("receiver connection closed: %s", error)
                self.stop_event.set()
            elif not self.stop_event.is_set():
                self.failure = error
                self.terminal_reason = f"socket failure: {error}"
                LOG.error("socket writer failed: %s", error)
                self.stop_event.set()
        except BaseException as error:
            if not self.stop_event.is_set():
                self.failure = error
                self.terminal_reason = f"socket failure: {error}"
                LOG.error("socket writer failed: %s", error)
                self.stop_event.set()
        finally:
            self.connected.clear()
            self.ready.set()

    def _run(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            self._listener = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.host, self.port))
            listener.listen(1)
            listener.settimeout(0.5)
            self.ready.set()
            LOG.info("waiting for receiver on %s:%d", self.host, self.port)
            connection: socket.socket | None = None
            while not self.stop_event.is_set() and connection is None:
                try:
                    connection, address = listener.accept()
                    LOG.info("receiver connected from %s:%d", *address)
                except socket.timeout:
                    continue
            if connection is None:
                return

            self._connection = connection
            with connection:
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                connection.settimeout(3.0)
                self.connected.set()
                sequence = 0
                while not self.stop_event.is_set():
                    try:
                        frame = self.frames.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    packet = encode_frame(
                        frame.payload,
                        sequence,
                        frame.pts_ms,
                        codec=frame.codec,
                        flags=frame.flags,
                    )
                    connection.sendall(packet)
                    self._record_send()
                    sequence = (sequence + 1) & 0xFFFFFFFF
                    self.frames_sent += 1
                    self.bytes_sent += len(packet)

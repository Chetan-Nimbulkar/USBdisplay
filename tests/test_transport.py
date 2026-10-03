import threading
import time
import unittest
from unittest import mock

from bridge.protocol import CODEC_H264, CODEC_MJPEG
from bridge.transport import EncodedFrame, LatestFrameQueue, SocketWriter


def frame(codec: int, marker: bytes, keyframe: bool = False) -> EncodedFrame:
    return EncodedFrame(marker, 0, codec, keyframe=keyframe)


class LatestFrameQueueTests(unittest.TestCase):
    def test_keeps_latest_mjpeg_frame_when_congested(self) -> None:
        frames = LatestFrameQueue(1)
        frames.put(frame(CODEC_MJPEG, b"old", keyframe=True))

        self.assertTrue(frames.put(frame(CODEC_MJPEG, b"new", keyframe=True)))
        self.assertEqual(frames.get(0).payload, b"new")
        self.assertEqual(frames.dropped, 1)

    def test_h264_waits_for_capacity_without_dropping(self) -> None:
        frames = LatestFrameQueue(1)
        stop = threading.Event()
        self.assertTrue(frames.put(frame(CODEC_H264, b"idr", keyframe=True), stop))

        result: list[bool] = []
        writer = threading.Thread(
            target=lambda: result.append(frames.put(frame(CODEC_H264, b"p"), stop))
        )
        writer.start()

        deadline = time.monotonic() + 1
        while frames.blocked_puts == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(frames.blocked_puts, 1)
        self.assertTrue(writer.is_alive())
        self.assertEqual(frames.get(0).payload, b"idr")

        writer.join(timeout=1)
        self.assertFalse(writer.is_alive())
        self.assertEqual(result, [True])
        self.assertEqual(frames.get(0).payload, b"p")
        self.assertEqual(frames.dropped, 0)
        self.assertGreater(frames.blocked_seconds, 0)

    def test_h264_backpressure_stops_cleanly(self) -> None:
        frames = LatestFrameQueue(1)
        stop = threading.Event()
        frames.put(frame(CODEC_H264, b"idr", keyframe=True), stop)

        result: list[bool] = []
        writer = threading.Thread(
            target=lambda: result.append(frames.put(frame(CODEC_H264, b"p"), stop))
        )
        writer.start()
        deadline = time.monotonic() + 1
        while frames.blocked_puts == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        stop.set()
        writer.join(timeout=1)

        self.assertFalse(writer.is_alive())
        self.assertEqual(result, [False])
        self.assertEqual(frames.dropped, 0)


class SocketWriterLifecycleTests(unittest.TestCase):
    def test_broken_pipe_is_one_terminal_peer_disconnect(self) -> None:
        stop = threading.Event()
        writer = SocketWriter(LatestFrameQueue(1), stop, "127.0.0.1", 18958)

        with mock.patch.object(writer, "_run", side_effect=BrokenPipeError(32, "pipe")):
            writer.run()

        self.assertTrue(stop.is_set())
        self.assertTrue(writer.peer_disconnected)
        self.assertIsNone(writer.failure)
        self.assertEqual(writer.terminal_reason, "receiver disconnected")

    def test_unexpected_socket_error_remains_a_failure(self) -> None:
        stop = threading.Event()
        writer = SocketWriter(LatestFrameQueue(1), stop, "127.0.0.1", 18958)

        with mock.patch.object(writer, "_run", side_effect=TimeoutError("timed out")):
            writer.run()

        self.assertTrue(stop.is_set())
        self.assertFalse(writer.peer_disconnected)
        self.assertIsInstance(writer.failure, TimeoutError)


if __name__ == "__main__":
    unittest.main()

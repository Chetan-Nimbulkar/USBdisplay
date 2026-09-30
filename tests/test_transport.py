import threading
import time
import unittest

from bridge.protocol import CODEC_H264, CODEC_MJPEG
from bridge.transport import EncodedFrame, LatestFrameQueue


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


if __name__ == "__main__":
    unittest.main()

"""Persistent capture/encode producers for the USBDisplay transport queue."""

from __future__ import annotations

import collections
import logging
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .protocol import CODEC_H264, CODEC_MJPEG, MAX_PAYLOAD_LEN
from .transport import EncodedFrame, LatestFrameQueue

LOG = logging.getLogger("usbdisplay.capture")


def _start_codes(data: bytes | bytearray) -> list[tuple[int, int, int]]:
    """Return (start offset, NAL header offset, NAL type) tuples."""
    result: list[tuple[int, int, int]] = []
    index = 0
    length = len(data)
    marker = b"\x00\x00\x01"
    while index + 3 < length:
        found = data.find(marker, index)
        if found < 0:
            break
        four_byte = found > 0 and data[found - 1] == 0
        start = found - 1 if four_byte else found
        header = found + len(marker)
        if header < length:
            result.append((start, header, data[header] & 0x1F))
        index = header + 1
    return result


def access_unit_is_keyframe(payload: bytes) -> bool:
    return any(nal_type == 5 for _start, _header, nal_type in _start_codes(payload))


class AnnexBAccessUnitParser:
    """Split an Annex-B H.264 stream at Access Unit Delimiter NALs."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        if data:
            self._buffer.extend(data)
        nals = _start_codes(self._buffer)
        aud_offsets = [start for start, _header, nal_type in nals if nal_type == 9]
        if not aud_offsets:
            if len(self._buffer) > MAX_PAYLOAD_LEN:
                raise RuntimeError("H.264 stream exceeded the frame limit without an AUD")
            return []
        if aud_offsets[0] > 0:
            del self._buffer[: aud_offsets[0]]
            aud_offsets = [offset - aud_offsets[0] for offset in aud_offsets]
        if len(aud_offsets) < 2:
            return []

        frames = [
            bytes(self._buffer[start:end])
            for start, end in zip(aud_offsets, aud_offsets[1:])
        ]
        del self._buffer[: aud_offsets[-1]]
        for frame in frames:
            if len(frame) > MAX_PAYLOAD_LEN:
                raise RuntimeError("encoded H.264 access unit exceeds the protocol limit")
        return frames

    def finish(self) -> bytes | None:
        if not self._buffer:
            return None
        frame = bytes(self._buffer)
        self._buffer.clear()
        if not any(nal_type == 9 for _s, _h, nal_type in _start_codes(frame)):
            return None
        if len(frame) > MAX_PAYLOAD_LEN:
            raise RuntimeError("encoded H.264 access unit exceeds the protocol limit")
        return frame


class Producer(threading.Thread):
    def __init__(self, name: str, frames: LatestFrameQueue, stop: threading.Event) -> None:
        super().__init__(name=name, daemon=True)
        self.frames = frames
        self.stop_event = stop
        self.failure: BaseException | None = None
        self.frames_produced = 0

    def run(self) -> None:
        try:
            self.produce()
        except BaseException as error:
            if not self.stop_event.is_set():
                self.failure = error
                LOG.error("capture producer failed: %s", error)
                self.stop_event.set()

    def produce(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        self.stop_event.set()


class H264Producer(Producer):
    def __init__(
        self,
        frames: LatestFrameQueue,
        stop: threading.Event,
        output: str,
        fps: int,
        crf: int,
        constant_fps: bool,
    ) -> None:
        super().__init__("usbdisplay-h264-producer", frames, stop)
        self.output = output
        self.fps = fps
        self.crf = crf
        self.constant_fps = constant_fps
        self.process: subprocess.Popen[bytes] | None = None
        self.encoder_log: collections.deque[str] = collections.deque(maxlen=20)
        self.capture_name = "wf-recorder"

    def command(self) -> list[str]:
        x264_params = (
            f"keyint={self.fps}:min-keyint={self.fps}:scenecut=0:"
            "repeat-headers=1:aud=1"
        )
        args = [
            "wf-recorder",
            "-o",
            self.output,
            "-f",
            "pipe:1",
            "-m",
            "h264",
            "-c",
            "libx264",
            "-x",
            "yuv420p",
            "-r",
            str(self.fps),
            "-b",
            "0",
            "-p",
            "preset=ultrafast",
            "-p",
            "tune=zerolatency",
            "-p",
            "profile=baseline",
            "-p",
            f"crf={self.crf}",
            "-p",
            "threads=2",
            "-p",
            f"x264-params={x264_params}",
        ]
        if self.constant_fps:
            args.append("-D")
        return args

    def _drain_stderr(self, stream: object) -> None:
        if not hasattr(stream, "readline"):
            return
        while True:
            line = stream.readline()
            if not line:
                return
            decoded = line.decode(errors="replace").rstrip()
            self.encoder_log.append(decoded)
            LOG.debug("%s: %s", self.capture_name, decoded)

    def produce(self) -> None:
        LOG.info(
            "starting persistent H.264 capture backend=%s: %s",
            self.capture_name,
            " ".join(self.command()),
        )
        process = subprocess.Popen(
            self.command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.process = process
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(process.stderr,),
            name="usbdisplay-encoder-log",
            daemon=True,
        )
        stderr_thread.start()
        parser = AnnexBAccessUnitParser()
        started = time.monotonic()
        try:
            while not self.stop_event.is_set():
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                for payload in parser.feed(chunk):
                    frame = EncodedFrame(
                        payload=payload,
                        pts_ms=int((time.monotonic() - started) * 1000),
                        codec=CODEC_H264,
                        keyframe=access_unit_is_keyframe(payload),
                    )
                    if not self.frames.put(frame, self.stop_event):
                        break
                    self.frames_produced += 1
                if self.stop_event.is_set():
                    break
            tail = parser.finish()
            if tail and not self.stop_event.is_set():
                accepted = self.frames.put(
                    EncodedFrame(
                        payload=tail,
                        pts_ms=int((time.monotonic() - started) * 1000),
                        codec=CODEC_H264,
                        keyframe=access_unit_is_keyframe(tail),
                    ),
                    self.stop_event,
                )
                if accepted:
                    self.frames_produced += 1
        finally:
            self._stop_process()
            stderr_thread.join(timeout=1)
        if not self.stop_event.is_set() and process.returncode not in (0, None):
            details = " | ".join(self.encoder_log)
            raise RuntimeError(
                f"{self.capture_name} exited with {process.returncode}: {details}"
            )

    def _stop_process(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def stop(self) -> None:
        super().stop()
        self._stop_process()


class GnomePortalH264Producer(H264Producer):
    """Persistent H.264 produced from a GNOME portal PipeWire stream."""

    def __init__(
        self,
        frames: LatestFrameQueue,
        stop: threading.Event,
        helper: Path,
        width: int,
        height: int,
        fps: int,
        refresh: int,
        crf: int,
        python: str = sys.executable,
        cursor_mode: str = "auto",
    ) -> None:
        super().__init__(
            frames=frames,
            stop=stop,
            output="portal-virtual",
            fps=fps,
            crf=crf,
            constant_fps=True,
        )
        self.helper = helper
        self.width = width
        self.height = height
        self.refresh = refresh
        self.python = python
        self.capture_name = "gnome-portal"
        self.name = "usbdisplay-gnome-h264-producer"
        self.cursor_mode = cursor_mode

    def command(self) -> list[str]:
        return [
            self.python,
            str(self.helper),
            "--width",
            str(self.width),
            "--height",
            str(self.height),
            "--fps",
            str(self.fps),
            "--refresh",
            str(self.refresh),
            "--crf",
            str(self.crf),
            "--cursor-mode",
            self.cursor_mode,
        ]


class MjpegProducer(Producer):
    def __init__(
        self,
        frames: LatestFrameQueue,
        stop: threading.Event,
        output: str,
        fps: int,
        quality: int,
        include_cursor: bool,
    ) -> None:
        super().__init__("usbdisplay-mjpeg-producer", frames, stop)
        self.output = output
        self.fps = fps
        self.quality = quality
        self.include_cursor = include_cursor

    def _capture(self) -> bytes:
        args = ["grim", "-o", self.output, "-t", "jpeg", "-q", str(self.quality)]
        if self.include_cursor:
            args.append("-c")
        args.append("-")
        result = subprocess.run(
            args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if not result.stdout.startswith(b"\xff\xd8"):
            message = result.stderr.decode(errors="replace")
            raise RuntimeError(f"grim returned invalid JPEG data: {message}")
        return result.stdout

    def produce(self) -> None:
        interval = 1.0 / self.fps
        started = time.monotonic()
        deadline = started
        while not self.stop_event.is_set():
            payload = self._capture()
            accepted = self.frames.put(
                EncodedFrame(
                    payload=payload,
                    pts_ms=int((time.monotonic() - started) * 1000),
                    codec=CODEC_MJPEG,
                    keyframe=True,
                ),
                self.stop_event,
            )
            if accepted:
                self.frames_produced += 1
            deadline += interval
            delay = deadline - time.monotonic()
            if delay > 0:
                self.stop_event.wait(delay)
            else:
                deadline = time.monotonic()

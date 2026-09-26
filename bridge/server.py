#!/usr/bin/env python3
"""Stream a compositor-managed Wayland display to one USB-locked Android device."""

from __future__ import annotations

import argparse
from datetime import datetime
import logging
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bridge.compositors import CompositorBackend, detect_compositor, select_backend
    from bridge.device import DeviceLease, discover_devices, select_device
    from bridge.producers import Producer
    from bridge.transport import LatestFrameQueue, SocketWriter
else:
    from .compositors import CompositorBackend, detect_compositor, select_backend
    from .device import DeviceLease, discover_devices, select_device
    from .producers import Producer
    from .transport import LatestFrameQueue, SocketWriter

LOG = logging.getLogger("usbdisplay")
LOG_MAX_AGE_SECONDS = 5 * 24 * 60 * 60
LOG_MAX_TOTAL_BYTES = 30 * 1024 * 1024


def prune_logs(
    logs_dir: Path,
    *,
    now: float | None = None,
    max_age_seconds: int = LOG_MAX_AGE_SECONDS,
    max_total_bytes: int = LOG_MAX_TOTAL_BYTES,
) -> None:
    """Remove expired logs, then oldest logs until the directory is bounded."""
    if not logs_dir.exists():
        return
    current_time = time.time() if now is None else now
    retained: list[tuple[float, int, Path]] = []
    for path in logs_dir.glob("*.log"):
        try:
            stat = path.stat()
            if stat.st_mtime < current_time - max_age_seconds:
                path.unlink()
            else:
                retained.append((stat.st_mtime, stat.st_size, path))
        except FileNotFoundError:
            continue

    total_bytes = sum(size for _mtime, size, _path in retained)
    for _mtime, size, path in sorted(retained):
        if total_bytes <= max_total_bytes:
            break
        try:
            path.unlink()
            total_bytes -= size
        except FileNotFoundError:
            continue


def configure_logging(root: Path, debug: bool) -> tuple[Path, Path]:
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    prune_logs(logs_dir)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    log_path = logs_dir / f"usbdisplay-{timestamp}.log"
    formatter = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format=formatter,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
        force=True,
    )
    return logs_dir, log_path


def is_expected_shutdown_interrupt(
    error: BaseException,
    stop: threading.Event,
) -> bool:
    return (
        stop.is_set()
        and isinstance(error, subprocess.CalledProcessError)
        and error.returncode == -signal.SIGINT
    )


def ensure_receiver(lease: DeviceLease, apk: Path, install: bool) -> None:
    if install:
        lease.run("install", "-r", str(apk), timeout=60)
    package = lease.run(
        "shell",
        "pm",
        "path",
        "com.usbdisplay.receiver",
        capture=True,
    )
    if not package.stdout.strip():
        raise RuntimeError("USBDisplay receiver is not installed; run with --install")


def launch_receiver(lease: DeviceLease, width: int, height: int) -> None:
    lease.run("shell", "am", "force-stop", "com.usbdisplay.receiver", check=False)
    lease.run(
        "shell",
        "am",
        "start",
        "-n",
        "com.usbdisplay.receiver/.MainActivity",
        "--ez",
        "autoconnect",
        "true",
        "--ei",
        "stream_width",
        str(width),
        "--ei",
        "stream_height",
        str(height),
    )


def parse_resolution(value: str) -> tuple[int, int]:
    dimensions = value.lower().split("x")
    if len(dimensions) != 2:
        raise argparse.ArgumentTypeError("resolution must use WIDTHxHEIGHT")
    try:
        width, height = (int(dimension) for dimension in dimensions)
    except ValueError as error:
        raise argparse.ArgumentTypeError("resolution must use WIDTHxHEIGHT") from error
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("resolution dimensions must be positive")
    return width, height


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compositor",
        choices=("auto", "hyprland", "gnome", "kde"),
        default="auto",
        help="Wayland compositor backend (default: auto-detect)",
    )
    parser.add_argument("--output", default="USBDisplay")
    parser.add_argument("--resolution", type=parse_resolution, metavar="WIDTHxHEIGHT")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--refresh", type=int, default=30)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--codec", choices=("h264", "mjpeg"), default="h264")
    parser.add_argument("--h264-crf", type=int, default=23)
    parser.add_argument("--damage-aware", action="store_true")
    parser.add_argument("--quality", type=int, default=70, help="MJPEG quality")
    parser.add_argument(
        "--cursor-mode",
        choices=("embedded", "metadata", "hidden", "auto"),
        default="auto",
        help="Cursor capture mode: embedded (composited), metadata (separate stream), hidden (no cursor), auto (default)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18958)
    parser.add_argument("--queue-depth", type=int, default=2)
    parser.add_argument("--device-serial")
    parser.add_argument("--usb-path", help="physical ADB path, for example usb:1-1")
    parser.add_argument("--install", action="store_true", help="reinstall the bundled APK")
    parser.add_argument("--keep-output", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    if args.resolution is not None:
        if args.width is not None or args.height is not None:
            parser.error("--resolution cannot be combined with --width or --height")
        args.width, args.height = args.resolution
    else:
        args.width = 800 if args.width is None else args.width
        args.height = 600 if args.height is None else args.height
    return args


def validate_args(args: argparse.Namespace) -> None:
    if args.fps <= 0 or args.refresh <= 0:
        raise SystemExit("fps and refresh must be positive")
    if args.width <= 0 or args.height <= 0:
        raise SystemExit("width and height must be positive")
    if args.width % 2 or args.height % 2:
        raise SystemExit("H.264/MJPEG width and height must be even")
    if args.width > 4096 or args.height > 4096:
        raise SystemExit("width and height must not exceed 4096 pixels")
    if args.width * args.height > 4096 * 2160:
        raise SystemExit("resolution must not exceed 4K pixel count")
    if args.quality not in range(1, 101):
        raise SystemExit("quality must be between 1 and 100")
    if args.h264_crf not in range(0, 52):
        raise SystemExit("H.264 CRF must be between 0 and 51")
    if args.queue_depth not in range(1, 5):
        raise SystemExit("queue depth must be between 1 and 4")
    if args.cursor_mode not in ("embedded", "metadata", "hidden", "auto"):
        raise SystemExit("cursor-mode must be one of: embedded, metadata, hidden, auto")


def run_session(
    args: argparse.Namespace,
    stop: threading.Event,
    apk: Path,
    backend: CompositorBackend,
) -> None:
    device = select_device(
        discover_devices(),
        serial=args.device_serial,
        usb_path=args.usb_path,
    )
    writer: SocketWriter | None = None
    producer: Producer | None = None
    started = time.monotonic()
    try:
        with DeviceLease(device) as lease:
            LOG.info("locked Android device %s", device.label)
            try:
                backend.prepare_output(
                    args.output,
                    args.width,
                    args.height,
                    args.refresh,
                )
                ensure_receiver(lease, apk, args.install)
                lease.configure_reverse(args.port)

                frames = LatestFrameQueue(args.queue_depth)
                writer = SocketWriter(frames, stop, args.host, args.port)
                writer.start()
                if not writer.ready.wait(timeout=5) or writer.failure:
                    raise RuntimeError(f"socket listener failed: {writer.failure}")

                launch_receiver(lease, args.width, args.height)
                connection_deadline = time.monotonic() + 10
                while not stop.is_set() and not writer.connected.wait(timeout=0.5):
                    lease.validate()
                    if time.monotonic() >= connection_deadline:
                        raise RuntimeError("Android receiver did not connect within 10 seconds")
                if stop.is_set():
                    return

                producer = backend.create_producer(
                    codec=args.codec,
                    frames=frames,
                    stop=stop,
                    output=args.output,
                    width=args.width,
                    height=args.height,
                    fps=args.fps,
                    refresh=args.refresh,
                    crf=args.h264_crf,
                    constant_fps=not args.damage_aware,
                    quality=args.quality,
                    include_cursor=args.cursor_mode != "hidden",
                    cursor_mode=args.cursor_mode,
                )
                producer.start()
                LOG.info(
                    "streaming codec=%s output=%s %dx%d@%d to %s",
                    args.codec,
                    args.output,
                    args.width,
                    args.height,
                    args.fps,
                    device.label,
                )

                previous_frames = 0
                previous_bytes = 0
                previous_report = time.monotonic()
                while not stop.wait(1):
                    lease.validate()
                    if writer.failure:
                        raise RuntimeError(f"socket writer failed: {writer.failure}")
                    if producer.failure:
                        raise RuntimeError(f"capture producer failed: {producer.failure}")
                    if not writer.is_alive():
                        raise RuntimeError("socket writer stopped unexpectedly")
                    if not producer.is_alive():
                        raise RuntimeError("capture producer stopped unexpectedly")
                    now = time.monotonic()
                    if now - previous_report >= 5:
                        elapsed = now - previous_report
                        sent = writer.frames_sent - previous_frames
                        sent_bytes = writer.bytes_sent - previous_bytes
                        LOG.info(
                            "stats fps=%.1f bitrate=%.2fMbps encoded=%d sent=%d "
                            "dropped=%d backpressure=%d blocked=%.2fs queue=%d/%d",
                            sent / elapsed,
                            sent_bytes * 8 / elapsed / 1_000_000,
                            producer.frames_produced,
                            writer.frames_sent,
                            frames.dropped,
                            frames.blocked_puts,
                            frames.blocked_seconds,
                            frames.depth,
                            frames.capacity,
                        )
                        previous_frames = writer.frames_sent
                        previous_bytes = writer.bytes_sent
                        previous_report = now

                if writer.failure:
                    raise RuntimeError(f"socket writer failed: {writer.failure}")
                if producer.failure:
                    raise RuntimeError(f"capture producer failed: {producer.failure}")
            finally:
                stop.set()
                if producer is not None:
                    producer.stop()
                    producer.join(timeout=5)
                if writer is not None:
                    writer.stop()
                    writer.join(timeout=5)
                    elapsed = max(time.monotonic() - started, 0.001)
                    LOG.info(
                        "session finished frames=%d average_bitrate=%.2fMbps "
                        "dropped=%d backpressure=%d blocked=%.2fs max_queue=%d/%d",
                        writer.frames_sent,
                        writer.bytes_sent * 8 / elapsed / 1_000_000,
                        frames.dropped,
                        frames.blocked_puts,
                        frames.blocked_seconds,
                        frames.max_depth,
                        frames.capacity,
                    )
                lease.remove_reverse(args.port)
                lease.run(
                    "shell",
                    "am",
                    "force-stop",
                    "com.usbdisplay.receiver",
                    check=False,
                    timeout=5,
                )
    finally:
        backend.cleanup_output(args.output, args.keep_output)


def main() -> int:
    args = parse_args()
    validate_args(args)
    root = Path(__file__).resolve().parents[1]
    logs_dir, log_path = configure_logging(root, args.debug)
    LOG.info("session log %s", log_path)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    exit_code = 0
    try:
        compositor = detect_compositor(args.compositor)
        backend = select_backend(compositor)
        backend.validate(args.codec)
        LOG.info("selected compositor backend=%s", backend.name)
        run_session(args, stop, root / "android" / "app-debug.apk", backend)
    except (RuntimeError, subprocess.SubprocessError) as error:
        if is_expected_shutdown_interrupt(error, stop):
            LOG.info("shutdown interrupted an in-flight device validation")
        else:
            LOG.error("%s", error)
            exit_code = 1
    finally:
        LOG.info("log saved to %s", log_path)
        logging.shutdown()
        prune_logs(logs_dir)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

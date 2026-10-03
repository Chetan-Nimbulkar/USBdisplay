#!/usr/bin/env python3
"""Stream a compositor-managed Wayland display to one USB-locked Android device."""

from __future__ import annotations

import argparse
from datetime import datetime
import logging
import re
import signal
import shutil
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
VERSION = "0.95-Beta"
LOG_MAX_AGE_SECONDS = 5 * 24 * 60 * 60
LOG_MAX_TOTAL_BYTES = 30 * 1024 * 1024
AUTO_RESOLUTION_MAX_EDGE = 1280
PHYSICAL_SIZE_PATTERN = re.compile(r"^Physical size:\s*(\d+)x(\d+)\s*$", re.MULTILINE)
SURFACE_ORIENTATION_PATTERN = re.compile(r"SurfaceOrientation:\s*([0-3])")


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


class TerminalFilter(logging.Filter):
    """Keep normal terminal output concise while retaining full file telemetry."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or bool(
            getattr(record, "terminal", False)
        )


def terminal_info(message: str, *args: object) -> None:
    LOG.info(message, *args, extra={"terminal": True})


def configure_logging(root: Path, debug: bool) -> tuple[Path, Path]:
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    prune_logs(logs_dir)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    log_path = logs_dir / f"usbdisplay-{timestamp}.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(formatter)
    if not debug:
        console.addFilter(TerminalFilter())
    session_file = logging.FileHandler(log_path, encoding="utf-8")
    session_file.setLevel(logging.DEBUG)
    session_file.setFormatter(formatter)
    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[console, session_file],
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


def receiver_activity_running(lease: DeviceLease) -> bool:
    result = lease.run(
        "shell",
        "dumpsys",
        "activity",
        "activities",
        capture=True,
        check=False,
        timeout=5,
    )
    return "com.usbdisplay.receiver/.MainActivity" in result.stdout


def stop_receiver(lease: DeviceLease) -> None:
    """Allow EOF cleanup, then remove any cached receiver process state."""
    deadline = time.monotonic() + 2
    graceful = False
    while time.monotonic() < deadline:
        if not receiver_activity_running(lease):
            graceful = True
            LOG.info("Android receiver Activity exited after socket closure")
            break
        time.sleep(0.1)
    if not graceful:
        LOG.warning("Android receiver did not exit after EOF; forcing session teardown")
    lease.run(
        "shell",
        "am",
        "force-stop",
        "com.usbdisplay.receiver",
        check=False,
        timeout=5,
    )
    LOG.info("Android receiver package stopped; no cached session process retained")


def launch_receiver(lease: DeviceLease, width: int, height: int, rotation: int) -> None:
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
        "--ei",
        "session_rotation",
        str(rotation),
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


def parse_physical_size(output: str) -> tuple[int, int]:
    """Extract the immutable panel dimensions from Android's `wm size` output."""
    match = PHYSICAL_SIZE_PATTERN.search(output)
    if match is None:
        raise RuntimeError("Android did not report a physical display size")
    width, height = (int(dimension) for dimension in match.groups())
    if width <= 0 or height <= 0:
        raise RuntimeError("Android reported an invalid physical display size")
    return width, height


def read_device_rotation(lease: DeviceLease) -> int:
    result = lease.run("shell", "dumpsys", "input", capture=True, timeout=10)
    match = SURFACE_ORIENTATION_PATTERN.search(result.stdout)
    if match is None:
        raise RuntimeError("Android did not report its current display orientation")
    rotation = int(match.group(1))
    LOG.info("Android starting orientation rotation=%d", rotation)
    return rotation


def compatible_resolution(
    panel_width: int,
    panel_height: int,
    *,
    vertical: bool,
    max_edge: int = AUTO_RESOLUTION_MAX_EDGE,
) -> tuple[int, int]:
    """Preserve panel aspect ratio while avoiding host/USB-expensive upscaling."""
    long_edge = max(panel_width, panel_height)
    short_edge = min(panel_width, panel_height)
    scale = min(1.0, max_edge / long_edge)
    selected_long = max(2, int(long_edge * scale) // 2 * 2)
    selected_short = max(2, int(short_edge * scale) // 2 * 2)
    if vertical:
        return selected_short, selected_long
    return selected_long, selected_short


def resolve_auto_resolution(
    args: argparse.Namespace,
    lease: DeviceLease,
    rotation: int,
) -> None:
    if args.width is not None and args.height is not None:
        LOG.info("using explicit resolution %dx%d", args.width, args.height)
        return
    size = lease.run("shell", "wm", "size", capture=True, timeout=5)
    panel_width, panel_height = parse_physical_size(size.stdout)
    vertical = args.vertical or rotation in (0, 2)
    args.width, args.height = compatible_resolution(
        panel_width,
        panel_height,
        vertical=vertical,
    )
    orientation = "portrait" if vertical else "landscape"
    LOG.info(
        "Android panel=%dx%d; selected automatic %s resolution=%dx%d",
        panel_width,
        panel_height,
        orientation,
        args.width,
        args.height,
    )


def orientation_name(rotation: int) -> str:
    return {
        0: "portrait",
        1: "landscape",
        2: "reverse portrait",
        3: "reverse landscape",
    }.get(rotation, f"unknown ({rotation})")


def run_preflight(args: argparse.Namespace, root: Path) -> int:
    """Print a read-only host/device compatibility report."""
    compositor = detect_compositor(args.compositor)
    backend = select_backend(compositor)
    print(f"USBDisplay {VERSION} compatibility check")
    print(f"Session: {compositor} Wayland")
    print(f"Backend: {backend.name.upper()}")

    programs = backend.required_programs(args.codec)
    print("Host tools:")
    missing_programs: list[str] = []
    for program in programs:
        path = shutil.which(program)
        if path is None:
            missing_programs.append(program)
            print(f"  {program}: MISSING")
        else:
            print(f"  {program}: OK ({path})")

    backend_error: str | None = None
    try:
        backend.validate(args.codec)
    except RuntimeError as error:
        backend_error = str(error)
    if backend_error is None:
        print("Backend readiness: OK")
    else:
        print(f"Backend readiness: UNAVAILABLE ({backend_error})")

    apk = root / "android" / f"USBdisplay-{VERSION}.apk"
    print(f"Bundled receiver APK: {'OK' if apk.is_file() else 'MISSING'} ({apk})")

    device = select_device(
        discover_devices(),
        serial=args.device_serial,
        usb_path=args.usb_path,
    )
    with DeviceLease(device) as lease:
        rotation = read_device_rotation(lease)
        size = lease.run("shell", "wm", "size", capture=True, timeout=5)
        panel_width, panel_height = parse_physical_size(size.stdout)
        receiver = lease.run(
            "shell",
            "pm",
            "path",
            "com.usbdisplay.receiver",
            capture=True,
            check=False,
            timeout=5,
        )

    landscape = compatible_resolution(panel_width, panel_height, vertical=False)
    portrait = compatible_resolution(panel_width, panel_height, vertical=True)
    current = portrait if rotation in (0, 2) else landscape
    print("Android device:")
    print(f"  Model: {device.model or 'unknown'}")
    print(f"  Serial: {device.serial}")
    print(f"  Physical USB: usb:{device.usb_path}")
    print(f"  Physical panel: {panel_width}x{panel_height}")
    print(f"  Current orientation: {orientation_name(rotation)} (rotation {rotation})")
    print(f"  Receiver installed: {'YES' if receiver.stdout.strip() else 'NO'}")
    print("Recommended stream modes:")
    print(f"  Current orientation: {current[0]}x{current[1]}")
    print(f"  Landscape: {landscape[0]}x{landscape[1]}")
    print(f"  Portrait: {portrait[0]}x{portrait[1]}")
    if args.width is not None and args.height is not None:
        print(f"  Command-line override: {args.width}x{args.height}")
    print("Audio route: not supported in v0.95-Beta")

    ready = (
        not missing_programs
        and backend_error is None
        and apk.is_file()
        and bool(receiver.stdout.strip())
    )
    print(f"Overall readiness: {'READY' if ready else 'NOT READY'}")
    return 0 if ready else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"USBDisplay {VERSION}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="inspect host, USB device, receiver, and recommended modes without starting",
    )
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
    parser.add_argument(
        "--vertical",
        action="store_true",
        help="select the automatic portrait resolution",
    )
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
        if args.vertical:
            parser.error("--resolution cannot be combined with --vertical")
        args.width, args.height = args.resolution
    elif args.width is not None or args.height is not None:
        if args.vertical:
            parser.error("--width/--height cannot be combined with --vertical")
        args.width = 800 if args.width is None else args.width
        args.height = 600 if args.height is None else args.height
    return args


def validate_args(args: argparse.Namespace) -> None:
    if args.fps <= 0 or args.refresh <= 0:
        raise SystemExit("fps and refresh must be positive")
    if (args.width is None) != (args.height is None):
        raise SystemExit("width and height must be provided together")
    if args.width is not None and args.height is not None:
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
            terminal_info("Device: %s", device.model or device.serial)
            try:
                rotation = read_device_rotation(lease)
                resolve_auto_resolution(args, lease, rotation)
                validate_args(args)
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

                launch_receiver(lease, args.width, args.height, rotation)
                connection_deadline = time.monotonic() + 10
                while not stop.is_set() and not writer.connected.wait(timeout=0.5):
                    lease.validate()
                    if time.monotonic() >= connection_deadline:
                        raise RuntimeError("Android receiver did not connect within 10 seconds")
                if stop.is_set():
                    return
                terminal_info("Receiver connected")

                backend.confirm_output(
                    args.output,
                    args.width,
                    args.height,
                    args.refresh,
                )

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
                terminal_info(
                    "Display: %dx%d @ %d FPS",
                    args.width,
                    args.height,
                    args.fps,
                )
                terminal_info("Streaming")
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
                previous_encoded = 0
                previous_bytes = 0
                previous_report = time.monotonic()
                next_output_check = previous_report + 2
                while not stop.wait(1):
                    lease.validate()
                    now = time.monotonic()
                    if writer.failure:
                        raise RuntimeError(f"socket writer failed: {writer.failure}")
                    if producer.failure:
                        raise RuntimeError(f"capture producer failed: {producer.failure}")
                    if not writer.is_alive():
                        raise RuntimeError("socket writer stopped unexpectedly")
                    if not producer.is_alive():
                        raise RuntimeError("capture producer stopped unexpectedly")
                    if now >= next_output_check:
                        backend.validate_output(
                            args.output,
                            args.width,
                            args.height,
                            args.refresh,
                        )
                        next_output_check = now + 2
                    if now - previous_report >= 5:
                        elapsed = now - previous_report
                        sent = writer.frames_sent - previous_frames
                        encoded = producer.frames_produced - previous_encoded
                        sent_bytes = writer.bytes_sent - previous_bytes
                        LOG.info(
                            "stats sent_fps=%.1f encoded_fps=%.1f "
                            "bitrate=%.2fMbps encoded=%d sent=%d dropped=%d "
                            "backpressure=%d blocked=%.2fs queue=%d/%d "
                            "send_gap_min=%.1fms send_gap_max=%.1fms",
                            sent / elapsed,
                            encoded / elapsed,
                            sent_bytes * 8 / elapsed / 1_000_000,
                            producer.frames_produced,
                            writer.frames_sent,
                            frames.dropped,
                            frames.blocked_puts,
                            frames.blocked_seconds,
                            frames.depth,
                            frames.capacity,
                            writer.min_frame_interval_ms or 0.0,
                            writer.max_frame_interval_ms,
                        )
                        previous_frames = writer.frames_sent
                        previous_encoded = producer.frames_produced
                        previous_bytes = writer.bytes_sent
                        previous_report = now

                if writer.failure:
                    raise RuntimeError(f"socket writer failed: {writer.failure}")
                if producer.failure:
                    raise RuntimeError(f"capture producer failed: {producer.failure}")
            finally:
                terminal_info("Stopping")
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
                        "dropped=%d backpressure=%d blocked=%.2fs max_queue=%d/%d "
                        "send_gap_min=%.1fms send_gap_max=%.1fms reason=%s",
                        writer.frames_sent,
                        writer.bytes_sent * 8 / elapsed / 1_000_000,
                        frames.dropped,
                        frames.blocked_puts,
                        frames.blocked_seconds,
                        frames.max_depth,
                        frames.capacity,
                        writer.min_frame_interval_ms or 0.0,
                        writer.max_frame_interval_ms,
                        writer.terminal_reason or "host stop",
                    )
                lease.remove_reverse(args.port)
                stop_receiver(lease)
                LOG.info("Android receiver stopped; Activity orientation lock released")
                terminal_info("Orientation restored")
    finally:
        backend.cleanup_output(args.output, args.keep_output)


def main() -> int:
    args = parse_args()
    validate_args(args)
    root = Path(__file__).resolve().parents[1]
    if args.check:
        try:
            return run_preflight(args, root)
        except (RuntimeError, subprocess.SubprocessError) as error:
            print(f"Check failed: {error}", file=sys.stderr)
            return 1

    logs_dir, log_path = configure_logging(root, args.debug)
    LOG.info("session log %s", log_path)
    terminal_info("USBDisplay v%s", VERSION)
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
        terminal_info("Backend: %s", backend.name.upper())
        run_session(args, stop, root / "android" / "USBdisplay-0.95-Beta.apk", backend)
    except (RuntimeError, subprocess.SubprocessError) as error:
        if is_expected_shutdown_interrupt(error, stop):
            LOG.info("shutdown interrupted an in-flight device validation")
        else:
            LOG.error("%s", error)
            exit_code = 1
    finally:
        if exit_code == 0:
            terminal_info("Stopped cleanly")
        LOG.info("log saved to %s", log_path)
        logging.shutdown()
        prune_logs(logs_dir)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

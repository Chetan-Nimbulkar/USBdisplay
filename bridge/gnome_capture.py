#!/usr/bin/env python3
"""GNOME portal capture with clock-driven persistent Annex-B H.264 output."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
import signal
import sys
import threading
import time
import uuid

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402


BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_INTERFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"
SOURCE_VIRTUAL = 4
CURSOR_HIDDEN = 1
CURSOR_EMBEDDED = 2
CURSOR_METADATA = 4


def cursor_mode_to_value(mode: str) -> int:
    """Convert cursor mode string to portal cursor mode value."""
    return {
        "embedded": CURSOR_EMBEDDED,
        "metadata": CURSOR_METADATA,
        "hidden": CURSOR_HIDDEN,
        "auto": CURSOR_EMBEDDED,
    }.get(mode, CURSOR_METADATA)


def token(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class PortalError(RuntimeError):
    pass


class ScreenCastPortal:
    def __init__(self, timeout_seconds: int = 120, cursor_mode: str = "auto") -> None:
        self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.proxy = Gio.DBusProxy.new_sync(
            self.connection,
            Gio.DBusProxyFlags.NONE,
            None,
            BUS_NAME,
            OBJECT_PATH,
            SCREENCAST_INTERFACE,
            None,
        )
        self.timeout_seconds = timeout_seconds
        self.session_path: str | None = None
        self.cursor_mode = cursor_mode
        source_types = self.proxy.get_cached_property("AvailableSourceTypes")
        if source_types is not None and not int(source_types.unpack()) & SOURCE_VIRTUAL:
            raise PortalError("the active ScreenCast portal does not support virtual monitors")
        cursor_modes = self.proxy.get_cached_property("AvailableCursorModes")
        if cursor_modes is not None:
            available = int(cursor_modes.unpack())
            cursor_value = cursor_mode_to_value(cursor_mode)
            if not available & cursor_value:
                raise PortalError(
                    f"the active ScreenCast portal does not support cursor mode '{cursor_mode}'"
                )

    def _request(self, method: str, parameters: GLib.Variant) -> dict[str, object]:
        loop = GLib.MainLoop()
        response: dict[str, object] = {}
        expected_path: list[str | None] = [None]
        pending_responses: dict[str, tuple[int, object]] = {}
        timed_out = [False]

        def on_response(
            _connection: Gio.DBusConnection,
            _sender: str,
            object_path: str,
            _interface: str,
            _signal: str,
            values: GLib.Variant,
            _data: object,
        ) -> None:
            status, results = values.unpack()
            pending_responses[object_path] = (status, results)
            if object_path != expected_path[0]:
                return
            response["status"] = status
            response["results"] = results
            loop.quit()

        subscription = self.connection.signal_subscribe(
            BUS_NAME,
            REQUEST_INTERFACE,
            "Response",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
            None,
        )

        def on_timeout() -> bool:
            timed_out[0] = True
            loop.quit()
            return GLib.SOURCE_REMOVE

        timeout_id = GLib.timeout_add_seconds(self.timeout_seconds, on_timeout)
        try:
            result = self.proxy.call_sync(
                method,
                parameters,
                Gio.DBusCallFlags.NONE,
                30_000,
                None,
            )
            expected_path[0] = result.unpack()[0]
            early_response = pending_responses.get(expected_path[0])
            if early_response is not None:
                response["status"], response["results"] = early_response
            else:
                loop.run()
        finally:
            self.connection.signal_unsubscribe(subscription)
            if not timed_out[0]:
                GLib.source_remove(timeout_id)

        if timed_out[0]:
            raise PortalError(f"portal {method} request timed out")
        status = int(response.get("status", 2))
        if status != 0:
            reason = "cancelled" if status == 1 else f"failed with response {status}"
            raise PortalError(f"portal {method} request was {reason}")
        return response.get("results", {})  # type: ignore[return-value]

    def create_session(self) -> str:
        results = self._request(
            "CreateSession",
            GLib.Variant(
                "(a{sv})",
                (
                    {
                        "handle_token": GLib.Variant("s", token("create")),
                        "session_handle_token": GLib.Variant("s", token("session")),
                    },
                ),
            ),
        )
        session = results["session_handle"]
        if isinstance(session, GLib.Variant):
            session = session.unpack()
        self.session_path = str(session)
        return self.session_path

    def select_virtual_source(self) -> None:
        assert self.session_path is not None
        self._request(
            "SelectSources",
            GLib.Variant(
                "(oa{sv})",
                (
                    self.session_path,
                    {
                        "handle_token": GLib.Variant("s", token("select")),
                        "types": GLib.Variant("u", SOURCE_VIRTUAL),
                        "multiple": GLib.Variant("b", False),
                        "cursor_mode": GLib.Variant(
                            "u", cursor_mode_to_value(self.cursor_mode)
                        ),
                    },
                ),
            ),
        )

    def start(self) -> int:
        assert self.session_path is not None
        results = self._request(
            "Start",
            GLib.Variant(
                "(osa{sv})",
                (
                    self.session_path,
                    "",
                    {"handle_token": GLib.Variant("s", token("start"))},
                ),
            ),
        )
        streams = results["streams"]
        if isinstance(streams, GLib.Variant):
            streams = streams.unpack()
        if len(streams) != 1:  # type: ignore[arg-type]
            raise PortalError(
                f"portal returned {len(streams)} streams, expected one"  # type: ignore[arg-type]
            )
        return int(streams[0][0])  # type: ignore[index]

    def open_pipewire_remote(self) -> int:
        assert self.session_path is not None
        result, fd_list = self.proxy.call_with_unix_fd_list_sync(
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session_path, {})),
            Gio.DBusCallFlags.NONE,
            30_000,
            None,
            None,
        )
        handle = int(result.unpack()[0])
        fd = fd_list.get(handle)
        if fd < 0:
            raise PortalError("portal returned an invalid PipeWire file descriptor")
        return fd

    def close(self) -> None:
        if self.session_path is None:
            return
        try:
            self.connection.call_sync(
                BUS_NAME,
                self.session_path,
                SESSION_INTERFACE,
                "Close",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                5_000,
                None,
            )
        except GLib.Error as error:
            print(f"portal close warning: {error}", file=sys.stderr, flush=True)
        self.session_path = None


def watch_parent_exit(parent_pid: int, loop: GLib.MainLoop) -> None:
    """Exit if the orchestrator died without stopping us."""

    def _watch() -> None:
        while True:
            time.sleep(1)
            if os.getppid() != parent_pid:
                print(
                    "orchestrator gone; closing portal session",
                    file=sys.stderr,
                    flush=True,
                )
                loop.quit()
                time.sleep(2)
                os._exit(3)

    threading.Thread(
        target=_watch,
        name="usbdisplay-parent-watch",
        daemon=True,
    ).start()


def source_caps() -> str:
    # Unconstrained on purpose: the virtual node rejects fixed pins.
    return "video/x-raw"


@dataclass(frozen=True)
class CachedFrame:
    payload: bytes
    generation: int
    source_ns: int


class LatestFrameCache:
    """Thread-safe one-frame cache backed by application-owned bytes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._available = threading.Event()
        self._frame: CachedFrame | None = None
        self._generation = 0

    def replace(self, payload: bytes, source_ns: int) -> CachedFrame:
        with self._lock:
            self._generation += 1
            self._frame = CachedFrame(payload, self._generation, source_ns)
            frame = self._frame
        self._available.set()
        return frame

    def wait(self, timeout: float) -> bool:
        return self._available.wait(timeout)

    def snapshot(self) -> CachedFrame | None:
        with self._lock:
            return self._frame


class CadenceMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.source_frames = 0
        self.output_frames = 0
        self.new_frames = 0
        self.repeated_frames = 0
        self.first_source_ns: int | None = None
        self.first_output_ns: int | None = None
        self.last_output_ns: int | None = None
        self.max_output_gap_ms = 0.0

    def add_source(self, stamp_ns: int) -> int:
        with self._lock:
            self.source_frames += 1
            if self.first_source_ns is None:
                self.first_source_ns = stamp_ns
            return self.source_frames

    def add_output(self, stamp_ns: int, repeated: bool) -> None:
        with self._lock:
            if self.first_output_ns is None:
                self.first_output_ns = stamp_ns
            if self.last_output_ns is not None:
                gap_ms = (stamp_ns - self.last_output_ns) / 1_000_000
                self.max_output_gap_ms = max(self.max_output_gap_ms, gap_ms)
            self.last_output_ns = stamp_ns
            self.output_frames += 1
            if repeated:
                self.repeated_frames += 1
            else:
                self.new_frames += 1

    def snapshot(self) -> tuple[int, int, int, int, float, float | None]:
        with self._lock:
            startup_ms = (
                (self.first_output_ns - self.first_source_ns) / 1_000_000
                if self.first_source_ns is not None
                and self.first_output_ns is not None
                else None
            )
            return (
                self.source_frames,
                self.output_frames,
                self.new_frames,
                self.repeated_frames,
                self.max_output_gap_ms,
                startup_ms,
            )


def raw_caps(args: argparse.Namespace) -> str:
    return (
        f"video/x-raw,format=I420,width={args.width},height={args.height},"
        "pixel-aspect-ratio=1/1"
    )


def build_source_pipeline(
    fd: int,
    node_id: int,
    args: argparse.Namespace,
) -> Gst.Pipeline:
    pipeline = Gst.parse_launch(
        " ".join(
            [
                f"pipewiresrc fd={fd} path={node_id} do-timestamp=true",
                f"! {source_caps()}",
                "! videoconvert",
                "! videoscale",
                f"! {raw_caps(args)}",
                "! appsink name=source_sink emit-signals=true max-buffers=1 drop=true sync=false",
            ]
        )
    )
    if not isinstance(pipeline, Gst.Pipeline):
        raise RuntimeError("GStreamer did not create the source pipeline")
    return pipeline


def build_output_pipeline(args: argparse.Namespace) -> Gst.Pipeline:
    pipeline = Gst.parse_launch(
        " ".join(
            [
                "appsrc name=output_source is-live=true format=time block=true max-buffers=2",
                f"caps={raw_caps(args)},framerate={args.fps}/1",
                "! x264enc",
                "byte-stream=true",
                "aud=true",
                "tune=zerolatency",
                "speed-preset=ultrafast",
                f"key-int-max={args.fps}",
                "pass=qual",
                f"quantizer={args.crf}",
                "threads=1",
                f'option-string="repeat-headers=1:scenecut=0:min-keyint={args.fps}"',
                "! video/x-h264,stream-format=byte-stream,alignment=au,profile=baseline",
                "! h264parse config-interval=-1",
                "! fdsink fd=1 sync=false",
            ]
        )
    )
    if not isinstance(pipeline, Gst.Pipeline):
        raise RuntimeError("GStreamer did not create the output pipeline")
    return pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--refresh", type=int, required=True)
    parser.add_argument("--crf", type=int, required=True)
    parser.add_argument(
        "--cursor-mode",
        choices=("embedded", "metadata", "hidden", "auto"),
        default="auto",
        help="Cursor capture mode: embedded, metadata, hidden, or auto",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    Gst.init(None)
    portal = ScreenCastPortal(cursor_mode=args.cursor_mode)
    source_pipeline: Gst.Pipeline | None = None
    output_pipeline: Gst.Pipeline | None = None
    pipewire_fd = -1
    loop = GLib.MainLoop()
    stop_event = threading.Event()
    cache = LatestFrameCache()
    metrics = CadenceMetrics()
    scheduler: threading.Thread | None = None
    watch_parent_exit(os.getppid(), loop)
    exit_code = 0

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()
        loop.quit()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        portal.create_session()
        portal.select_virtual_source()
        node_id = portal.start()
        pipewire_fd = portal.open_pipewire_remote()
        print(
            f"GNOME portal stream node={node_id} "
            f"{args.width}x{args.height}@{args.refresh} capture={args.fps}fps",
            file=sys.stderr,
            flush=True,
        )

        source_pipeline = build_source_pipeline(pipewire_fd, node_id, args)
        output_pipeline = build_output_pipeline(args)
        source_sink = source_pipeline.get_by_name("source_sink")
        output_source = output_pipeline.get_by_name("output_source")
        if source_sink is None or output_source is None:
            raise RuntimeError("required named GStreamer element missing")

        def on_sample(sink: Gst.Element) -> Gst.FlowReturn:
            sample = sink.emit("pull-sample")
            if sample is None:
                return Gst.FlowReturn.ERROR
            buffer = sample.get_buffer()
            if buffer is None:
                return Gst.FlowReturn.ERROR
            stamp_ns = time.monotonic_ns()
            payload = buffer.extract_dup(0, buffer.get_size())
            frame = cache.replace(payload, stamp_ns)
            if metrics.add_source(stamp_ns) == 1:
                print(
                    f"GNOME first source frame generation={frame.generation} "
                    f"bytes={len(payload)}",
                    file=sys.stderr,
                    flush=True,
                )
            return Gst.FlowReturn.OK

        source_sink.connect("new-sample", on_sample)

        def on_message(
            _bus: Gst.Bus,
            message: Gst.Message,
            pipeline_name: str,
        ) -> None:
            nonlocal exit_code
            if message.type == Gst.MessageType.ERROR:
                error, details = message.parse_error()
                print(
                    f"GStreamer {pipeline_name} error: {error}: {details}",
                    file=sys.stderr,
                    flush=True,
                )
                exit_code = 1
                stop_event.set()
                loop.quit()
            elif message.type == Gst.MessageType.EOS and pipeline_name == "source":
                if not stop_event.is_set():
                    print("GStreamer source ended unexpectedly", file=sys.stderr, flush=True)
                    exit_code = 1
                    stop_event.set()
                loop.quit()

        for pipeline_name, pipeline in (
            ("source", source_pipeline),
            ("output", output_pipeline),
        ):
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", on_message, pipeline_name)

        if output_pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer output pipeline failed to enter PLAYING state")
        if source_pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer source pipeline failed to enter PLAYING state")

        def run_scheduler() -> None:
            nonlocal exit_code
            while not stop_event.is_set() and not cache.wait(0.1):
                pass
            if stop_event.is_set():
                return

            period_ns = 1_000_000_000 / args.fps
            start_ns = time.monotonic_ns()
            report_ns = start_ns
            report_source = 0
            report_output = 0
            last_generation: int | None = None
            frame_index = 0
            while not stop_event.is_set():
                deadline_ns = start_ns + round(frame_index * period_ns)
                remaining_ns = deadline_ns - time.monotonic_ns()
                if remaining_ns > 0:
                    stop_event.wait(remaining_ns / 1_000_000_000)
                    if stop_event.is_set():
                        break

                frame = cache.snapshot()
                if frame is None:
                    continue
                buffer = Gst.Buffer.new_allocate(None, len(frame.payload), None)
                buffer.fill(0, frame.payload)
                clock = output_pipeline.get_clock()
                if clock is not None:
                    buffer.pts = max(
                        0,
                        clock.get_time() - output_pipeline.get_base_time(),
                    )
                else:
                    buffer.pts = round(frame_index * Gst.SECOND / args.fps)
                buffer.dts = Gst.CLOCK_TIME_NONE
                buffer.duration = round(Gst.SECOND / args.fps)

                repeated = last_generation == frame.generation
                push_ns = time.monotonic_ns()
                result = output_source.emit("push-buffer", buffer)
                if result != Gst.FlowReturn.OK:
                    if not stop_event.is_set():
                        print(
                            f"GStreamer appsrc push failed: {result}",
                            file=sys.stderr,
                            flush=True,
                        )
                        exit_code = 1
                    stop_event.set()
                    GLib.idle_add(loop.quit)
                    return
                metrics.add_output(push_ns, repeated)
                if frame_index == 0:
                    print("GNOME first clocked output frame", file=sys.stderr, flush=True)
                last_generation = frame.generation
                frame_index += 1

                now_ns = time.monotonic_ns()
                if now_ns - report_ns >= 5_000_000_000:
                    (
                        source_count,
                        output_count,
                        new_count,
                        repeated_count,
                        max_gap,
                        startup_ms,
                    ) = metrics.snapshot()
                    elapsed = (now_ns - report_ns) / 1_000_000_000
                    print(
                        "GNOME cadence "
                        f"source_fps={(source_count - report_source) / elapsed:.2f} "
                        f"output_fps={(output_count - report_output) / elapsed:.2f} "
                        f"source={source_count} output={output_count} "
                        f"new={new_count} repeated={repeated_count} "
                        f"max_gap_ms={max_gap:.3f} "
                        f"startup_ms={startup_ms if startup_ms is not None else -1:.3f}",
                        file=sys.stderr,
                        flush=True,
                    )
                    report_ns = now_ns
                    report_source = source_count
                    report_output = output_count

            output_source.emit("end-of-stream")

        scheduler = threading.Thread(
            target=run_scheduler,
            name="usbdisplay-gnome-clock",
            daemon=True,
        )
        scheduler.start()
        loop.run()
    except (GLib.Error, PortalError, RuntimeError, KeyError) as error:
        print(f"GNOME capture failed: {error}", file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        stop_event.set()
        if source_pipeline is not None:
            source_pipeline.set_state(Gst.State.NULL)
        if output_pipeline is not None:
            output_pipeline.set_state(Gst.State.NULL)
        if scheduler is not None:
            scheduler.join(timeout=3)
        portal.close()
        if pipewire_fd >= 0:
            os.close(pipewire_fd)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""GNOME ScreenCast portal to persistent Annex-B H.264 stdout stream."""

from __future__ import annotations

import argparse
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
CURSOR_EMBEDDED = 1
CURSOR_METADATA = 2
CURSOR_HIDDEN = 0


def cursor_mode_to_value(mode: str) -> int:
    """Convert cursor mode string to portal cursor mode value."""
    return {
        "embedded": CURSOR_EMBEDDED,
        "metadata": CURSOR_METADATA,
        "hidden": CURSOR_HIDDEN,
        "auto": CURSOR_METADATA,  # default to metadata for lower latency
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
                raise PortalError(f"the active ScreenCast portal does not support cursor mode '{cursor_mode}'")

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
        cursor_value = cursor_mode_to_value(self.cursor_mode)
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
                        "cursor_mode": GLib.Variant("u", cursor_mode_to_value(self.cursor_mode)),
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
            raise PortalError(f"portal returned {len(streams)} streams, expected one")  # type: ignore[arg-type]
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
    """Exit if the orchestrator died without stopping us.

    The portal session (and its virtual monitor) dies with this process,
    and Mutter also reaps the session on peer disconnect, so terminating
    here can never leak the output. Covers SIGKILL-style orchestrator
    death where no teardown code runs.
    """

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

    thread = threading.Thread(target=_watch, name="usbdisplay-parent-watch", daemon=True)
    thread.start()


def source_caps() -> str:
    # Unconstrained on purpose: the virtual node rejects fixed pins.
    return "video/x-raw"


def build_pipeline(fd: int, node_id: int, args: argparse.Namespace) -> Gst.Pipeline:
    pipeline = Gst.parse_launch(
        " ".join(
            [
                f"pipewiresrc fd={fd} path={node_id} do-timestamp=true",
                f"! {source_caps()}",
                "! videorate",
                "! videoscale",
                "! videoconvert",
                f"! video/x-raw,width={args.width},height={args.height},framerate={args.fps}/1,format=I420",
                "! x264enc",
                "byte-stream=true",
                "aud=true",
                "tune=zerolatency",
                "speed-preset=ultrafast",
                f"key-int-max={args.fps}",
                "pass=qual",
                f"quantizer={args.crf}",
                "threads=2",
                f'option-string="repeat-headers=1:scenecut=0:min-keyint={args.fps}"',
                "! video/x-h264,stream-format=byte-stream,alignment=au,profile=baseline",
                "! fdsink fd=1 sync=false",
            ]
        )
    )
    if not isinstance(pipeline, Gst.Pipeline):
        raise RuntimeError("GStreamer did not create a pipeline")
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
        help="Cursor capture mode: embedded (composited), metadata (separate stream), hidden (no cursor), auto (default)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    Gst.init(None)
    portal = ScreenCastPortal(cursor_mode=args.cursor_mode)
    pipeline: Gst.Pipeline | None = None
    pipewire_fd = -1
    loop = GLib.MainLoop()
    watch_parent_exit(os.getppid(), loop)
    exit_code = 0

    def stop(_signum: int, _frame: object) -> None:
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
        pipeline = build_pipeline(pipewire_fd, node_id, args)
        bus = pipeline.get_bus()
        bus.add_signal_watch()

        def on_message(_bus: Gst.Bus, message: Gst.Message) -> None:
            nonlocal exit_code
            if message.type == Gst.MessageType.ERROR:
                error, details = message.parse_error()
                print(f"GStreamer error: {error}: {details}", file=sys.stderr, flush=True)
                exit_code = 1
                loop.quit()
            elif message.type == Gst.MessageType.EOS:
                loop.quit()

        bus.connect("message", on_message)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer pipeline failed to enter PLAYING state")
        loop.run()
    except (GLib.Error, PortalError, RuntimeError, KeyError) as error:
        print(f"GNOME capture failed: {error}", file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        portal.close()
        if pipewire_fd >= 0:
            os.close(pipewire_fd)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

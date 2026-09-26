"""Compositor-specific virtual-output and capture backends."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Mapping

from .producers import GnomePortalH264Producer, H264Producer, MjpegProducer, Producer
from .transport import LatestFrameQueue

LOG = logging.getLogger("usbdisplay.compositor")


def _command(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=15,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def detect_compositor(
    requested: str = "auto",
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return a normalized compositor name without silently guessing."""
    if requested != "auto":
        return requested

    env = os.environ if environ is None else environ
    session_type = env.get("XDG_SESSION_TYPE", "").casefold()
    if session_type and session_type != "wayland":
        raise RuntimeError(
            f"USBDisplay requires a Wayland session; detected {session_type!r}"
        )

    desktops = {
        value.strip().casefold()
        for value in env.get("XDG_CURRENT_DESKTOP", "").split(":")
        if value.strip()
    }
    if "hyprland" in desktops:
        return "hyprland"
    if "gnome" in desktops:
        return "gnome"
    if desktops.intersection({"kde", "plasma"}):
        return "kde"
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    detected = env.get("XDG_CURRENT_DESKTOP", "unknown")
    raise RuntimeError(
        f"unsupported Wayland compositor {detected!r}; use --compositor to override"
    )


class CompositorBackend:
    name = "unknown"

    def required_programs(self, codec: str) -> list[str]:
        raise NotImplementedError

    def validate(self, codec: str) -> None:
        missing = [
            program
            for program in self.required_programs(codec)
            if shutil.which(program) is None
        ]
        if missing:
            raise RuntimeError("missing required programs: " + ", ".join(missing))

    def prepare_output(self, name: str, width: int, height: int, refresh: int) -> None:
        raise NotImplementedError

    def cleanup_output(self, name: str, keep: bool) -> None:
        raise NotImplementedError

    def create_producer(
        self,
        *,
        codec: str,
        frames: LatestFrameQueue,
        stop: threading.Event,
        output: str,
        width: int,
        height: int,
        fps: int,
        refresh: int,
        crf: int,
        constant_fps: bool,
        quality: int,
        include_cursor: bool,
        cursor_mode: str = "auto",
    ) -> Producer:
        raise NotImplementedError


class HyprlandBackend(CompositorBackend):
    name = "hyprland"

    def __init__(self) -> None:
        self.created_output = False

    def required_programs(self, codec: str) -> list[str]:
        return ["adb", "hyprctl", "wf-recorder" if codec == "h264" else "grim"]

    @staticmethod
    def monitor_names() -> set[str]:
        result = _command("hyprctl", "-j", "monitors", "all", capture=True)
        return {monitor["name"] for monitor in json.loads(result.stdout)}

    def prepare_output(self, name: str, width: int, height: int, refresh: int) -> None:
        self.created_output = name not in self.monitor_names()
        if self.created_output:
            _command("hyprctl", "output", "create", "headless", name)

        mode = f"{width}x{height}@{refresh}"
        lua = (
            "hl.monitor({ output = "
            + json.dumps(name)
            + ", mode = "
            + json.dumps(mode)
            + ', position = "auto-right", scale = 1 })'
        )
        try:
            _command("hyprctl", "eval", lua)
            if name not in self.monitor_names():
                raise RuntimeError(f"Hyprland did not expose output {name}")
        except Exception:
            self.cleanup_output(name, keep=False)
            raise

    def cleanup_output(self, name: str, keep: bool) -> None:
        if self.created_output and not keep:
            LOG.info("removing temporary Hyprland output %s", name)
            subprocess.run(
                ["hyprctl", "output", "remove", name],
                check=False,
                timeout=15,
            )
            self.created_output = False

    def create_producer(self, **options: object) -> Producer:
        codec = str(options["codec"])
        if codec == "h264":
            return H264Producer(
                frames=options["frames"],  # type: ignore[arg-type]
                stop=options["stop"],  # type: ignore[arg-type]
                output=str(options["output"]),
                fps=int(options["fps"]),
                crf=int(options["crf"]),
                constant_fps=bool(options["constant_fps"]),
            )
        return MjpegProducer(
            frames=options["frames"],  # type: ignore[arg-type]
            stop=options["stop"],  # type: ignore[arg-type]
            output=str(options["output"]),
            fps=int(options["fps"]),
            quality=int(options["quality"]),
            include_cursor=bool(options["include_cursor"]),
        )


class GnomeBackend(CompositorBackend):
    name = "gnome"

    def required_programs(self, codec: str) -> list[str]:
        return ["adb", "gst-inspect-1.0"]

    def validate(self, codec: str) -> None:
        super().validate(codec)
        if codec != "h264":
            raise RuntimeError("the GNOME backend currently supports only --codec h264")
        bindings = subprocess.run(
            [
                sys.executable,
                "-c",
                "import gi; gi.require_version('Gst', '1.0'); "
                "from gi.repository import Gio, GLib, Gst",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
        if bindings.returncode:
            raise RuntimeError(
                "missing Python GObject/GStreamer bindings; install python-gobject"
            )
        for plugin in ("pipewiresrc", "x264enc"):
            result = subprocess.run(
                ["gst-inspect-1.0", plugin],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
            )
            if result.returncode:
                raise RuntimeError(
                    f"missing GStreamer element {plugin}; install the GNOME capture dependencies"
                )

    def prepare_output(self, name: str, width: int, height: int, refresh: int) -> None:
        LOG.info(
            "GNOME virtual output will be created by the ScreenCast portal at %dx%d@%d",
            width,
            height,
            refresh,
        )

    def cleanup_output(self, name: str, keep: bool) -> None:
        if keep:
            LOG.warning("--keep-output is ignored on GNOME; portal outputs are session-scoped")
        self._reap_helper_processes()

    @staticmethod
    def _helper_argv_markers() -> tuple[str, str]:
        return ("gnome_capture.py", "--width")

    def _reap_helper_processes(self) -> None:
        """Terminate leftover capture helpers by exact argv match.

        The portal session (and its virtual monitor) dies with the helper,
        so this both stops encoding and removes the output. Matches only
        processes running this file with its stream arguments; never broad
        names like ``python`` or ``gst-launch``.
        """
        marker_file, marker_arg = self._helper_argv_markers()
        try:
            listing = subprocess.run(
                ["ps", "-eo", "pid,args"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (subprocess.SubprocessError, OSError) as error:
            LOG.warning("GNOME helper sweep unavailable: %s", error)
            return
        targets: list[int] = []
        for line in listing.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            pid_text, args = parts
            argv = args.split()
            if (
                len(argv) >= 3
                and argv[1].endswith(marker_file)
                and marker_arg in argv
                and pid_text.isdigit()
                and int(pid_text) != os.getpid()
            ):
                targets.append(int(pid_text))
        for pid in targets:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError) as error:
                LOG.debug("GNOME helper %d already gone: %s", pid, error)
                continue
        deadline = time.monotonic() + 5
        pending = [
            pid
            for pid in targets
            if _pid_alive(pid) and time.monotonic() < deadline
        ]
        while pending and time.monotonic() < deadline:
            time.sleep(0.2)
            pending = [pid for pid in pending if _pid_alive(pid)]
        for pid in pending:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if targets:
            LOG.info("reaped %d leftover GNOME capture helper(s)", len(targets))

    def create_producer(self, **options: object) -> Producer:
        helper = Path(__file__).with_name("gnome_capture.py")
        if not bool(options["constant_fps"]):
            LOG.warning("--damage-aware is ignored by the GNOME portal backend")
        crf = int(options["crf"])
        if crf > 50:
            raise RuntimeError("the GNOME x264 encoder supports --h264-crf from 0 to 50")
        return GnomePortalH264Producer(
            frames=options["frames"],  # type: ignore[arg-type]
            stop=options["stop"],  # type: ignore[arg-type]
            helper=helper,
            width=int(options["width"]),
            height=int(options["height"]),
            fps=int(options["fps"]),
            refresh=int(options["refresh"]),
            crf=crf,
            python=sys.executable,
            cursor_mode=options.get("cursor_mode", "auto"),
        )


class KdeBackend(CompositorBackend):
    name = "kde"

    def validate(self, codec: str) -> None:
        raise RuntimeError(
            "KDE Wayland detected, but the KWin portal backend is not implemented yet"
        )

    def required_programs(self, codec: str) -> list[str]:
        return ["adb"]

    def prepare_output(self, name: str, width: int, height: int, refresh: int) -> None:
        raise RuntimeError("KDE backend is not implemented")

    def cleanup_output(self, name: str, keep: bool) -> None:
        return


def select_backend(name: str) -> CompositorBackend:
    if name == "hyprland":
        return HyprlandBackend()
    if name == "gnome":
        return GnomeBackend()
    if name == "kde":
        return KdeBackend()
    raise RuntimeError(f"unsupported compositor backend {name!r}")

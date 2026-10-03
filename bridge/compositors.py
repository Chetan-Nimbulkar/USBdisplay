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

    def confirm_output(
        self,
        name: str,
        width: int,
        height: int,
        refresh: int,
    ) -> None:
        """Finalize and verify output geometry immediately before capture."""
        return

    def validate_output(
        self,
        name: str,
        width: int,
        height: int,
        refresh: int,
    ) -> None:
        """Raise if a compositor-managed output changed during the session."""
        return

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
    REFRESH_TOLERANCE = 0.5

    def __init__(self) -> None:
        self.created_output = False
        self.monitor_manager_paused = False

    def required_programs(self, codec: str) -> list[str]:
        return ["adb", "hyprctl", "wf-recorder" if codec == "h264" else "grim"]

    @staticmethod
    def monitor_states() -> list[dict[str, object]]:
        result = _command("hyprctl", "-j", "monitors", "all", capture=True)
        try:
            monitors = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Hyprland returned invalid monitor JSON") from error
        if not isinstance(monitors, list):
            raise RuntimeError("Hyprland returned an invalid monitor list")
        return [monitor for monitor in monitors if isinstance(monitor, dict)]

    @classmethod
    def monitor_state(cls, name: str) -> dict[str, object] | None:
        return next(
            (
                monitor
                for monitor in cls.monitor_states()
                if monitor.get("name") == name
            ),
            None,
        )

    @classmethod
    def monitor_names(cls) -> set[str]:
        return {
            str(monitor["name"])
            for monitor in cls.monitor_states()
            if "name" in monitor
        }

    @staticmethod
    def _apply_mode(name: str, width: int, height: int, refresh: int) -> None:
        mode = f"{width}x{height}@{refresh}"
        lua = (
            "hl.monitor({ output = "
            + json.dumps(name)
            + ", mode = "
            + json.dumps(mode)
            + ', position = "auto-right", scale = 1 })'
        )
        _command("hyprctl", "eval", lua)

    @classmethod
    def _verified_geometry(
        cls,
        name: str,
        width: int,
        height: int,
        refresh: int,
    ) -> tuple[int, int, float]:
        monitor = cls.monitor_state(name)
        if monitor is None:
            raise RuntimeError(f"Hyprland output {name} disappeared")
        try:
            actual_width = int(monitor["width"])
            actual_height = int(monitor["height"])
            actual_refresh = float(monitor["refreshRate"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(
                f"Hyprland output {name} did not report valid geometry"
            ) from error
        if (
            actual_width != width
            or actual_height != height
            or abs(actual_refresh - refresh) > cls.REFRESH_TOLERANCE
        ):
            raise RuntimeError(
                f"Hyprland output {name} geometry changed: expected "
                f"{width}x{height}@{refresh}, got "
                f"{actual_width}x{actual_height}@{actual_refresh:.2f}"
            )
        return actual_width, actual_height, actual_refresh

    def _pause_monitor_manager(self) -> None:
        if shutil.which("systemctl") is None:
            return
        status = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", "hyprmoncfgd.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        if status.returncode != 0:
            return
        LOG.info(
            "temporarily masking hyprmoncfgd while USBDisplay owns the virtual output"
        )
        _command(
            "systemctl", "--user", "mask", "--runtime", "--now",
            "hyprmoncfgd.service",
        )
        self.monitor_manager_paused = True

    def _resume_monitor_manager(self) -> None:
        if not self.monitor_manager_paused:
            return
        unmasked = subprocess.run(
            [
                "systemctl",
                "--user",
                "unmask",
                "--runtime",
                "hyprmoncfgd.service",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
        if unmasked.returncode:
            LOG.warning(
                "could not unmask hyprmoncfgd; run systemctl --user "
                "unmask --runtime hyprmoncfgd.service"
            )
            self.monitor_manager_paused = False
            return
        result = subprocess.run(
            ["systemctl", "--user", "start", "hyprmoncfgd.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
        if result.returncode:
            LOG.warning(
                "could not restart hyprmoncfgd; run "
                "systemctl --user start hyprmoncfgd.service"
            )
        else:
            LOG.info("restored hyprmoncfgd after USBDisplay teardown")
        self.monitor_manager_paused = False

    def prepare_output(self, name: str, width: int, height: int, refresh: int) -> None:
        self._pause_monitor_manager()
        self.created_output = self.monitor_state(name) is None
        if self.created_output:
            _command("hyprctl", "output", "create", "headless", name)

        try:
            self._apply_mode(name, width, height, refresh)
            if self.monitor_state(name) is None:
                raise RuntimeError(f"Hyprland did not expose output {name}")
        except Exception:
            self.cleanup_output(name, keep=False)
            raise

    def confirm_output(
        self,
        name: str,
        width: int,
        height: int,
        refresh: int,
    ) -> None:
        self._apply_mode(name, width, height, refresh)
        actual_width, actual_height, actual_refresh = self._verified_geometry(
            name,
            width,
            height,
            refresh,
        )
        LOG.info(
            "verified Hyprland output %s at %dx%d@%.2f",
            name,
            actual_width,
            actual_height,
            actual_refresh,
        )

    def validate_output(
        self,
        name: str,
        width: int,
        height: int,
        refresh: int,
    ) -> None:
        self._verified_geometry(name, width, height, refresh)

    def cleanup_output(self, name: str, keep: bool) -> None:
        try:
            if self.created_output and not keep:
                LOG.info("removing temporary Hyprland output %s", name)
                subprocess.run(
                    ["hyprctl", "output", "remove", name],
                    check=False,
                    timeout=15,
                )
                self.created_output = False
        finally:
            self._resume_monitor_manager()

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
        native_helper = Path(__file__).with_name("gnome_capture_native")
        if not native_helper.is_file() or not os.access(native_helper, os.X_OK):
            raise RuntimeError(
                "missing native GNOME capture helper; "
                "run tools/build_gnome_capture_native.sh"
            )
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
        return ("gnome_capture_metadata.py", "--width")

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
        helper = Path(__file__).with_name("gnome_capture_metadata.py")
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

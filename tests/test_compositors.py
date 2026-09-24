import signal
import subprocess
import unittest
from unittest import mock

from bridge.compositors import (
    GnomeBackend,
    HyprlandBackend,
    KdeBackend,
    detect_compositor,
    select_backend,
)


class CompositorDetectionTests(unittest.TestCase):
    def test_detects_hyprland(self) -> None:
        self.assertEqual(
            detect_compositor(
                environ={
                    "XDG_SESSION_TYPE": "wayland",
                    "XDG_CURRENT_DESKTOP": "Hyprland",
                }
            ),
            "hyprland",
        )

    def test_detects_gnome_from_colon_separated_desktop(self) -> None:
        self.assertEqual(
            detect_compositor(
                environ={
                    "XDG_SESSION_TYPE": "wayland",
                    "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
                }
            ),
            "gnome",
        )

    def test_desktop_identity_wins_over_stale_hyprland_variable(self) -> None:
        self.assertEqual(
            detect_compositor(
                environ={
                    "XDG_SESSION_TYPE": "wayland",
                    "XDG_CURRENT_DESKTOP": "GNOME",
                    "HYPRLAND_INSTANCE_SIGNATURE": "stale",
                }
            ),
            "gnome",
        )

    def test_detects_kde(self) -> None:
        self.assertEqual(
            detect_compositor(
                environ={
                    "XDG_SESSION_TYPE": "wayland",
                    "XDG_CURRENT_DESKTOP": "KDE",
                }
            ),
            "kde",
        )

    def test_rejects_x11_session(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires a Wayland session"):
            detect_compositor(
                environ={
                    "XDG_SESSION_TYPE": "x11",
                    "XDG_CURRENT_DESKTOP": "GNOME",
                }
            )

    def test_explicit_override_is_respected(self) -> None:
        self.assertEqual(
            detect_compositor("gnome", environ={"XDG_SESSION_TYPE": "x11"}),
            "gnome",
        )

    def test_selects_backend_implementations(self) -> None:
        self.assertIsInstance(select_backend("hyprland"), HyprlandBackend)
        self.assertIsInstance(select_backend("gnome"), GnomeBackend)
        self.assertIsInstance(select_backend("kde"), KdeBackend)


class GnomeCleanupSweepTests(unittest.TestCase):
    PS_FIXTURE = """\
    PID COMMAND
      1 /sbin/init splash
   1234 /usr/bin/python /home/kali/Projects/USBdisplay/bridge/gnome_capture.py --width 800 --height 600 --fps 30 --refresh 30 --crf 23
   5678 /usr/bin/python /home/kali/Projects/USBdisplay/bridge/server.py
   9999 /usr/bin/python /opt/other.py --width 800
"""

    def test_reaps_only_exact_helper_match(self) -> None:
        killed: list[tuple[int, int]] = []
        dead: set[int] = set()

        def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(list(args[0]), 0, stdout=self.PS_FIXTURE, stderr="")

        def fake_kill(pid: int, sig: int) -> None:
            if pid in dead:
                raise ProcessLookupError(pid)
            killed.append((pid, sig))
            if sig == signal.SIGTERM:
                dead.add(pid)

        with (
            mock.patch("bridge.compositors.subprocess.run", side_effect=fake_run),
            mock.patch("bridge.compositors.os.kill", side_effect=fake_kill),
        ):
            GnomeBackend().cleanup_output("USBDisplay", keep=False)
        termed = [pid for pid, sig in killed if sig == signal.SIGTERM]
        self.assertEqual(termed, [1234])
        self.assertFalse(any(pid == 5678 for pid, _ in killed))
        self.assertFalse(any(pid == 9999 for pid, _ in killed))


if __name__ == "__main__":
    unittest.main()

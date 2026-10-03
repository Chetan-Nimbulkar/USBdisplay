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


class HyprlandGeometryTests(unittest.TestCase):
    @staticmethod
    def monitor(width: int, height: int, refresh: float) -> list[dict[str, object]]:
        return [
            {
                "name": "USBDisplay",
                "width": width,
                "height": height,
                "refreshRate": refresh,
            }
        ]

    def test_prepare_creates_and_configures_headless_output(self) -> None:
        states = [[], self.monitor(800, 600, 30.0)]

        def fake_command(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
            stdout = ""
            if args[:4] == ("hyprctl", "-j", "monitors", "all"):
                stdout = __import__("json").dumps(states.pop(0))
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        backend = HyprlandBackend()
        with (
            mock.patch("bridge.compositors._command", side_effect=fake_command) as command,
            mock.patch.object(backend, "_pause_monitor_manager"),
        ):
            backend.prepare_output("USBDisplay", 800, 600, 30)

        command.assert_any_call(
            "hyprctl", "output", "create", "headless", "USBDisplay"
        )
        eval_calls = [call for call in command.call_args_list if call.args[1] == "eval"]
        self.assertEqual(len(eval_calls), 1)
        self.assertIn('mode = "800x600@30"', eval_calls[0].args[2])
        self.assertTrue(backend.created_output)

    def test_confirm_reapplies_and_verifies_mode(self) -> None:
        backend = HyprlandBackend()
        state = self.monitor(800, 600, 30.0)
        result = subprocess.CompletedProcess(
            ("hyprctl",),
            0,
            stdout=__import__("json").dumps(state),
            stderr="",
        )

        with (
            mock.patch("bridge.compositors._command", return_value=result) as command,
        ):
            backend.confirm_output("USBDisplay", 800, 600, 30)

        eval_calls = [call for call in command.call_args_list if call.args[1] == "eval"]
        self.assertEqual(len(eval_calls), 1)
        self.assertIn('mode = "800x600@30"', eval_calls[0].args[2])

    def test_validate_rejects_external_geometry_change(self) -> None:
        state = self.monitor(1920, 1080, 60.0)
        result = subprocess.CompletedProcess(
            ("hyprctl",),
            0,
            stdout=__import__("json").dumps(state),
            stderr="",
        )
        backend = HyprlandBackend()

        with mock.patch("bridge.compositors._command", return_value=result):
            with self.assertRaisesRegex(
                RuntimeError,
                r"expected 800x600@30, got 1920x1080@60\.00",
            ):
                backend.validate_output("USBDisplay", 800, 600, 30)

    def test_pauses_and_restores_active_hyprmoncfg_service(self) -> None:
        backend = HyprlandBackend()
        active = subprocess.CompletedProcess(("systemctl",), 0)
        unmasked = subprocess.CompletedProcess(("systemctl",), 0)
        started = subprocess.CompletedProcess(("systemctl",), 0)

        with (
            mock.patch("bridge.compositors.shutil.which", return_value="/usr/bin/systemctl"),
            mock.patch(
                "bridge.compositors.subprocess.run",
                side_effect=(active, unmasked, started),
            ) as run,
            mock.patch("bridge.compositors._command") as command,
        ):
            backend._pause_monitor_manager()
            self.assertTrue(backend.monitor_manager_paused)
            backend._resume_monitor_manager()

        command.assert_called_once_with(
            "systemctl", "--user", "mask", "--runtime", "--now",
            "hyprmoncfgd.service",
        )
        self.assertEqual(
            run.call_args_list[0].args[0],
            [
                "systemctl",
                "--user",
                "is-active",
                "--quiet",
                "hyprmoncfgd.service",
            ],
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                "systemctl",
                "--user",
                "unmask",
                "--runtime",
                "hyprmoncfgd.service",
            ],
        )
        self.assertEqual(
            run.call_args_list[2].args[0],
            ["systemctl", "--user", "start", "hyprmoncfgd.service"],
        )
        self.assertFalse(backend.monitor_manager_paused)

    def test_base_backend_geometry_hooks_are_noops(self) -> None:
        backend = GnomeBackend()

        backend.confirm_output("ignored", 800, 600, 30)
        backend.validate_output("ignored", 800, 600, 30)


class GnomeCleanupSweepTests(unittest.TestCase):
    PS_FIXTURE = """\
    PID COMMAND
      1 /sbin/init splash
   1234 /usr/bin/python /home/kali/Projects/USBdisplay/bridge/gnome_capture_metadata.py --width 800 --height 600 --fps 30 --refresh 30 --crf 23
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

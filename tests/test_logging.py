import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import unittest

from bridge.server import is_expected_shutdown_interrupt, prune_logs


class LogRetentionTests(unittest.TestCase):
    def test_removes_logs_older_than_maximum_age(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary)
            old = logs / "old.log"
            recent = logs / "recent.log"
            old.write_bytes(b"old")
            recent.write_bytes(b"recent")
            os.utime(old, (100, 100))
            os.utime(recent, (190, 190))

            prune_logs(logs, now=200, max_age_seconds=50, max_total_bytes=100)

            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())

    def test_removes_oldest_logs_until_size_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary)
            oldest = logs / "oldest.log"
            middle = logs / "middle.log"
            newest = logs / "newest.log"
            for path, modified in ((oldest, 100), (middle, 110), (newest, 120)):
                path.write_bytes(b"123456")
                os.utime(path, (modified, modified))

            prune_logs(
                logs,
                now=130,
                max_age_seconds=100,
                max_total_bytes=12,
            )

            self.assertFalse(oldest.exists())
            self.assertTrue(middle.exists())
            self.assertTrue(newest.exists())


class ShutdownTests(unittest.TestCase):
    def test_accepts_sigint_child_exit_after_shutdown_request(self) -> None:
        stop = threading.Event()
        stop.set()
        error = subprocess.CalledProcessError(-signal.SIGINT, ["adb", "devices"])

        self.assertTrue(is_expected_shutdown_interrupt(error, stop))

    def test_does_not_hide_other_child_failures(self) -> None:
        stop = threading.Event()
        stop.set()
        error = subprocess.CalledProcessError(1, ["adb", "devices"])

        self.assertFalse(is_expected_shutdown_interrupt(error, stop))


if __name__ == "__main__":
    unittest.main()

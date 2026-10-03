#!/usr/bin/env python3
"""Production GNOME portal/session wrapper for native cursor composition."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from bridge.gnome_capture import ScreenCastPortal  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=800)
    parser.add_argument("--height", type=int, default=600)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--refresh", type=int, required=True)
    parser.add_argument("--crf", type=int, required=True)
    parser.add_argument(
        "--cursor-mode",
        choices=("embedded", "metadata", "hidden", "auto"),
        default="auto",
    )
    parser.add_argument(
        "--helper",
        type=Path,
        default=Path(__file__).with_name("gnome_capture_native"),
    )
    args = parser.parse_args()

    if not args.helper.is_file() or not os.access(args.helper, os.X_OK):
        raise SystemExit(
            f"missing native GNOME helper {args.helper}; "
            "run tools/build_gnome_capture_native.sh"
        )

    effective_cursor_mode = "metadata" if args.cursor_mode == "auto" else args.cursor_mode
    portal = ScreenCastPortal(cursor_mode=effective_cursor_mode)
    remote_fd = -1
    try:
        portal.create_session()
        portal.select_virtual_source()
        node_id = portal.start()
        remote_fd = portal.open_pipewire_remote()
        print(
            f"GNOME portal node={node_id} fd={remote_fd} mode={effective_cursor_mode} "
            f"native={args.width}x{args.height} output={args.fps}fps",
            file=sys.stderr,
            flush=True,
        )
        command = [
            str(args.helper),
            str(remote_fd),
            str(node_id),
            str(args.width),
            str(args.height),
            str(args.fps),
            "h264",
            str(args.crf),
        ]
        process = subprocess.Popen(command, pass_fds=(remote_fd,))
        parent_pid = os.getppid()

        def forward_stop(signum: int, _frame: object) -> None:
            if process.poll() is None:
                process.send_signal(signum)

        signal.signal(signal.SIGINT, forward_stop)
        signal.signal(signal.SIGTERM, forward_stop)

        def watch_parent() -> None:
            while process.poll() is None:
                if os.getppid() != parent_pid:
                    process.send_signal(signal.SIGTERM)
                    return
                time.sleep(1)

        threading.Thread(
            target=watch_parent,
            name="usbdisplay-gnome-parent-watch",
            daemon=True,
        ).start()
        return process.wait()
    finally:
        portal.close()
        if remote_fd >= 0:
            os.close(remote_fd)


if __name__ == "__main__":
    raise SystemExit(main())

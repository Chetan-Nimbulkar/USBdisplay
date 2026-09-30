#!/usr/bin/env python3
"""Open an isolated metadata-mode portal session and run the native probe."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from bridge.gnome_capture import ScreenCastPortal  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=800)
    parser.add_argument("--height", type=int, default=600)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument(
        "--probe",
        type=Path,
        default=Path(__file__).with_name("phase42c_cursor_probe"),
    )
    args = parser.parse_args()

    portal = ScreenCastPortal(cursor_mode="metadata")
    remote_fd = -1
    try:
        portal.create_session()
        portal.select_virtual_source()
        node_id = portal.start()
        remote_fd = portal.open_pipewire_remote()
        print(
            f"PORTAL node={node_id} fd={remote_fd} mode=metadata "
            f"size={args.width}x{args.height}",
            file=sys.stderr,
            flush=True,
        )
        command = [
            str(args.probe),
            str(remote_fd),
            str(node_id),
            str(args.width),
            str(args.height),
            str(args.fps),
        ]
        return subprocess.run(command, pass_fds=(remote_fd,), check=False).returncode
    finally:
        portal.close()
        if remote_fd >= 0:
            os.close(remote_fd)


if __name__ == "__main__":
    raise SystemExit(main())

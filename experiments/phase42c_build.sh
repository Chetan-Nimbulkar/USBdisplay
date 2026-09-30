#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cc -std=gnu11 -O2 -g -Wall -Wextra \
  "$script_dir/phase42c_cursor_probe.c" \
  -o "$script_dir/phase42c_cursor_probe" \
  $(pkg-config --cflags --libs gstreamer-1.0 gstreamer-app-1.0 libpipewire-0.3) \
  -pthread
printf 'built %s\n' "$script_dir/phase42c_cursor_probe"

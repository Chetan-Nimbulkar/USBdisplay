#!/usr/bin/env bash
set -euo pipefail

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source_file="$project_dir/bridge/gnome_capture_native.c"
output_file="$project_dir/bridge/gnome_capture_native"

cc -std=gnu11 -O2 -g -Wall -Wextra \
  "$source_file" \
  -o "$output_file" \
  $(pkg-config --cflags --libs gstreamer-1.0 gstreamer-app-1.0 libpipewire-0.3) \
  -pthread

printf 'built %s\n' "$output_file"

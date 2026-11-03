#!/usr/bin/env bash
set -Eeuo pipefail

# Retained for compatibility; build our private manifest as well as rclone's
# native Photos cache. This reads only metadata and never downloads media.
script_dir="$(cd "$(dirname "$(realpath "$0")")" && pwd -P)"
exec python3 "$script_dir/archive_batch.py" photos-inventory --execute --defer-while-google

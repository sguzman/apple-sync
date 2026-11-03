#!/usr/bin/env bash
set -Eeuo pipefail

# Explicit, read-only first-inventory cache warming. NOT an archive transfer.
# rclone internally enumerates the large album before it can persist its cache.
command -v rclone >/dev/null || { echo 'rclone is required' >&2; exit 2; }
command -v timeout >/dev/null || { echo 'timeout is required' >&2; exit 2; }
if ! rclone listremotes | grep -Fxq 'icloudphotos:'; then
  echo 'icloudphotos: remote is not configured' >&2
  exit 2
fi

echo 'Warming the All Photos metadata cache (up to 30 minutes).'
echo 'This reads only the album inventory; no media files are downloaded.'
echo 'Filenames are discarded; rclone can persist its album cache on success.'
if timeout -k 10s 1800s rclone lsf \
    'icloudphotos:PrimarySync/All Photos' \
    --recursive --files-only --checkers 1 --tpslimit 2 \
    --log-level ERROR >/dev/null; then
  echo 'All Photos inventory finished; subsequent listings should use rclone cache.'
else
  rc=$?
  echo "All Photos cache warmup failed or timed out (exit $rc); no archive downloads started." >&2
  exit "$rc"
fi

#!/usr/bin/env bash
set -Eeuo pipefail
repo_dir="$(cd "$(dirname "$(realpath "$0")")/.." && pwd -P)"
unit_dir="$HOME/.config/systemd/user"
bin_dir="$HOME/.local/bin"

[[ "$(findmnt -n --mountpoint /drive -o FSTYPE)" == 'ext4' ]] || { echo '/drive must be mounted ext4' >&2; exit 1; }
[[ ! -L /drive/icloud ]] || { echo 'Refusing symlink /drive/icloud' >&2; exit 1; }
mkdir -p /drive/icloud/drive /drive/icloud/photos/primary /drive/icloud/photos/shared /drive/icloud/exports /drive/icloud/manifests /drive/icloud/logs
# Built-in, visible Photos albums only. Never include Hidden or Recently Deleted.
for album in Animated Bursts Panoramas Slo-mo Time-lapse Videos Favorites; do
  mkdir -p "/drive/icloud/photos/albums/$album"
done
mkdir -p "$unit_dir" "$bin_dir"
link_safe() {
  local source="$1" target="$2"
  if [[ -L "$target" ]]; then
    [[ "$(realpath "$target")" == "$source" ]] || { echo "Refusing to replace $target" >&2; exit 1; }
  elif [[ -e "$target" ]]; then
    echo "Refusing to replace $target" >&2; exit 1
  fi
  ln -sfn "$source" "$target"
}
link_safe "$repo_dir/scripts/archive_batch.py" "$bin_dir/apple-sync-batch"
for kind in drive photos photos-progress; do
  for suffix in service timer; do
    link_safe "$repo_dir/systemd/user/apple-sync-$kind.$suffix" "$unit_dir/apple-sync-$kind.$suffix"
  done
done
systemctl --user daemon-reload
printf 'Installed symlinks for apple-sync. Timers are NOT enabled.\n'
printf 'Preview: %s/apple-sync-batch drive\n' "$bin_dir"
printf 'Preview: %s/apple-sync-batch photos\n' "$bin_dir"
printf 'Incremental preview: %s/apple-sync-batch photos-progress\n' "$bin_dir"
printf 'All Photos and Drive timers remain disabled; progress timer also starts disabled.\n'

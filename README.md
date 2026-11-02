# apple-sync

Linux-first, **download-only**, controlled iCloud archival using local rclone remotes and user-systemd units. Code and unit files live in this repository; **private data lives exclusively under /drive/icloud**. No passwords, 2FA codes, cookies, rclone config, photos, or device backups belong in Git.

## Sources and destinations

| Service | rclone source | Local destination | systemd timer |
| --- | --- | --- | --- |
| iCloud Drive | icloud: | /drive/icloud/drive | apple-sync-drive.timer |
| iCloud Photos primary library | icloudphotos:PrimarySync/All Photos | /drive/icloud/photos/primary | apple-sync-photos.timer |

The Photos path uses **All Photos** to archive original assets without duplicating each photo once for every album where it appears. Preservation of Apple albums/edits/extra metadata, shared libraries, and independent verification are separate future tasks; one path alone is not a complete Apple Photos export. An empty library or a changed API hierarchy can require adjusting the source path.

## Set up Photos authentication

Run rclone config -> new remote icloudphotos -> Storage iclouddrive -> service photos (2) -> Apple Account login/password -> 2FA -> keep remote. Do not disclose credentials or rclone config show output. Verify with a **read-only** command:

    rclone lsd icloudphotos:

The rclone backend is experimental, and Photos listing bugs have been reported in some versions. A successful scan is **not proof of a complete library**. Its trust token normally expires after around 30 days; rclone reconnect icloudphotos: may be required. Photos service is read-only from rclone.

## Install user systemd units (safe)

Clone this repository locally, then:

    bash scripts/install.sh

Installer requires /drive to be mounted ext4, creates only archive directories, and symlinks the script and units into ~/.local/bin and ~/.config/systemd/user. It refuses to overwrite unrelated unit files. **It never enables or starts either timer.**

Before enabling anything, update your local checkout and run offline tests:

    git pull --ff-only
    python3 -m unittest discover -s tests -v

Then probe **actual read-only source bytes** without publishing any archive files:

    python3 scripts/probe_ranges.py both

The probe looks only in a handful of small iCloud Drive folders and Photos albums, reads up to 16 KiB twice with overlapping offsets, checks exact lengths and overlap, and does not print filenames or file contents. It skips candidates larger than 16 MiB, since some backends may fetch more than the requested range internally. It reports PASS only when the range behavior is consistent; FAIL or INCONCLUSIVE means **do not enable the timers**. This is not end-to-end archive validation.

After the range probes pass, inspect a read-only full inventory plan (the first scan can take up to five minutes, especially for Photos):

    ~/.local/bin/apple-sync-batch drive
    ~/.local/bin/apple-sync-batch photos

Both inventory commands print an immediate scan-start message. Dry-run mode never repairs partial data; corrupt or uncommitted staging state is reported for manual review.

When ready **and only after both remote configurations and the plans are reviewed**, explicitly opt in:

    systemctl --user enable --now apple-sync-drive.timer apple-sync-photos.timer

To pause:

    systemctl --user disable --now apple-sync-drive.timer apple-sync-photos.timer
    systemctl --user stop apple-sync-drive.service apple-sync-photos.service

Status: systemctl --user list-timers 'apple-sync-*' and journalctl --user -u apple-sync-photos.service -n 50 --no-pager.

## Download budgets and persistent resumption

The two independent hourly jobs remain **disabled until explicitly enabled**. A run scans the relevant remote, then advances files using read-only rclone byte-range requests. It does not rely on interrupted rclone copy files resuming automatically.

- **128 MiB maximum requested payload per hourly pass**, unlimited number of small files within that byte budget.
- **512 KiB/s rclone transfer cap**; eight-MiB source byte-range chunks.
- **15-minute maximum execution time** including listing.
- **No maximum file size:** a 2-GiB video is staged in 128-MiB hourly installments and atomically published only after complete. When a run stops, completed chunks remain under /drive/icloud/.apple-sync-state; the next run checks the last chunk's SHA-256 and resumes at the recorded byte offset.
- Both remotes share a non-overlap lock; previously started large transfers are prioritized over new files.
- Final archive files cannot be overwritten or deleted by this downloader. Remote deletions never propagate.
- The script refuses to resume a changed source if listed size, timestamp, ID or hashes differ from the saved metadata. Such partials are retained for manual resolution.
- Network traffic includes protocol overhead and can exceed the requested payload cap. A failed chunk is not counted as durable progress and ends that run.
- **Live end-to-end validation is still needed.** Rclone's iCloud Photos backend is experimental; whether byte-range requests work correctly for the actual Apple file URLs must be verified before enabling schedules. The Apple API may also omit items from listings. Source hashes are not always available, so full end-to-end cryptographic verification is not always possible.
- Timers are staggered at :10 (Drive) and :40 (Photos), but the separate Google Drive downloader might still overlap. Do not enable the new timers until total bandwidth is reviewed.

## Development

Offline unit tests:

    python3 -m unittest discover -s tests -v

Version-controlled systemd service/timer definitions are authoritative. Actual rclone auth state and archive content remain outside the public repo.

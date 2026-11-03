# apple-sync

Linux-first, **download-only**, controlled iCloud archival using local rclone remotes and user-systemd units. Code and unit files live in this repository; **private data lives exclusively under /drive/icloud**. No passwords, 2FA codes, cookies, rclone config, photos, or device backups belong in Git.

## Sources and destinations

| Service | rclone source | Local destination | systemd timer |
| --- | --- | --- | --- |
| iCloud Drive | icloud: | /drive/icloud/drive | apple-sync-drive.timer |
| iCloud Photos primary library | icloudphotos:PrimarySync/All Photos | /drive/icloud/photos/primary | apple-sync-photos.timer (NOT READY) |
| Incremental built-in album subset | Selects one built-in album per pass | /drive/icloud/photos/albums/<album> | apple-sync-photos-progress.timer (opt-in, after testing) |

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

## Incremental Photos progress without the full All Photos inventory

The full `All Photos` first listing timed out after 300 seconds. **The
new `photos-progress` worker does not attempt that scan.** Each pass lists
just one smaller built-in visible album, downloads missing files using the
existing archive engine, and advances a persistent album cursor. After
the final album it cycles back to the beginning; new photos in those
albums are picked up on later passes. A failed album also advances the
cursor to avoid repeatedly blocking progress; failures are reported.

The initial rotation is Animated, Bursts, Panoramas, Slo-mo, Time-lapse,
Videos and Favorites. Hidden and Recently Deleted are deliberately
excluded. The album directories and individual progress checkpoints are
separate from `photos/primary`. Albums may overlap, and the worker does
not deduplicate files between albums. **This is only a partial archive of
selected collections, not a backup of the full All Photos library.** The canonical full-library path remains unmodified and disabled.

To exercise one real progress pass from the repository:

    git pull --ff-only
    bash scripts/install.sh
    python3 -m unittest discover -s tests -v
    python3 scripts/archive_batch.py photos-progress
    timeout 1000s python3 scripts/archive_batch.py photos-progress --execute

The preview does not move the persistent cursor. The execute command
downloads at most 128 MiB of requested payload at 512 KiB/s, for up to
15 minutes including scanning, and advances one album after the pass.
If a file exceeds the budget, its verified chunks stay in per-album
staging for a future visit. The album cursor lives under
`/drive/icloud/.apple-sync-state`. It is not stored in Git.

A separate user-systemd `apple-sync-photos-progress.timer` is installed
**disabled**. Once a live progress pass, restart/resumption, and bandwidth
coordination with the existing Google Drive downloader have been checked,
the incremental subset timer can be enabled **independently** of the
full-library and iCloud Drive timers:

    systemctl --user enable --now apple-sync-photos-progress.timer

Do **not** enable `apple-sync-photos.timer` or
`apple-sync-drive.timer` yet. The original full-library inventory still
needs a reliable first cache build (or a paginated backend), and the
Drive byte-range failure is unresolved. The progress timer is not a
substitute for full-library backup.

## Optional full-library cache warmup

The rotating worker only covers a subset of albums. To attempt the first
full All Photos metadata listing separately, run:

    bash scripts/warm_photos_cache.sh

This is an **optional, manual** 30-minute maximum read-only inventory,
not a media download or a timer. It discards filenames from the terminal.
Only a *completed* listing can populate rclone's All Photos disk cache.
Do not run it alongside bandwidth-intensive jobs on a weak connection.
After successful cache warming, validate a normal full archive pass
before considering the original `apple-sync-photos.timer`.

## First live Photos archive: bounded Favorites album

A live `photos --execute` attempt stopped after five minutes of listing
`PrimarySync/All Photos`; **no downloads were attempted**. Rclone's iCloud Photos backend documents that
initial scans of large albums require expensive pagination and can benefit
from its local disk cache after a complete scan.

To validate the actual **archive write and resumption** pipeline without
waiting for a complete All Photos inventory, the script additionally supports:

    git pull --ff-only
    bash scripts/install.sh
    python3 -m unittest discover -s tests -v
    timeout 1000s python3 scripts/archive_batch.py photos-favorites --execute

The built-in Favorites album is suitable for a small initial archive test. This archive uses the same byte-range,
checksum, lock and no-overwrite implementation as the full Photos archive;
it writes only to `/drive/icloud/photos/albums/Favorites` and separate
checkpoint state. It has the same 128 MiB/15-minute pass limits and will
retain partial transfers. It does **not** enable timers.

A Favorites archive is **not** a backup of All Photos. Albums can contain
copies of assets also found in All Photos; this path is deliberately
separate to preserve album membership and may consume duplicate storage.
After a real small-album archive passes, rework the full-library first
scan to allow its initial cache warmup and validate the canonical path.
Never report the entire photo library as archived based on an album pass.

## Current iCloud Drive range-verification blocker (2026-11-02)

A live rclone v1.75.2 probe reported **overlapping bytes disagree** for an
iCloud Drive file, while the iCloud Photos sample passed. This does not yet
prove whether the problem is backend-wide, file-specific, or a changing source.

**Drive archive execution is temporarily blocked in code.** The Photos archive
is also not yet approved for unattended operation. Do not enable timers.

To classify the Drive failure without modifying the remote or archive:

    git pull --ff-only
    python3 -m unittest discover -s tests -v
    timeout 210s python3 scripts/diagnose_drive_ranges.py

The diagnosis reads three small ranges (offsets 0, 1, 0) from each of up to
three different small files. It checks whether a repeated identical range is
stable, whether the shifted range matches, and whether an offset was apparently
ignored. It outputs only status categories and sample counts, not filenames,
secrets, or file data. A pass across samples is **not** validation for every
iCloud Drive file or permission to enable unattended archiving. The downloader
must remain blocked until a robust approach is implemented and live-tested.

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

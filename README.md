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

Before enabling anything, inspect a read-only plan:

    ~/.local/bin/apple-sync-batch drive
    ~/.local/bin/apple-sync-batch photos

When ready **and only after both remote configurations and the plans are reviewed**, explicitly opt in:

    systemctl --user enable --now apple-sync-drive.timer apple-sync-photos.timer

To pause:

    systemctl --user disable --now apple-sync-drive.timer apple-sync-photos.timer
    systemctl --user stop apple-sync-drive.service apple-sync-photos.service

Status: systemctl --user list-timers 'apple-sync-*' and journalctl --user -u apple-sync-photos.service -n 50 --no-pager.

## Download limits

Every run **scans** one remote, plans missing files within a total payload budget, then ends. There is **no two-file cap**: one run may fetch dozens or hundreds of small files. Downloads use rclone copyto on those two specific paths, not rclone sync or an unlimited tree copy. Defaults:

- **0 files downloaded in manual dry-run mode** (omit --execute).
- **At most 128 MiB of listed source payload per service invocation**, regardless of the number of files. Files larger than 128 MiB are deferred and remain outstanding; a separate bounded large-file approach is needed to archive those. Remote sizes are estimates; network traffic can exceed payload because of retries and protocol overhead.
- **Maximum 512 KiB/s**, one concurrent transfer, capped at about **15 minutes total** including listing. Rclone is stopped if the deadline expires; an interrupted file may have to start again. The hard time cap means a slow large file can fail repeatedly; no files are considered complete in that case.
- **No uploads, deletions, or overwrites.** Existing local file paths are skipped. Source deletions never propagate. Changes to a previously archived source file are not captured automatically; historical versioning is future work.
- Reject symlink destinations, unsafe remote paths and unknown sizes. Fail closed if /drive is not a writable ext4 mount. The two iCloud services share a lock and don't overlap each other.
- **Drive timer at :10 and Photos timer at :40 hourly**, timers disabled until explicitly enabled. Existing Google Drive service is not modified; its traffic can coexist with iCloud, so consider aggregate bandwidth before enabling.

**Coverage caveat:** the currently implemented bounded scanner re-enumerates the source on every run. For very large photo libraries, listing may be slow even without downloading media. Oversized files, incomplete Apple API listings and shared library contents need follow-up coverage before an archive can be called complete.

## Development

Offline unit tests:

    python3 -m unittest discover -s tests -v

Version-controlled systemd service/timer definitions are authoritative. Actual rclone auth state and archive content remain outside the public repo.

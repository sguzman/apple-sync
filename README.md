# apple-sync

Linux-first, **download-only**, controlled iCloud archival using local rclone remotes and user-systemd units. Code and unit files live in this repository; **private data lives exclusively under /drive/icloud**. No passwords, 2FA codes, cookies, rclone config, photos, or device backups belong in Git.

## Sources and destinations

| Service | rclone source | Local destination | systemd timer |
| --- | --- | --- | --- |
| iCloud Drive | icloud: | /drive/icloud/drive | apple-sync-drive.timer |
| iCloud Photos primary library | icloudphotos:PrimarySync/All Photos | /drive/icloud/photos/primary | apple-sync-photos.timer (disabled pending validation) |
| Full Photos metadata inventory | icloudphotos:PrimarySync/All Photos | /drive/icloud/.apple-sync-state/photos-primary-inventory.json | apple-sync-photos-inventory.timer (disabled pending validation) |
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

Drive and individual-album commands still list their sources. The full Photos
worker now requires a private, recently refreshed inventory; use
`photos-inventory --execute` to create it. Dry-run mode never repairs
partial data; corrupt or uncommitted staging state is reported for manual
review.

Do **not** enable the Drive and full-Photos timers together on the basis
of a successful inventory. Drive remains blocked by byte-range issues,
and full Photos needs a verified first live download. When those distinct
issues are independently resolved, explicitly opt in to each timer.

To pause:

    systemctl --user disable --now apple-sync-drive.timer apple-sync-photos.timer
    systemctl --user stop apple-sync-drive.service apple-sync-photos.service

Status: systemctl --user list-timers 'apple-sync-*' and journalctl --user -u apple-sync-photos.service -n 50 --no-pager.

## Full-library Photos: cache-first incremental design (2026-11-03)

The earlier full-library worker gave a cold `All Photos` listing only **300
seconds**, then stopped before any media downloads. That was our listing
timeout, not a media transfer failure. The new implementation separates
remote **metadata discovery** from **media download**:

1. Run `python3 scripts/archive_batch.py photos-inventory --execute --defer-while-google`
   to build the initial private full-library inventory. This operation gets
   up to **30 minutes** and downloads **zero photo/video payload bytes**.
   It writes a complete atomic JSON inventory at
   `/drive/icloud/.apple-sync-state/photos-primary-inventory.json` (0600).
   It reports item count and summed file sizes, so we can establish the
   expected amount of media to archive without downloading the media.
2. Ordinary `photos --execute --defer-while-google` runs read that local
   snapshot immediately; they **never issue an All Photos listing**. They
   compare each listed file against the archive and partial-file checkpoints,
   download only missing bytes (128 MiB per pass, 512 KiB/s), and resume
   interrupted large files. Completed local copies remain immutable.
3. An **opt-in** daily `apple-sync-photos-inventory.timer` refreshes
   our private local snapshot by asking rclone for its inventory. Rclone's
   backend uses Apple change tokens and a persistent disk cache, so when
   nothing changes it should not re-download the whole metadata listing
   from Apple. Our script still receives and writes a full **local** JSON
   snapshot at each refresh; it does not make a full cold network request
   on every bounded download pass. The initial remote listing can be slow; later refreshes should
   normally reuse rclone's cache. New media is discovered at the next
   successful inventory refresh, not instantly. A cached manifest older
   than **48 hours** is refused until refreshed: no stale snapshot is
   silently treated as an up-to-date source. The separate bounded archive
   timer is hourly once explicitly enabled after live validation.

The inventory cache never goes into Git. It refuses empty, malformed or unsafe listings; atomic writes prevent
partial refreshes from replacing the previous completed inventory. Apple
can expose distinct assets under an identical visible path: the inventory
retains *all* those records, but the name-based downloader excludes every
ambiguous path and reports their counts until an ID-targeted strategy is
validated. A successful full-library pass is therefore **not** a completeness
claim while any collisions remain unresolved. If a refresh omits any
previously inventoried path (whether due to real cloud deletion or an
incomplete Apple/rclone listing), it stops and keeps the prior inventory
for review rather than silently dropping those records. No cloud deletions
are propagated to the local archive. It may therefore require intervention
after genuine upstream removals or renames.

**Completeness warning:** rclone issue #9955 (September 2026) reports a
Photos pagination bug where a first pass can miss items despite an
apparently successful exit. The initial count must be compared against
the actual Apple Photos library count and spot-checked before enabling
unattended full backup. A successful inventory, tests, or timer run alone
does not prove every Apple asset was listed.

Rclone's native photo metadata cache is under
`~/.cache/rclone/iclouddrive-photos/`; our separate inventory protects
the bounded downloader from repeated listing timeouts. Neither cache is
the media archive. The metadata byte total is a **listed** sum: if a
backend reports wrong sizes, it does not equal verified downloaded bytes.

**Manual validation (full-library and Drive timers remain disabled):**

```fish
git pull --ff-only
bash scripts/install.sh
python3 -m unittest discover -s tests -v
python3 scripts/archive_batch.py photos-inventory --execute --defer-while-google
python3 scripts/archive_batch.py photos-inventory
python3 scripts/archive_batch.py photos
```

The first inventory may be deferred if the Google Drive service is active. Likewise, a full Photos transfer using `--defer-while-google` reports `DEFER` and exits cleanly if Google is busy; this is **not** evidence of downloaded bytes. Any already committed partial chunks remain preserved for the next pass.
Confirm a nonzero item count and plausible total size before trying the
bounded full-library `photos --execute --defer-while-google` run. Do not
enable `apple-sync-photos.timer` or `apple-sync-photos-inventory.timer`
until live tests demonstrate correct source listing, downloaded file
integrity and coexistence with Google backup. iCloud Drive remains blocked
by separate byte-range integrity problems.

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
**disabled**. Its service uses `--defer-while-google`: when the local
`google-drive-rclone.service` is running, the Apple worker defers without
advancing its album cursor; it also rechecks before each download chunk
(approximately every 8 MiB). If the Google service state cannot be read,
the Apple job fails closed. This requires the Google downloader to run
under that exact systemd user unit name. The existing Google repository
and timer are not modified.

This is **best-effort bandwidth coordination**, not a shared global lock.
A Google run that starts midway through an Apple listing or chunk can
temporarily overlap, and if Google stays busy constantly, scheduled
Apple progress may be delayed indefinitely. A future shared scheduler
would be needed for strict exclusivity and fairness.

To test the guarded production entrypoint locally before enabling:

    python3 -m unittest discover -s tests -v
    systemctl --user show google-drive-rclone.service -p ActiveState --value
    timeout 1000s python3 scripts/archive_batch.py photos-progress --execute --defer-while-google

A `DEFER` result means **no downloaded bytes and no cursor advance**;
it is a safe skip, not proof of a successful archive. Check a later pass
when Google is inactive.

After repeated live testing and bandwidth review, the incremental subset
timer may be enabled **independently** of the full-library and Drive timers:

    systemctl --user enable --now apple-sync-photos-progress.timer

Do **not** enable `apple-sync-photos.timer` or
`apple-sync-drive.timer` yet. The original full-library inventory still
needs a reliable first cache build (or a paginated backend), and the
Drive byte-range failure is unresolved. The progress timer is not a
substitute for full-library backup.

## Legacy full-library warmup command

`bash scripts/warm_photos_cache.sh` now invokes the same inventory worker.
It no longer discards the metadata that it spent time fetching, and it
never downloads media. Prefer `photos-inventory --execute` above for
clearer operation and count/size reporting.

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

**Drive archive execution remains blocked in code.** Do not enable the
Drive timer. The independent Photos archival timer was enabled and its first
unattended 2026-11-03 run was confirmed to finish successfully; that does not
establish iCloud Drive integrity.

To classify the Drive failure without modifying the remote or archive:

    git pull --ff-only
    python3 -m unittest discover -s tests -v
    timeout 210s python3 scripts/diagnose_drive_ranges.py

The original range-only diagnosis reads three small ranges (offsets 0, 1, 0)
from up to three files. It checks repeatability, overlap and ignored offsets.
The next, more informative **read-only comparison** is:

    python3 scripts/diagnose_drive_downloads.py

This tries at most four 4-KiB-or-larger files, each with a listed size no
greater than 1 MiB, from named iCloud Drive subfolders only. It reads two
whole copies and three small ranges per file (offsets 0, 1, 0), classifies
whether full copies agree, whether their sizes match the listing, and whether
ranges agree with the full copy. After two consistent ordinary-file samples
on 2026-11-03, the expanded follow-up is:

    python3 scripts/diagnose_drive_downloads.py --extended --wait-idle

With `--wait-idle`, the diagnostic first waits up to 12 minutes for an idle
window instead of immediately giving up if Google Drive or a Photos worker is
active. It never stops or reconfigures those services. It checks the full
Photos, progress-album, inventory-refresh and Google Drive workers; it also
rechecks between scopes/files, and may still print `DEFER` if the next
backup starts while it is running. A deferred diagnosis is not evidence of
Drive integrity; rerun during a quieter window if necessary. Allow up to
19 minutes total for an idle wait and the bounded diagnostic.

The expanded mode samples up to eight files from the same limited directory
scopes (maximum four per scope), spreads selection across smaller/larger files,
and compares the whole-file copies to overlapping ranges as large as 16 KiB
plus a middle-of-file read. The 16-KiB test mirrors the size of the original
mismatched range probe, which later smaller samples did not reproduce. It
checks local Google and Apple Photos backup activity before and during probing,
and defers if either is running or its state cannot be checked. It caps total
wall time to 420 seconds and performs no archive writes. It does not scan the
entire iCloud Drive and may still miss problematic formats or folders.

Both modes suppress remote stderr, never print file names or content, and cap
response memory and runtime; they make no remote mutations or archive writes.
An iCloud Drive source could still change during tests; identical full copies
are **not** proof of original correctness, and no sample result authorizes
Drive archiving.

The diagnostic intentionally returns success after producing classifications,
even if the results show a mismatch; read its final **Drive archive remains
BLOCKED** line rather than treating exit code 0 as backup clearance.

### Quarantined Drive staging verification (2026-11-04)

The 2026-11-04 extended diagnostic completed with **5/5 ordinary files**
reporting stable whole-file reads, matching advertised sizes, consistent 16-KiB
overlaps and middle-of-file ranges. No iWork package was included and the
older failing sample was not conclusively reproduced. Drive remains blocked.

The next validation checks more than remote ranges: it exercises the actual
archive's chunk download, SHA-256 checkpoint, resumed state verification,
and no-overwrite atomic publication on up to two small Drive files:

    python3 scripts/verify_drive_staging.py --wait-idle

The script reads limited non-recursive-to-root folder scopes, chooses eligible
64-KiB-to-8-MiB files, downloads each whole file twice, reconstructs it in
multiple bounded chunks with the production staging helpers, then compares the
assembled file's SHA-256 against both independently downloaded whole copies.
**Every staging file and synthetic published test file is under a disposable
temporary directory; the real archive and all user-systemd units are untouched.**
Private source filenames, hashes and bytes are never printed. The test waits
up to twelve minutes for an idle window if requested, then has an eight-minute
source-read budget. It stops if another backup becomes active. Network reads
do not modify iCloud; no iCloud Drive timer is enabled by this test.

Results are classifications, not authorization to run a full unattended Drive
backup. Whole files matching each other cannot prove fidelity to Apple's
original when the remote does not supply a trusted content hash. Specialized
iWork documents and bigger files still need dedicated testing.
If it reports range inconsistency with stable full downloads, investigate a
whole-file downloader that strictly validates file identities and stages writes
atomically. If full lengths differ from listing metadata, classify Apple
packages separately rather than globally suppress size verification.

## Persistent iCloud Drive metadata inventory (opt-in)

After a verified one-file permanent-archive pilot on 2026-11-04,
`scripts/drive_inventory.py` implements the next required component:
a **private, persistent full iCloud Drive metadata snapshot**, stored at
`/drive/icloud/.apple-sync-state/drive-file-inventory.json`. It distinguishes
regular files with known sizes, unknown-size (including Apple-native) files,
directories, and repeated/ambiguous paths; it retains all records for later
assessment, including ineligible ones.

To explicitly create/refresh it (metadata only, **no file downloads**):

    python3 scripts/drive_inventory.py --execute --wait-idle

To report the previously saved counts and size without contacting Apple:

    python3 scripts/drive_inventory.py

The first attempt exposed an operational flaw: on 2026-11-04 a scheduled
backup became active during a long recursive enumeration, the scan deferred,
and no finished snapshot existed. The subsequent status command returned
`INVENTORY_NOT_CREATED` even though no source data was damaged.

The inventory now uses a **resumable per-folder breadth-first traversal**,
with one nonrecursive `rclone lsjson` request per folder. After each folder
is successfully listed, its records and remaining directory queue are saved
atomically under `/drive/icloud/.apple-sync-state/drive-inventory-progress.json`
(mode 0600). Future executions resume at the first outstanding folder; they
do not relist folders already checkpointed. A folder interrupted by an active
backup is retried later, while completed folder records are preserved.

A pass has a 12-minute enumeration budget, with a two-minute per-folder
limit, an 8-MiB response ceiling per folder, a 128-MiB progress manifest
limit and a maximum of 250,000 records. It optionally waits up to twelve
minutes for Google Drive and Photos workers to be idle. The Apple archive
lock coordinates with Photos workers without disabling or reconfiguring
them. The final complete inventory is installed only once **every reachable
folder** was successfully enumerated and validated. If an expected source
record disappears, a failed refresh does not replace the previous snapshot.

On a partial result, the command prints `Drive inventory PARTIAL` with the
number of folders completed, folders pending and records checkpointed.
The status command reports both an existing complete snapshot (if any) and
partial checkpoint counts; `NO_COMPLETE_SNAPSHOT` is informative, not a
failed read. A partial folder walk **is not a full inventory**.

This reduces repeat work after interruptions, but each new folder still
requires Apple API calls. It does not establish perfect remote coverage,
and frequent full refreshes are still inappropriate.

**Critically, a normal backup worker must never enumerate the entire Drive
before each download pass.** This snapshot is designed to be reused locally
by future batches. Unlike Photos, there is no independently established
cheap, persistent iCloud Drive change-token listing cache in our current
backend. Accordingly, **no recurring full inventory refresh is enabled**:
frequent full rescans would reproduce the original inefficiency. An
optional timer can continue the initial checkpoint only, then do no more
remote work after the first complete snapshot. Refresh cadence/delta
handling remain engineering tasks. A successful listing still does not
prove Apple exposed every cloud asset, nor does a snapshot authorize
unattended Drive downloads.

### Optional hourly resume timer (not a download timer)

The initial folder walk reached **340 folders completed, 729 pending and
2,699 records checkpointed** on 2026-11-04. New folders continue to be
discovered, so no overall completion percentage is valid yet.

To avoid repetitive manual Jobflick jobs, the new
`apple-sync-drive-inventory.timer` starts the inventory worker at minute
**05 of each hour**, away from the existing Photos starts at minutes 25
and 40. The service uses `--execute --resume-only`, a 12-minute per-pass
enumeration budget, low I/O/CPU scheduling priority, the existing archive
lock, and the active-backup deferral checks. If the worker finds the
first complete inventory already present and no pending checkpoint,
it exits **without making any remote calls**. It does not trigger
`drive_cached_batch.py`, the unrestricted Drive archive or any Photos
modification.

The installer adds symlinks to this **optional** service and timer but
**does not enable it**. After local unit tests pass:

    bash scripts/install.sh
    systemctl --user enable --now apple-sync-drive-inventory.timer

Read the saved progress at any time without contacting Apple:

    python3 scripts/drive_inventory.py

Or verify the scheduled unit independently:

    systemctl --user list-timers apple-sync-drive-inventory.timer

Disable just the inventory-resume timer (and leave Photos unaffected):

    systemctl --user disable --now apple-sync-drive-inventory.timer

This timer's one-time initial enumeration may still produce many Apple
metadata requests before it completes. It is **not** an incremental or
change-token-backed future inventory refresh. Continue treating Drive
download automation as blocked until explicitly validated.

## Local Drive coverage report (no Apple requests)

Use the current private metadata snapshot or an **incomplete checkpoint** to
measure size and type coverage before deciding which transfer mechanisms to
develop next:

    python3 scripts/report_drive_coverage.py

This is entirely local and read-only. It prints aggregate counts for known
and unknown-size files, empty and small files, 8–64 MiB, 64 MiB–1 GiB and
larger files, Apple-native document records, repeated/ambiguous file paths,
and potential 64 KiB–8 MiB unique ordinary-file candidates. It also reports
known-size and potentially eligible byte totals. It never outputs source
filenames, hashes, or private file contents and never contacts rclone or
systemd. With the in-progress folder walk, it labels all totals as **partial**,
since the number of yet-undiscovered folders is unknown.

This report makes no changes to the inventory, Photos, archive files,
automations or timers. Its size buckets are for planning, not approval
to download any unsupported file type.

## Read-only archive audit and durable verification receipts

The backup worker skips existing destination paths to prevent overwrites,
but **existence alone is not evidence of a verified backup**.
`scripts/audit_drive_archive.py` compares archived files against the
currently available partial or completed Drive inventory:

    python3 scripts/audit_drive_archive.py

It checks that `/drive` is mounted, rejects archive symlinks, and counts
missing files, wrong-size files, ambiguous paths, and matching-size files.
It does not modify archive files, make Apple requests, or touch systemd.
Only aggregate counts are printed—never private filenames or hashes.

New `pilot_drive_archive.py` and `drive_cached_batch.py` **live
publications** also write a private, write-once receipt under
`/drive/icloud/.apple-sync-state/drive-verification-receipts/`. The
receipt binds the remote source identity (path, size, mtime, ID and any
advertised hashes) and exact SHA-256 to the *newly published* local file,
and is written **only after** two whole-file downloads match, a fresh
parent-folder listing confirms source identity, the stage passes its
checkpoint/hash check, and atomic no-overwrite publication plus local
SHA verification both succeed.

Receipts use mode 0600 under mode-0700 directories. Existing receipts are
never silently overwritten. A competing file that already exists at the
destination does not get a new receipt. If publication succeeds but
writing the receipt fails, the file remains intact **without certification**;
the archive skips future overwrites and the audit continues to classify it
as unverified. Receipt-write errors are handled without exposing filenames.

For a file with a valid receipt, the audit **re-hashes current local
contents** and compares source metadata, size, and SHA-256 before counting
it as `Files with valid dual-whole-read receipt and matching local SHA-256`.
Corrupt/unsafe receipts, stale source identity, or same-size local tampering
are listed separately and **never counted as verified**. Files with matching
size but no valid receipt remain explicitly unverified.

The first real iCloud Drive file archived on 2026-11-04 (77,256 bytes)
predates receipts and is **not retroactively certified**. Its pilot
succeeded at that time, but its present contents will require an explicit
re-verification against independently read remote bytes before a receipt
can be issued. The initial read-only audit on 2026-11-04 found one
size-matching file and 1,630 missing among 1,631 discovered files; these
figures describe only the **incomplete 340-folder checkpoint**.

The receipts attest two matching *Apple-visible* remote reads; they are
**not signed by Apple and not a substitute for a trusted source checksum**,
nor are they evidence of full backup coverage. The old unrestricted Drive
downloader stays blocked, and no timer has been enabled for archive writes.

## Existing Drive archive re-verification (explicit opt-in)

The first successful 77,256-byte Drive pilot predates durable SHA-256
verification receipts. Its bytes must remain intact; a matching file size
is insufficient to create a receipt retroactively.

The new `scripts/reverify_drive_existing.py` previews which **already
present, size-matching ordinary files up to 8 MiB** are missing a receipt:

    python3 scripts/reverify_drive_existing.py

Preview is local and read-only. The script requires a **complete and
recent** saved Drive inventory even in preview mode. If the initial
folder walk is still underway, it says `BLOCKED` and makes no Apple
requests. It never uses the incomplete checkpoint to start downloading.

When the inventory is complete and this implementation has passed QA,
a separate opt-in one-file verification may be run using:

    python3 scripts/reverify_drive_existing.py --execute --wait-idle

**This live command has not yet been approved.** It selects at most
one existing file, checks its inode/type/size and local SHA-256,
downloads the remote file **twice in full** with the existing 8-MiB per
read cap, confirms both remote copies match the original local hash,
checks the current parent-folder metadata/identity without recursively
listing Drive, then rechecks the same local inode and SHA-256 before
creating a new private immutable receipt. It never opens, overwrites,
replaces, or removes the existing archive file. Existing or invalid
receipts must be reviewed, not replaced. If either remote read or
metadata verification fails, it emits a sanitized deferral without
creating a receipt.

The same shared archive lock and active-Photos/Google deferral rules
apply. This is a **manual recovery of evidence for legacy archived
files**, not a file downloader, scheduler, remote checksum oracle, or
substitute for a complete Drive inventory.

## Cached-inventory Drive batch (experimental; opt-in only)

`scripts/drive_cached_batch.py` prepares the next phase without enabling the
existing unrestricted `apple-sync-batch drive --execute` or a timer.

    python3 scripts/drive_cached_batch.py

This preview uses **only** a complete, private Drive inventory younger than
30 days, with no Apple network calls, and never tries to use a partial folder
checkpoint. The completed inventory is necessary; `NO_COMPLETE_SNAPSHOT`
or `INVENTORY_NOT_CREATED` means the batch stays blocked.

Once the inventory has completed and the verifier has passed local QA,
the separate, explicitly opt-in command is:

    python3 scripts/drive_cached_batch.py --execute --wait-idle

It processes at most two missing ordinary files per pass, with at most
three attempts, each **1 byte to 8 MiB**, and reserves at most 24 MiB for
independent duplicate whole-file reads. Files of zero bytes are deliberately
deferred until empty-object semantics are verified; larger files, native
Apple formats, unknown sizes, repeated paths and existing archive files
are excluded. The pilot command described below still uses its original
64 KiB minimum; this broader range applies to the *cached worker* only.
Each file must have two matching whole downloads, a current and unique
metadata identity from a single **nonrecursive parent-folder listing**,
a valid checkpoint, and a no-overwrite atomic publication. The Apple archive
lock prevents concurrent state mutations, and the job defers while Photos
or Google Drive backups are active. File names are omitted from logs.

### Opt-in empty-file profile (zero-byte objects, not automated)

The partial inventory has five actual zero-byte file records. Such objects
cannot be safely inferred from an empty response alone: an interrupted
or failed download could also return no bytes. The new `--empty` profile
therefore exercises the same **two separate successful whole-file reads**
as other cached workers, and verifies both results against the exact
SHA-256 digest of the empty byte string. The underlying rclone subprocess
must return success for each read. It then requires unchanged,
unambiguous **file-not-directory** metadata from a fresh nonrecursive
parent-folder listing before atomically publishing one zero-byte file.
It never creates an empty placeholder merely because the remote read failed.

The preview makes no cloud requests or archive writes:

    python3 scripts/drive_cached_batch.py --empty

The separate live form, `--execute --wait-idle --empty`, is
**not yet approved**; only one zero-byte file may be published per
run after full-inventory completion and independent QA. The `--empty`
and `--large` modes are mutually exclusive. Zero-byte files remain
excluded from the ordinary small/large profiles. Empty native iWork
documents, ambiguous paths, incomplete inventories, existing destination
files and symlink targets are excluded. No timers are changed.

This verifies reproducibility of Apple-visible empty bytes, not that
Apple's backend metadata always reflects the intended original content.
The remote Drive completeness issue remains independently unresolved.

### Opt-in large-file profile (8–64 MiB, not automated)

The local-only coverage scan identified **59 files above 8 MiB through
64 MiB**, totaling much of the as-yet-indexed Drive bytes. An additional
`--large` mode now selects these ordinary, unambiguous, known-size files
without changing the normal small-file cap:

    python3 scripts/drive_cached_batch.py --large

The above command is a **read-only preview** using the same fully complete
inventory prerequisite as the ordinary cached batch. It performs no cloud
requests or archive writes. Once the full inventory and new tests are
verified, an explicitly requested live large-file test can use
`--execute --wait-idle --large`; do not schedule or run that command
until local QA is reviewed.

The large mode handles **at most one file per pass** (up to two attempts).
It permits files larger than 8 MiB and no larger than 64 MiB. It permits up
to **128 MiB of source reads reserved per pass**, because each candidate
is retrieved twice as an independent, rate-limited whole-file download;
each whole-file read enforces its own hard 64-MiB cap. The two files must
match in size and SHA-256, the source's parent-folder metadata must still
match, and publication uses the existing private checkpoint and atomic
no-overwrite path. The same shared lock, active-backup deferral, bounded
runtime and output privacy protections apply. The existing 8-MiB default
cap is unchanged unless `--large` is specified.

**Limitations:** these two matching whole downloads do not prove original
Apple bytes without a trusted provider checksum. Native Apple documents, unknown-size files, and files above 64 MiB remain
blocked; zero-byte files are opt-in only through the separate empty profile;
large files do not yet resume a partially downloaded whole-file transfer
across runs. The full Drive download timer remains disabled.

The ordinary pass performs **no full-root Drive listing**. It never modifies
iCloud and does not change any systemd unit. The existing unrestricted Drive
timer remains disabled; full coverage, large-file support, Apple-native files,
and robust inventory freshness remain future work. A successful limited
batch must not be mistaken for a full backup. Do **not** run the live
`--execute` form until the complete inventory and tests are independently
verified.

### 2026-11-04 local inventory coverage

From the 340-folder partial checkpoint, the local-only report found
**1,631 file records**: 705 between 64 KiB and 8 MiB, 862 under 64 KiB
(including five empty files), and 59 above 8 MiB through 64 MiB. No
Apple-native, unknown-size, or path-collision file records were seen in
this **partial** snapshot. Total known-size bytes were **1,958,930,705**.
The 705 previously eligible files contained **816,070,419 bytes**.

Because the majority of discovered files were below the earlier minimum,
the cached batch now includes nonempty files as small as one byte; it still
requires two matching whole downloads, stable parent-folder identity,
verified staging and atomic no-overwrite publication. On this partial
checkpoint that makes **up to 1,562 of the 1,631 discovered file records**
potentially eligible by type/size/path, ignoring whether some were already
archived. This is a candidate count, **not evidence of files backed up**.
The actual saved inventory remains incomplete, so the batch is still blocked
from running against these records. Empty files and the 59 large files
will need separate validation.

## Controlled permanent iCloud Drive pilot (2026-11-04)

The 2026-11-04 quarantined Drive staging test passed **72/72 total unit
tests** and successfully reconstructed two real ordinary files of 7,599,366
and 4,047,012 bytes. Both reconstructed files matched two independently
downloaded whole-file copies. The test used temporary paths only.

The next opt-in step permits **at most one ordinary Drive file**, between
64 KiB and 8 MiB, to be published to its correct location beneath the real
`/drive/icloud/drive/` archive. This pilot does not unblock the existing
`archive_batch.py drive --execute` or enable the Drive timer:

    python3 scripts/pilot_drive_archive.py
    python3 scripts/pilot_drive_archive.py --execute --wait-idle

The first command previews source eligibility without writing archive data.
The second waits for other backups to be idle, obtains the shared Apple
archive lock, retrieves **two whole copies** through rclone, and compares
their sizes and SHA-256 hashes. It re-lists only that small source scope to
confirm the chosen record's identity is unchanged; it then commits the
verified bytes via the existing archive checkpoint code to a private staging
directory on the same filesystem and publishes using the archive's atomic
hardlink/no-overwrite helper. Once published, it checks the final archive's
size and SHA-256. The pilot never requests remote byte ranges. It refuses
symlinks or existing targets; it does not delete existing archive files.
Temporary staging data is cleaned up on normal exit, and a forced process
termination could leave incomplete private staging folders for review.

Only one file is attempted per invocation. The pilot deliberately excludes
native iWork documents and large files pending more testing. It scans only
selected bounded scopes (`Desktop`, `Downloads`, `read`), not all iCloud
Drive, so **pilot success is not evidence of complete cloud coverage**.
The full Drive timer stays disabled. Results are printed without private
filenames, even when the archive helper publishes a file.

## Live first full-library media pass (2026-11-03)

After the first persistent inventory succeeded (20,566 records, 52,483,863,139
listed bytes, 26 ambiguous records), a real bounded full-library run
completed **18 files**, then rclone returned exit status 5 and a short
8 MiB response (7,962,624 bytes rather than 8,388,608). The downloader
correctly rejected the incomplete response; the 18 fully published
files remain in the immutable archive, and the affected file was not
published as complete. This motivated bounded retry with adaptive smaller
ranges and a per-pass request budget that includes failed attempts.
Later live Photos tests passed (41 unit tests, two consecutive successful
bounded transfers, and a successful first unattended run ending at 19:55 on
2026-11-03). The Photos timer was enabled locally. This is separate from
the known iCloud Drive overlap mismatch, which remains blocked.

## Download budgets and persistent resumption

The Photos and daily inventory refresh timers were **enabled locally** on
2026-11-03 after successful bounded live Photos runs. The iCloud Drive timer
remains **disabled** pending integrity work. The separate incremental album
timer was also observed active and may duplicate canonical archive work;
it has not been modified by this Drive diagnostic. A full Photos run uses the
locally cached inventory (not a fresh remote listing), then advances
missing files using read-only rclone byte-range requests. It does not rely on interrupted rclone copy files resuming automatically.

- **128 MiB maximum requested payload per hourly pass**, unlimited number of small files within that byte budget.
- **512 KiB/s rclone transfer cap**; up to eight-MiB source byte-range chunks. A short/failed range is discarded and retried at the same offset with a smaller request (up to four attempts, with short backoff). If all attempts fail, its verified partial remains staged and the pass can continue to other media. The 128 MiB source-request budget also counts unsuccessful attempts; HTTP/protocol overhead is additional.
- **15-minute maximum execution time** including listing.
- **No maximum file size:** a 2-GiB video is staged in 128-MiB hourly installments and atomically published only after complete. When a run stops, completed chunks remain under /drive/icloud/.apple-sync-state; the next run checks the last chunk's SHA-256 and resumes at the recorded byte offset.
- Both remotes share a non-overlap lock; previously started large transfers are prioritized over new files.
- Final archive files cannot be overwritten or deleted by this downloader. Remote deletions never propagate.
- The script refuses to resume a changed source if listed size, timestamp, ID or hashes differ from the saved metadata. Such partials are retained for manual resolution.
- Network traffic includes protocol overhead and can exceed the requested payload cap. A failed or short chunk is **never** counted as durable progress: its temporary bytes are discarded. A repeatedly failing file is explicitly reported as deferred; other files can advance without overwriting existing archive content.
- **Live end-to-end validation is still needed.** Rclone's iCloud Photos backend is experimental; whether byte-range requests work correctly for the actual Apple file URLs must be verified before enabling schedules. The Apple API may also omit items from listings. Source hashes are not always available, so full end-to-end cryptographic verification is not always possible.
- Timers are staggered at :10 (Drive) and :40 (Photos), but the separate Google Drive downloader might still overlap. Do not enable the new timers until total bandwidth is reviewed.

## Development

Offline unit tests:

    python3 -m unittest discover -s tests -v

Version-controlled systemd service/timer definitions are authoritative. Actual rclone auth state and archive content remain outside the public repo.

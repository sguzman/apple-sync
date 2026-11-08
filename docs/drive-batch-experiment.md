# Experimental batched iCloud Drive metadata transport

Status: **offline implementation only; live Apple use is not authorized or
integrated**. This is an isolated performance experiment, not a new backup
mode. The existing one-pass metered service continues to use the original
persistent rclone RC reader and saved checkpoint.

## Why this is possible

Rclone's iCloud Drive API already implements
`GetItemsByDriveID(ctx, ids, true)`, which issues one read-only
`retrieveItemDetailsInFolders` request for a slice of Drive IDs. The ordinary
`Fs.List` path requests one directory at a time. See the exact rclone version
before building; do not assume the workstation version matches current
upstream source.

## Current modules

- `scripts/drive_pending_id_audit.py` checked all 1,420 pending folders for
  unique candidate directory IDs at the last local snapshot. This is a
  prerequisite, **not** a completeness guarantee.
- `scripts/drive_batch_contract.py` plans batches up to eight folders and
  rejects missing, duplicate, or incomplete response envelopes.
- `scripts/drive_batch_equivalence.py` compares rclone-visible file/directory
  identity, normalized name plus extension, type, ETag, and size against a
  separate conventional per-folder reference. It never writes a checkpoint.
- `rclone-addon/iclouddrive/apple_sync_batch.go` implements an *opt-in*
  `apple-sync-read-batch` backend command. It uses rclone's authenticated
  session and pacing rather than taking Apple passwords or cookies into
  Python. It does not download media or issue cloud write requests.
- `scripts/drive_batch_transport.py` sends the fixed backend command in
  JSON over an already running, user-private 0700 Unix socket. IDs stay out
  of process arguments and URLs. Responses are capped at 8 MiB; request
  size is capped at 16 KiB and timeout at 30 seconds. It never starts rclone,
  bypasses the budget, or mutates archive state.

## Deliberate isolation and release gates

The Go file has the build constraint
`apple_sync_experimental && !plan9 && !solaris`. Only a separately compiled
rclone test binary built with this tag can expose the new command. The stock
rclone executable is untouched. Because rclone's `fs.Features.Fill` detects
optional `fs.Commander` implementations, adding the file inside a matching
upstream `backend/iclouddrive` package should advertise the command without
editing rclone's existing source.

**This Go addon has not yet been compiled against a pinned local rclone
checkout.** Avoid claiming Go tests or source compatibility until that build
and test are actually run. We have not verified Apple's live batch response,
pagination, returned child count, response ordering, metadata equivalence,
network efficiency, or error semantics.

Before any Apple network experiment:

1. The workstation reports **rclone v1.75.2** (2026-11-08).
   `scripts/build_rclone_batch.py --execute` is now the isolated build
   launcher. It fetches public source from `rclone/rclone` tag `v1.75.2`
   and **requires** commit `be8d6044eacb843b74d335db0c5fbe09222031fe`.
   It copies only the two addon files into a private source checkout under
   `~/.cache/apple-sync/rclone/v1.75.2`, formats them, runs the targeted Go
   tests, then builds an isolated binary at
   `~/.local/share/apple-sync/experimental/rclone-batch-v1.75.2`.
   The script does not touch the installed rclone binary or PATH.
   Without `--execute` it only prints the plan.
2. Validate the **actual output** of the Go test/build and the existing
   Python suite on the workstation. The build fetches public GitHub sources
   and Go modules but makes no Apple requests. Reject any upstream API
   differences instead of adapting blindly. Never treat a successful build
   as authorization to scan.
3. Implement a new, deliberately opt-in system-metered probe with the existing
   exclusive backup lock, backup-idle checks, root-owned ledger, soft bandwidth
   cutoff and no checkpoint writes. The existing manual one/eight-pass service
   files must not change implicitly.
4. Compare *the same one or two folders* with the current rclone reader and
   the batch command in one bounded measured window. Do not log raw names,
   paths, IDs, account data or Apple errors. Explicitly report incompleteness
   and the amount of measured IP ingress and egress.
5. Only after identical records and a useful efficiency gain are demonstrated
   on real data, design a separately gated resumable batched scanner. Preserve
   exactly the existing checkpoint semantics, fail-closed rules and fallback
   to the unmodified one-folder reader.

A full inventory is **not** complete simply because an API returns HTTP 200,
a folder has a plausible number of children, or the Python tests pass.
Never advance a pending folder merely because it appeared in a batch.

## Recovery after interrupted builds (2026-11-08)

The first workstation attempt passed all 319 Python tests and the targeted
Go addon tests, then exited after starting the full Go build. Its unhelpful
`LOCAL_BUILD_ERROR` may indicate the former 15-minute timeout, but the
available log cannot prove that was the sole cause. It is **not** evidence
that any iCloud data was modified.

The build launcher now holds a nonblocking exclusive lock in its private
source-cache directory. A second invocation reports
`BUILD_ALREADY_RUNNING_NO_CHANGES` without touching source, Go caches,
binaries or archives. Locks release when the owning process exits, including
after interruption. Prior gofmt-normalized addon files are accepted if
identical to the canonical originals; unexpected edits are rejected.
A matching already-built experimental binary is checked and reused
rather than overwritten. Go build timeout is raised from 900 to 3600
seconds, and timeouts report the failed stage by name. When a subprocess
fails or is interrupted, the launcher terminates its entire process group
so compiler children do not remain running.

The next workstation attempt passed the targeted Go backend tests and
completed `go build`, but the launcher refused to publish the temporary
binary with `EXPERIMENTAL_BINARY_VERSION_OR_TAG_MISMATCH`. This was a
**verification implementation bug**: rclone's `version` command reports its
own curated `buildinfo.Tags` (which may say `go/tags: none`), not arbitrary
`go build -tags` values. The verification now checks the exact
`rclone v1.75.2` identity **and** reads the compiled binary's module path
and `-tags=apple_sync_experimental` entry using `go version -m`. Both are
required before publishing or reusing the isolated binary. Offline tests
cover truthful `go/tags: none`, wrong/missing tags, wrong module, wrong
version and duplicate build-information fields. The previous unsuccessful
temporary binary was not installed. Its Go compilation cache can still
be reused by the next build; no reset is needed.

Neither the repair nor its tests remove the existing cache, alter the
installed `/usr/bin/rclone`, or touch cloud archive state. If the next
isolated build fails, retain its stage-specific error; **do not clean or
reset either the iCloud checkpoint or the metering ledger**. The actual
tagged binary and live API behavior still need validation on the workstation.

## Offline tests

Run from the checked-out `apple-sync` repository:

```fish
python3 -m unittest discover -s tests -q
python3 scripts/drive_metered.py status
```

These steps make no Apple requests. No command in this document authorizes
the new live transport. Do not request an eight-pass scan while evaluating
batch API design.

Last verified local checkpoint (2026-11-08): 1,606 finished, 1,420 pending,
10,455 records; measured remaining budget 48,739,583 bytes and pending=no.
These are historical figures until the workstation reports a newer status.

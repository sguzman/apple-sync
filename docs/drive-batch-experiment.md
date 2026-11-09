# Experimental batched iCloud Drive metadata transport

Status: **experimental binary compiled and verified; live batch testing
not enabled**. This remains an isolated performance experiment, not a new
backup mode. The existing one-pass metered scanner is unchanged and continues
to use its original persistent rclone RC reader and saved checkpoint.

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
  size is capped at 16 KiB and timeout at 30 seconds. It now offers a raw
  native-envelope result (after validating the whole batch), needed for an
  independent per-folder comparison. It never starts rclone, bypasses the
  budget, or mutates archive state.
- `scripts/drive_batch_probe.py` is a **pure, offline orchestration function**:
  it selects at most two pending IDs, asks injected callbacks for one native
  batch and ordinary nonrecursive rclone reference listings, and rejects any
  difference in the identity/name/type/ETag/size checks already implemented.
  It refuses to continue on any active or uncertain backup status. No CLI,
  Apple requests, file writes, or systemd service are exposed by this module.
- `scripts/drive_batch_probe_worker.py` acquires the same archive lock
  as the existing scanner, verifies the checkpoint hash is unchanged,
  requires a metered systemd execution context, and performs a two-folder
  native-versus-legacy comparison. It prints only aggregate counts.
- `scripts/metered_drive_batch_probe.py` is the separate root controller.
  It reserves and reconciles the *existing* cumulative IP ledger; its
  transient service has IPAccounting and `KillMode=control-group`. The
  controller stops the entire cgroup before final ledger accounting.
  A cutoff or time limit is **not** counted as successful equivalence.
- `packaging/apple-sync-drive-batch-probe.service` and
  `scripts/drive_batch_probe_launcher.py` make the experiment manual-only.
  The installation manifest and start-only Polkit rule include this exact
  new unit; no timer or autostart is configured. No installation or live
  request has been performed.
- `scripts/drive_inventory_rc.py` retains `rclone` as the scanner default
  and accepts an optional verified experimental binary when called by a future
  isolated comparison worker. Its private daemon and legacy listing client
  then use the same binary. This change does not enable a new reader in the
  installed service.

## Deliberate isolation and release gates

The Go file has the build constraint
`apple_sync_experimental && !plan9 && !solaris`. Only a separately compiled
rclone test binary built with this tag can expose the new command. The stock
rclone executable is untouched. Because rclone's `fs.Features.Fill` detects
optional `fs.Commander` implementations, adding the file inside a matching
upstream `backend/iclouddrive` package should advertise the command without
editing rclone's existing source.

**The experimental Go addon and standalone rclone v1.75.2 binary
successfully compiled and passed tag/module/version checks on the
workstation on November 8, 2026.** All 334 Python tests passed at that
checkpoint. New probe-oriented Python changes committed since then have
not yet been locally rerun. Apple's live batch response, pagination,
returned child count, ordering, metadata equivalence, network efficiency
and error semantics still have not been verified.

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
3. The separate two-folder worker and root-metered controller now exist
   in `scripts/drive_batch_probe_worker.py` and
   `scripts/metered_drive_batch_probe.py`, respectively. The root controller
   reuses the existing exclusive root-owned cumulative ledger, protects the
   archive with the original `.apple-sync.lock`, waits for backups to become
   idle and starts a time-limited, IP-accounted transient user service.
   Its **sampled** traffic cutoff is 1,500,000 bytes (not a hard quota);
   it commits observed traffic only after the worker cgroup stops, and
   leaves interrupted/unmeasurable runs pending. The worker never advances
   inventory checkpoints. The existing production scan implementation is
   unchanged. The new probe unit is **not installed or enabled**, and the
   new Python tests have **not** been verified on the workstation yet.
   The next step is to verify the entire Python test suite before installing
   or explicitly starting this separate manual-only service.
4. Compare *the same one or two folders* with the current rclone reader and
   the batch command in one bounded measured window. Do not log raw names,
   paths, IDs, account data or Apple errors. Explicitly report incompleteness
   and the amount of measured IP ingress and egress.
5. Only after identical records and a useful efficiency gain are demonstrated
   on real data, design a separately gated resumable batched scanner. Preserve
   exactly the existing checkpoint semantics, fail-closed rules and fallback
   to the unmodified one-folder reader.

## Local acceptance sequence (no full scanner resume)

**Step A — offline tests only.** After pulling the current branch, run
`python3 -m unittest discover -s tests -q` and
`python3 scripts/drive_metered.py status`. Current expected count is
**374 tests**; confirm it from the actual workstation output. No Apple
requests or system changes happen in this step.

**Step B — deliberate static installation, only after tests pass.**
The existing administrator-authorized `drive_metered.py install` installer
now also deploys a third, separate
`apple-sync-drive-batch-probe.service` and its root-owned controller.
This needs the user's explicit Polkit action; installation alone does not
start anything. The existing one- and eight-pass service definitions stay
the same and no timers are enabled.

**Step C — ONE deliberately metered live probe.** Only after Step B is
confirmed, `python3 scripts/drive_batch_probe_launcher.py run` requests
the exact static service using the start-only Polkit policy. The controller
waits for backups to be idle, reserves the existing cumulative ledger,
checks systemd IPAccounting, reads at most two pending folders once through
the experimental API and once through ordinary rclone, and records only
aggregate matching status and measured ingress/egress. It stops and
accounts for worker traffic even on a comparison failure. It NEVER saves
an inventory checkpoint. The sampled 1.5 MB cutoff is advisory and may
overshoot. Never invoke the worker's `--execute` directly.

**Step D — decide based on real data.** If equivalent, separately assess
traffic efficiency (the first probe measures the **combined** native and
legacy traffic, not their isolated costs), timestamp fidelity, and reliable
retry behavior before enabling batches in a resumable scanner. If the
comparison fails, leave the original scanner untouched and investigate
without resetting saved progress or guessing away 1,420 folders.

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
binary with `EXPERIMENTAL_BINARY_VERSION_OR_TAG_MISMATCH`. Two distinct
causes were identified in the pinned rclone v1.75.2 source and original
launcher: rclone's `version` command reports a curated `buildinfo.Tags`
(which may say `go/tags: none` even when compiled with arbitrary custom
Go tags), and the upstream `fs.VersionSuffix` defaults to `DEV` unless
the official release version is stamped at link time. The old launcher
mistakenly expected both `rclone v1.75.2` and a custom tag in rclone's
printed `go/tags`.

The launcher now stamps `fs.Version=v1.75.2` using the same Go linker
variable exposed by the upstream release Makefile, without editing source.
Verification checks the exact `rclone v1.75.2` identity **and** uses
`go version -m` to inspect the compiled executable's module path and
`-tags=apple_sync_experimental` metadata. Both must match before a binary
is published or reused. Offline tests cover truthful `go/tags: none`,
wrong/missing tags, wrong module, wrong version, duplicate metadata fields,
and the explicit release linker flag. The previous unsuccessful temporary
binary was not installed. Its Go compilation cache can still be reused by
the next build; no reset is needed.

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

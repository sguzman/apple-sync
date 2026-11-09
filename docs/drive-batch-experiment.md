# Experimental batched iCloud Drive metadata transport

Status: **isolated binary built; two-folder identity probe verified;
stricter timestamp/core-field probe awaiting offline and live verification**.
This remains a manually metered experiment, not a new backup mode. The existing one-pass metered scanner is unchanged and continues
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

## Live probe #1: measured but journal evidence missing (2026-11-08)

The first explicitly authorized comparison consumed **232,137 measured bytes**
(178,312 ingress, 53,825 egress). The cumulative ledger safely advanced
to **51,492,554 bytes used**, **48,507,446 remaining**, **67 passes**,
`pending=no`. The archive checkpoint remained **1,606 folders complete,
1,420 pending, 10,455 records saved**.

The transient worker reported a zero exit status to its root controller;
however the user could not find a `PROBE_EQUIVALENCE` line in system journal
output. A worker exit alone should not serve as retained equivalence evidence.
We therefore **do not claim that comparison is independently verified**.
Do not rerun this version merely to check journal delivery.

The revised worker now writes a tiny, private result receipt under an
ephemeral mode-0700 directory in `/run/user/<uid>/`, containing only
aggregate counts, zero checkpoint writes, and `equivalent=true`.
It does this only after identity comparison and checkpoint-hash verification.
The root controller first stops the transient worker cgroup and commits
observed traffic to the same cumulative ledger, then accepts the receipt
**only when worker exit status is zero and the receipt has the exact
expected schema, ownership, permissions and counts**. A missing, invalid,
or untrusted receipt yields an explicit failed result, even if the worker
exited zero. The file is cleaned up after the run. No backup media, cloud
secrets, folder names or Drive identifiers are written. Transient systemd
stdout/stderr are also explicitly sent to the journal for supplementary
diagnostics; journal delivery is no longer the proof channel.

The updated worker/controller passed **380 Python tests** on the workstation,
and the user reinstalled the root-owned controller through the existing
Polkit installer before the second probe. The existing one-/eight-pass
scanner definitions remained unchanged.

## Live probe #2: verified metadata equivalence (2026-11-09)

The second explicitly authorized, manual-only probe returned a root-controller
journal line from its **validated private worker receipt**:

`PROBE_EQUIVALENCE: equivalent=yes, folders=2, entries=12, files=10,
dirs=2, checkpoint_writes=0; verified by private worker receipt`

The service separately reported `PROBE_RESULT: worker completed and private
equivalence receipt validated; scanner unchanged`. The sampled IPAccounting
totals were **180,120 ingress + 50,907 egress = 231,027 bytes**. The shared
ledger has **51,723,581 bytes used**, **48,276,419 bytes remaining**,
**68 completed passes**, and **pending=no**. The checkpoint is still
**1,606 completed folders, 1,420 pending, 10,455 records**.

This is a valid end-to-end **identity and limited metadata equivalence**
result for the two sampled folders: IDs, names, types, directory ETags,
and file sizes agree with ordinary listings. It does **not** prove timestamps,
full-tree completeness, or whether the batched reader is faster or cheaper.
The 231,027-byte figure is the **combined native+reference+daemon traffic**
for the probe; there is no trustworthy per-method cost attribution. No
additional probe is automatically authorized by these results.

**Next engineering milestone:** offline integration design and regression
tests for a *separate, explicitly gated* resumable batch listing path.
Retain original scanner as fallback and preserve the existing budget,
archive lock, pending/finished ordering, metadata contract, and checkpoint
atomicity. Before granting the batch reader authority to commit folders,
require an appropriately bounded live pilot and acceptance of timestamp and
per-reader bandwidth uncertainties. Do not enable existing timers or
modify installed rclone by default.

## Offline resumable-batch integration scaffold (2026-11-09)

The verified live probe is now followed by a separate **non-executing**
scanner scaffold. The following modules are deliberately **not installed,
scheduled, or wired to the production service**, and they make no Apple
requests unless a future explicitly authorized, metered caller supplies a
network callback:

- `scripts/drive_batch_checkpoint.py` converts the tagged Go response
  into the conservative `Path`, `Name`, `Size`, `ModTime`, `IsDir`,
  and `ID` fields consumed by the original scanner. It requires a valid
  Drive child ID and RFC3339 modification timestamp, enforces directory
  ETags, size bounds, and safe nonrecursive names, and validates **every
  folder in a batch before returning any records**. The pure
  `plan_checkpoint()` creates a deep-copied, all-or-nothing tentative
  checkpoint using the same pending/finished queue order and duplicate
  directory/entry limits as the original walker. It never writes to disk
  and never marks itself authorized for production.
- `scripts/drive_batch_reader.py` holds a small in-memory converted batch
  and satisfies the original `walk_resumable(reader=...)` callback
  interface. The original walker remains responsible for atomic one-folder
  checkpoint commits. The buffered reader rereads the current checkpoint
  head before returning each folder, discards cached results when another
  backup becomes active, sanitizes callback failures, and never advances
  a checkpoint by itself. Its `verified_field_parity` gate is **False
  by default**; nothing in the installed services enables it. The original
  one-folder reader is still the fallback for a failed future batch run.
- `scripts/drive_batch_parity.py` extends the initial identity check with
  an independent, strict comparison against the ordinary rclone record
  fields, including exact modification-time strings (without silently
  truncating nanoseconds), directory IDs/ETags, file sizes, names,
  paths and types. MIME types and other unmodeled rclone fields remain
  explicitly **unverified**. This validator is pure and does not approve
  production commits.

**Offline tests added:** 39 new regression cases covering original walker
queue parity, all-or-nothing planning, invalid metadata, changing backup
state, private error sanitization, a simulation of a complete resumable
scan, exact timestamp differences, and missing fields. The expected Python
test count reached **419** (380 previously verified + 39 new), and
**all 419 passed on the workstation on November 9, 2026**. Seven further
offline tests now cover the strict live parity mode and its versioned
result receipt; they still require host verification.

**Remaining gates before real batched checkpoint writes:** (1) reproduce
the timestamp and core-field comparison against bounded live Apple/rclone
samples, (2) audit missing MIME/other fields and any differences, (3)
measure native-only vs legacy-only network traffic separately rather than
assuming the combined 231 KB probe proves a saving, (4) implement a
separate explicit root-metered batched resume worker with the same exclusive
archive lock, permanent cumulative ledger and fail-closed stop behavior,
(5) verify restart/fallback semantics and full-inventory finalization in
that gated execution path. Neither native batch results nor this scaffold
currently have permission to mutate the real checkpoint.

## Next gate: strict live core-field parity (2026-11-09)

The first real two-folder probe proved IDs, names, object types, ETags and
sizes, but did not compare modification timestamps. The new
`compare_readonly(..., require_core_fields=True)` path now uses
`drive_batch_parity.compare_core_fields` on **the same two read-only
native and ordinary reference listings**. It rejects missing or unequal
`Path`, `Name`, `Size`, `ModTime`, `IsDir` or `ID` values, even
if the prior identity check would have passed. Rclone timestamp strings
must be exactly equal; offset or precision differences fail closed.

The manual two-folder worker now requests strict validation explicitly.
It writes a **version-2** private result receipt only when the entire
comparison succeeds and both `core_fields_verified` and
`timestamps_verified` are true. The root-metered controller checks
these two proof flags and refuses an older identity-only receipt, even
when the worker exited zero. Its read-only journal output explicitly
reports both flags or a fixed sanitized error code, never private
names or identifiers. The old `rclone` binary and production scanner
remain untouched.

**Verification pending:** 7 new regression tests are committed, raising
the expected total from the last workstation-verified **419 to 426**.
No host test of these changes has occurred yet, the updated root
controller has **not** been reinstalled, and no further live Apple
request has been made or authorized. First run the offline test suite.
Only after a separately approved installation and manual metered run
should the stricter fields be considered actually validated.
Even a strict success would not establish a batch-only bandwidth saving,
full field fidelity beyond the six core fields, or permission to
write the production checkpoint.

## Strict probe #3: failed, failure code lost (2026-11-09)

The third explicitly authorized manual probe attempted strict timestamp and
core-field parity. The worker exited **status=2** after about 9 seconds.
Observed ingress was **178,534 bytes** and egress **50,284 bytes**, for
**228,818 total bytes**. The shared IP ledger was durably reconciled at
**51,952,399 bytes used, 48,047,601 bytes remaining, 69 completed passes,
pending=no**. The original Drive inventory checkpoint remains
**1,606 completed folders, 1,420 pending, 10,455 entries**.

System journal searches found only systemd lifecycle lines and no
`PROBE_FAILED` error from the transient worker. This establishes that
the worker **failed**, but does not prove whether it was a timestamp
mismatch, a missing metadata field, a backup deferral, or another issue.
The `2/INVALIDARGUMENT` systemd label is generic for exit status 2, not
a trustworthy diagnosis of the underlying failure. **Do not repeat this
live probe blindly.**

To avoid wasting further bandwidth, the worker now validates the private
receipt directory **before** any network call. On worker failure, it
writes a separate version-2, mode-0600 `result.json` containing only an
allowlisted fixed diagnostic code and `checkpoint_writes=0`. Unexpected
exceptions are reduced to `UNEXPECTED_WORKER_EXCEPTION`; no exception
details, filenames, folder IDs or raw Apple responses are written.
The root controller accepts a failure receipt only from the same trusted
ephemeral mode-0700 directory and **only after the transient service
stops and measured network bytes are committed to the cumulative ledger**.
It prints a sanitized `PROBE_FAILED: CODE` line on a nonzero worker exit.
A failure receipt cannot authorize metadata equivalence; missing/untrusted
failure evidence explicitly yields `WORKER_REASON_UNAVAILABLE`.
Uncontrolled exits and cgroup time/cutoff interruptions remain failures.
The strict parity validator also classifies **timestamp mismatches**,
**missing reference fields** and **invalid native core metadata**
separately, so the next manual report can say which kind of mismatch was
observed without logging any private values. These codes are matched by
closed allowlists in both worker and controller.

**New offline checks:** 10 regression tests were added to the previously
verified 426-test suite, bringing the expected count to **436 tests**.
The controller and worker changes are **not installed** or verified on
the workstation yet. First run the offline suite, then explicitly
reinstall the root-owned manual service. Do not start any additional live
comparison without a new decision that the diagnostic value justifies
its metered Apple traffic.

## Local acceptance sequence (no full scanner resume)

**Step A — offline tests only.** After pulling the current branch, run
`python3 -m unittest discover -s tests -q` and
`python3 scripts/drive_metered.py status`. Current expected count is
**436 tests**; confirm it from the actual workstation output. No Apple
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

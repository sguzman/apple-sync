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

## Strict probe #4: exact timestamp mismatch diagnosed (2026-11-09)

The subsequent manually authorized metered probe did report its private
failure category: `PROBE_TIMESTAMP_MISMATCH`. It read no file bodies,
advanced no checkpoint and consumed **177,763 ingress + 50,507 egress =
228,270 accounted bytes**. The exclusive cumulative ledger now reports
**70 passes, 52,180,669 bytes used, 47,819,331 bytes remaining, pending=no**;
the last verified inventory is still **1,606 completed, 1,420 pending,
10,455 records**. Do not interpret this code as proof of different
physical modification times: the strict gate compared RFC3339 strings
without normalizing offset or fractional representation. It did establish
that these serialized values were **not exactly equal**.

The recent, initially malformed classifier edit was discarded and the
previously workstation-verified parity module restored. A clean, separate
`scripts/drive_batch_timestamp.py` now classifies timestamps entirely
offline using integral UTC seconds and all **nine decimal fractional digits**.
Its fixed categories distinguish:
- Same UTC instant, different timestamp representations (time zone/zero
  padding)
- Different nanoseconds within the same UTC second
- Different whole seconds for a file vs. for a directory
- Invalid or unparseable metadata

The `drive_batch_parity` validator now uses this classifier **only to
produce a fixed failure reason**. Even a formatting-only mismatch still
fails strict validation. The worker and root controller have matching
allowlists for all new codes; private receipts remain mode-0600,
version-2, failure-only on nonzero exit, and are examined only after IP
bytes are durably accounted for. No real Apple responses, paths or
timestamps are recorded in logs or receipts. No production checkpoint
write path is enabled.

**Critical limit:** raw timestamp pairs from the previous live probes
were not saved for privacy. The new classifier cannot retroactively
tell which category caused the earlier `PROBE_TIMESTAMP_MISMATCH`.
It requires paired metadata supplied to it in a future separately
authorized, metered comparison; do not make another live request merely
to exercise the classifier.

**Offline verification:** 23 more regression tests were added since
the last **436-test workstation-confirmed result**. The expected suite
count is **459**. The new tests and code have **not been executed on the
workstation**, and the updated controller is **not installed**.
Historical note: the 459-test offline suite subsequently passed on the workstation, and its updated manual-only launcher was installed without enabling a timer.

## Strict probe #5: directory UTC second mismatch (2026-11-10)

The user explicitly authorized one more *manual-only* metered comparison,
and the installed 459-test diagnostic version reported
`PROBE_FAILED: PROBE_TIMESTAMP_DIRECTORY_VALUE_MISMATCH` in its system
journal. This specifically means the native and reference **directory**
`ModTime` values, once independently parsed into UTC, differed by one or
more **whole seconds**; it is **not** a timezone or fractional formatting
difference. This code does *not* reveal the raw timestamps, which folder
changed, the magnitude of the offset, or why it happened.

Measured IP traffic was **177,662 ingress + 49,448 egress = 227,110 bytes**.
The cumulative ledger reconciled to **72 completed passes, 52,407,779 bytes
used, 47,592,221 bytes remaining, pending=no**. The saved Drive checkpoint
remains **1,606 folders complete, 1,420 pending, 10,455 records**. The
start-of-run ledger was at pass 71, not 70; this log does **not** identify
the intervening pass. The batch experiment did **not** write checkpoints
or advance inventory.

### Pinned rclone source-level finding (v1.75.2)

Read upstream `backend/iclouddrive/iclouddrive.go` and
`backend/iclouddrive/api/drive.go` **at tag v1.75.2**. Its ordinary
`Fs.List` explicitly constructs directory records with
`fs.NewDir(remote, item.DateModified)`. Its `listAll` calls
`GetItemByDriveID(ctx, id, true)`, which is a wrapper around
`GetItemsByDriveID(ctx, []string{id}, true)`. That uses the
`/retrieveItemDetailsInFolders` API endpoint. Our experimental Go adapter
calls `GetItemsByDriveID(ctx, arg, true)` and serializes each child's
`DateModified`, so it does **not** intentionally source directory
timestamps from an entirely different Apple field or endpoint.

The remaining hypotheses include a directory timestamp changing between
the sequential Apple requests, server behavior that differs for one versus
multiple folder IDs, or another response-level inconsistency. **None is
proven.** Do not normalize away whole-second discrepancies or mark directory
parity verified; do not automatically run the live probe again. To
distinguish these cases, first design an offline replay experiment and
a separately authorized, bounded comparison of a repeated single-folder
read versus batched reads, ensuring all traffic is metered and results
remain private. Until then retain the original one-folder scanner and
the batch write gate closed.

## Offline bracketing replay after directory-second mismatch (2026-11-10)

Implemented `scripts/drive_batch_replay.py` as a **pure in-memory**
diagnostic that consumes already-supplied, synthetic or separately
authorized native API replies. It cannot open sockets, execute subprocesses,
download anything, read accounts, or save progress. The
`tests/test_drive_batch_replay.py` suite adds **16** regression tests
to the last workstation-verified **459**, so the **expected new total is
475 tests, not yet workstation-verified**.

### How to interpret an offline replay

For two validated folder IDs, the helper examines **one target folder**
from this chronological three-observation sequence:

1. **S0**: native single-folder response for the target folder
2. **B**: native two-folder response containing that same target folder
3. **S1**: native single-folder response for the same target folder

It rejects malformed envelopes; changes to child IDs, directory ETags,
non-timestamp core fields, or unsafe/incomplete timestamp values. It compares
the **same direct child directory IDs** across all three responses.

- **S0 = B = S1:** sampled directory time strings remained identical
- **S0 = S1 but B differs:** batch-only difference **observed**, which may
  also reflect a transient change followed by a reversion
- **S0 differs from S1:** single readings differ **observed**, which may
  reflect metadata updates or inconsistent API replies
- **Different directories exhibit different patterns:** mixed observations;
  do not infer a single cause

These are **observational categories, not root-cause proofs**. Anonymous
counts and fixed codes are the only outputs. Raw timestamps, private paths
and Drive IDs are **not persisted**. No replay outcome approves metadata
equivalence or checkpoint writes.

### Minimum *future* metered experiment — NOT authorized or implemented

To test whether a batch-versus-single difference reproduces on the account,
a separate, explicitly authorized manual mode could perform the chronological
**S0 / B / S1** comparison above, with an additional ordinary rclone RC
listing of the same folder if necessary to connect the result to the
original rclone mismatch. Even these observations would not fully rule out
a transient update between requests.

The experiment would need its **own offline-tested worker and private
failure/success receipt contract**, run under the **existing root-owned
cumulative IP ledger** and one stopped/verified systemd cgroup, retain
backup-idle checks and the existing exclusive archive lock, respect the
sampled traffic cutoff as a **soft** cutoff (never a hard quota), hash the
checkpoint before and after, and publish only aggregate category counts
after traffic is durably committed. Abort if the target has no directory
children or if any metadata/identity check fails. **Do not add it to the
current launcher or invoke Apple merely to exercise the offline helper.**

Current state remains **72 measured passes, 47,592,221 bytes remaining,
pending=no; 1,606 folders completed and 1,420 pending**. No new Apple
traffic or production scanner change was made in this step.

## Offline replay sequencing gate (2026-11-10)

The user's workstation fast-forwarded to `dd9d7c0` and **all 475 offline
tests passed** in 4.050 seconds. This confirms the earlier *pure comparison*
helper works on the user's system. It does not prove a live replay.

Added a **separate, uninstalled and unexecutable-on-its-own**
`scripts/drive_batch_replay_protocol.py`. It accepts a deep copy of the
existing checkpoint, selects the **first two distinct pending folder IDs**,
and calls an injected `read_native` callback in this exact order:
`[A]` → `[A,B]` → `[A]`. Each of the three replies is independently
validated and converted **before** a following request is permitted.
Each read is surrounded by fail-closed backup-idleness checks. A busy
backup, unknown status, malformed response, missing second folder, or
private callback exception prevents further reads; errors are fixed
category strings. The comparison delegates to `drive_batch_replay.py`
and returns anonymous observational counts with
`ip_accounting_verified=False`, `measured_ip_bytes=None`,
`checkpoint_writes=0`, `checkpoints_advanced=0`,
`production_commit_authorized=False`.

The new `tests/test_drive_batch_replay_protocol.py` contains **18**
offline regression tests, including early abort after an invalid first
or second response, request ordering, backup activation at each phase,
checkpoint immutability, callback failure sanitization, and metadata
identity checks. The expected next suite is **493 tests** (475 previously
passed +18); the new version is **not workstation-tested yet**.

**Important boundary:** This is just an offline, callback-driven
sequencer. It is not installed as a root service, not exposed through
the existing Polkit launcher, not counted as a live bandwidth check, and
does not authorize another Apple probe. Before any real traffic,
implement and offline-test a dedicated worker and root-owned controller,
with its own versioned private result contract, total wall-clock and
per-request timeouts, existing exclusive cumulative IP ledger and backup
locks, checkpoint SHA-256 verification and no-write guarantees.
A separately approved manual launch would still be necessary.

## Dormant replay worker and private receipt contract (2026-11-10)

The workstation confirmed **493 offline tests passed** in 4.053 seconds
after fast-forwarding to `bdef154`. The **new** replay worker added here
has **not been run or installed on the workstation**.

Added `scripts/drive_batch_replay_worker.py`, deliberately with **no
executable Apple-contacting entrypoint** and not referenced from systemd,
Polkit or the existing manual launcher. Direct script execution prints
`REPLAY_WORKER_DORMANT` and exits without contacting Apple. The private
`replay_once()` routine is staged for a future separately implemented
root-owned cumulative-IP-metered service. It reuses the pinned experimental
rclone binary identity check, the existing exclusive archive lock and
backup-idle guard, loads the saved checkpoint under that lock, and verifies
the checkpoint's SHA-256 before and after the three-read replay. The
`RcloneRCReader` session remains private and the callback applies a
**15-second** per-request cap. On interruption or unverifiable metadata,
it returns only allowlisted error codes; it never saves a scanner checkpoint.

A **distinct** version-1 private `replay.json` receipt contract uses
exclusive mode-0600 files in a trusted 0700 directory and distinguishes
`kind=observation` from `kind=failure`. An observation must have
exactly **three completed requests**, two selected folders, valid bounded
aggregate counts matching the claimed observational category, and
`checkpoint_writes=0`, `checkpoints_advanced=0`, and
`production_commit_authorized=false`. It does **not** contain raw
timestamps, Drive IDs, filenames, an `equivalent` attestation or a
`timestamps_verified` claim. The existing **version-2 equivalence
receipt** is not accepted as a replay receipt and vice versa. The
controller must validate the new receipt **only after the stopped worker's
IP bytes have been durably accounted for**; this is still future work.

Added **18** offline worker tests including lock contention, changed
checkpoint hashes, binary identity refusal, busy backup deferral,
sanitized protocol errors, untrusted result directories, exclusive
private receipts, data-free failure codes and outcome/count consistency.
The next expected full suite is **511 tests** (493 last verified +18),
**not yet workstation-verified**. No new live probe is authorized, no
manual service was installed, and no Apple network requests were made in
this step.

### Remaining live integration blockers

Before any future replay is offered for user authorization, implement and
offline-test an **independent root-metered controller**. It must enforce
the original shared cumulative budget; reserve before starting work,
measure and stop the whole transient systemd worker cgroup, reconcile
ingress/egress before trusting a receipt, guarantee a maximum wall clock
and timeouts, refuse enabled timers/active backups, and install its own
strictly manual-only service and start-only Polkit permission. Do not
reuse the strict probe's `equivalent` receipt or modify the original
scanner's write path. The new worker's dormant routine alone does not
establish or enforce IPAccounting.

## Dormant root receipt validation stage (2026-11-10)

The workstation now confirmed **511 offline tests passed** in 4.584
seconds after updating to commit `9e69f4a`. This confirms the dormant
worker and replay observation receipt on the user's system, not any
actual live Apple batch replay.

Added a **separate, dormant, root-side**
`scripts/metered_drive_batch_replay.py` whose current scope is
**receipt validation only**, with no systemd, Apple, ledger or CLI
execution path. It reads a separately created user-owned private
receipt channel via directory file descriptors and `O_NOFOLLOW`,
requires exact mode 0700 for the channel and 0600 for the single-link
regular receipt, limits it to 1024 bytes, and rejects duplicate JSON
keys. It exclusively accepts **version-1 observation** data whose
counts add up and agree with one of the four observational outcome
categories. An old **version-2 equivalence** receipt is always
rejected. Distinct failure receipts accept only closed, worker-matching
codes. Any private cloud field or claim of production equivalence
invalidates the receipt. All failures surface fixed codes.

The **18 new** regression tests in
`tests/test_metered_drive_batch_replay.py` exercise worker-to-root
receipt round trips, every observational category, reject/malformed
paths and types, symlink/hardlink checks, privacy, and allowlist
agreement. **529 tests are expected** (511 last verified +18),
**not yet run on the workstation**.

**Important:** This stage does not implement the critical
cumulative-budget reservation or `systemd-run` controller itself;
the new root-side module is deliberately dormant. No service
installation, traffic, Apple account access, checkpoint writes or
new live probe are authorized. The remaining controller work must
enforce stop-the-whole-cgroup and measured ledger reconciliation
**before** calling `read_observation()` or `read_failure()`.
A separate permissioned manual launcher and explicit approval are
still required before any live experiment.

## Dormant root replay accounting controller (2026-11-10)

The user's workstation fast-forwarded to `646b9d5` and **all 529
offline tests passed** in 3.162 seconds. That covers the earlier
root-side receipt reader, not this newly added controller core.

Added `scripts/drive_batch_replay_accounting.py`: an **injected-only,
dormant state machine**, with **no CLI, systemd launcher, Apple request,
timer, Polkit rule or installed service**. It is not wired to the
actual `replay_once` worker, so it cannot initiate a live replay on
its own.

A future separately approved root wrapper would have to supply all
the following already-security-reviewed operations: root/setup/timer
guard, backup-idle check, zero-traffic IPAccounting preflight, worker
launch, bounded systemd property reads, stop-confirmed cgroup shutdown,
monotonic clock and bounded sleep, and the strict root-side receipt
readers. The shared `BudgetLedger` must be held under its existing
exclusive lock for the whole operation.

The staged controller checks the exact replay unit name, rejects
unknown/busy idle state, runs the preflight before any reservation,
calculates the existing **1,500,000-byte sampled cutoff with a
2,000,000-byte reserve**, then calls `ledger.begin(unit)` **before**
the launch callback. After any launch attempt it **unconditionally
stops** the cgroup, verifies `ActiveState=inactive`,
`SubState=dead` and final `IPAccounting=yes` ingress/egress
counters, then durably calls `ledger.complete` **before reading
either the observation or failure receipt**. On a stop, final-counter,
or ledger-commit failure, it leaves the durable budget reservation
**pending**. Interrupted/time-limited/soft-cutoff runs cannot be
called successful, even if the worker happens to have emitted a
success-like receipt. Its outputs contain fixed statuses, aggregate
measured bytes, and explicitly `production_commit_authorized=false`.
The soft cutoff is **not a hard bandwidth quota**.

A new pure `validate_observation_data()` helper in the root
receipt module is reused by the controller on **every** candidate
observation. It rejects extra `equivalent` or private fields,
invalid counts and contradictory outcome categories even if a
future callback was accidentally wired to a weaker reader. The
existing version-1 replay receipt and old version-2 strict parity
receipt are not interchangeable.

**22 additional offline tests** in
`tests/test_drive_batch_replay_accounting.py` cover reservation
order, confirmation that stop precedes accounting and receipt reads,
nonzero exits, malformed/missing receipts, unknown backup states,
no-idle defer, guard/preflight refusals, bad units, start failures,
systemd stop failure, inactive-state and counter verification,
interrupted polling, soft cutoff, time expiry, ledger commit failure,
strict payload validation and the absence of a live CLI. The
expected new suite size is **551 tests** (529 previously verified
+22), **not yet run on the user's workstation**.

**Still required before actual Apple traffic:** A dedicated trusted
root wrapper binding those injected operations to systemd and the
existing ledger, a dedicated versioned private worker exit/receipt
entrypoint, unit/Polkit installation with manual-only policy, a
separate complete offline integration QA pass, and explicit approval
for each live probe. No Apple traffic or inventory writes occurred
in this development step, and the original inventory scanner is
unchanged. The last known ledger is **72 passes, 47,592,221 measured
bytes remaining, pending=no**; the saved snapshot remains
**1,606 completed, 1,420 pending, 10,455 entries**.

## Dormant systemd replay command planner (2026-11-10)

The user's workstation fast-forwarded to `1191e72` and **all 551
offline tests passed** in 4.119 seconds. This verifies the preceding
injected-only metering state machine, **not** a live service.

Added the pure `scripts/drive_batch_replay_systemd_plan.py`.
It can only **construct argv tuples** for a future transient
systemd worker, its fixed `systemctl show` and `systemctl stop`
commands, and a separately named, zero-traffic `/usr/bin/sleep 4`
IPAccounting preflight with its own query/stop commands. No subprocess,
Apple network, root installation, ledger mutation or CLI is executed
or available by importing or invoking this module.

The plan pins the existing repository worker path, the unprivileged
`sguzman` service account, `Type=exec`, `IPAccounting=yes`,
`KillMode=control-group`, `Restart=no`, `NoNewPrivileges=yes`,
`RuntimeMaxSec=95s`, and the exact replay-unit name pattern.
It requires a candidate receipt directory of the form
`/run/user/<uid>/.apple-sync-replay-<token>` and uses distinct
`APPLE_SYNC_REPLAY_*` environment variables rather than the
existing strict batch probe's result channel. The plan rejects
unexpected names, invalid IDs and other directory roots.
**Path-shape validation alone does not establish owner, 0700 permissions
or absence of symlinks**: the eventual trusted root wrapper must
check those properties before launching anything. The worker
it would name **still has no live `--execute` handler** and simply
prints `REPLAY_WORKER_DORMANT`.

The **22 new tests** in
`tests/test_drive_batch_replay_systemd_plan.py` exercise fixed
argv properties, strict unit and runtime-directory arguments,
preflight start/status/stop, no timers or shells, exact unprivileged
user, and the inert worker. The next expected full suite is
**573 tests** (551 last verified +22); these new tests have
**not yet been workstation-executed**.

**Remaining blockers:** A separate reviewed and tested worker
entrypoint that accepts only the trusted manually started replay
cgroup, a root-owned adapter that actually executes the fixed
systemd plan and binds it to `run_accounted` under the existing
exclusive `BudgetLedger`, rigorous stop/ledger/receipt integration
tests, manual-only privileged installation and start permission,
and new explicit authorization for any Apple traffic.
No replay launched; no service, timer, checkpoint or 100 MB ledger
was changed during this code-only step.

## Uninstalled systemd replay IO adapter (2026-11-10)

The user pulled `c9e2a7d` and verified **573 offline tests
passed** in 4.122 seconds. This covers the prior pure systemd command
planner; it does **not** authorize a live replay.

Added `scripts/drive_batch_replay_systemd_adapter.py`: a
**strictly injected-only** adapter using the existing fixed argv
planner. It supplies `start_worker()`, `properties()`,
`stop_worker()`, and `preflight()` methods suitable for **future**
wiring into the offline-tested `run_accounted` state machine.
It has no subprocess import, no default executor, no CLI, no root
service, and no installation step. Tests provide the executor as
an in-memory fake. The worker named by its arguments still prints
`REPLAY_WORKER_DORMANT` and cannot run the Apple replay.

Before a worker start, the adapter validates that `/run` and
`/run/user` are root-owned, non-symlink directories without
group/world write access; then requires the exact user-owned
`/run/user/<uid>` and private replay result channel to be
non-symlink directories of mode 0700. It rejects unexpected
systemd property lines, missing/duplicate values, or unauthorized
command arguments, and sanitizes executor exceptions without
printing stderr or private data. It executes no real commands.

The isolated zero-traffic accounting preflight, when exercised
by an injected fake executor, has mandatory stop and **post-stop
counter verification**, even after a start/query failure. Only a
sample observed as `active/running` and with `IPAccounting=yes`
and zero ingress/egress, followed by an `inactive/dead` zero-byte
final observation, passes. The adapter intentionally does
**not** call `reset-failed` before the accounting controller
has inspected the final worker counters.

The **22 new offline tests** in
`tests/test_drive_batch_replay_systemd_adapter.py` cover
argument binding, private channel trust, symlink rejection,
strict systemd output, sanitized errors, no shell, zero-traffic
preflight, failure cleanup and hard refusal to accept residual
traffic or uncertain stop state. **595 tests are expected**
(573 previously verified +22), **not yet workstation-verified**.

**Not yet implemented or authorized:** a trusted root entrypoint
binding this adapter to the `BudgetLedger`, existing backup/timer
guard and observation-only receipt reader; runtime directory
creation and per-run lifecycle; a guarded worker entrypoint; and
manual-only service and Polkit installation. A future full
offline integration QA must verify these boundaries before
any live Apple request is even proposed. Existing scanner,
services, inventory checkpoint, and measured budget remain
unchanged; no user approval for another live probe was given.

## Uninstalled end-to-end root replay wiring (2026-11-10)

The user's uniquely labeled **QA RUN #595** fast-forwarded to
`1b1d365` and verified **595/595 tests passed** in **4.627 seconds**.
This includes the previous systemd adapter, not the new integration.

Added `scripts/drive_batch_replay_root_wiring.py`, a **nonexecutable,
injected-only** composition layer. It binds the existing systemd
adapter, `drive_batch_replay_accounting.run_accounted`, and the
**actual strict version-1 observation/failure receipt readers**.
It opens the supplied ledger via a context manager and holds the
exclusive lock through reservation, worker execution, stop, final
IP measurement, durable reconciliation, and the receipt read.
The existing core requires successful preflight before
`ledger.begin`, stop-confirmed `inactive/dead` state and verified
IP counters before `ledger.complete`; it leaves the pending
reservation blocked if stop or reconciliation cannot be trusted.
All outcomes still forbid checkpoint writes or production batch
adoption.

The integration **does not provide** a real command executor, real
`BudgetLedger` factory, root/timer/backup guard, service entrypoint,
timer, or installation. It accepts these as **explicit test-only
dependencies**, so the caller could supply unsafe operations;
it must **not** be treated as a production-ready root controller.
The target worker still has only the dormant CLI refusal.
No live Apple requests, unit changes, privilege installation or
archive writes occurred in this step.

The added **21** mock-driven regression tests in
`tests/test_drive_batch_replay_root_wiring.py` check receipt
read ordering, the ledger lock held through receipt validation,
fixed systemd commands, channel binding, failures of preflight,
backup guard, worker launch and stop, invalid final IP accounting,
untrusted receipt schemas, failed ledger commits and missing
injected dependencies. They do **not** execute subprocesses or
contact Apple. The next expected suite is **616 tests**
(595 last verified +21), **not yet tested on the workstation**.

### Outstanding authorization / security gates

- A reviewed root-owned controller entrypoint with an immutable
  trusted executor, strict root-only invocation and the existing
  guard/setup; no arbitrary callbacks from unprivileged callers
- Secure creation and cleanup of the 0700 user-owned volatile
  receipt channel; handle race/symlink/ownership concerns
- A separately reviewed worker `--execute` implementation
  requiring the exact future transient metered unit and receipt
  channel; no direct user-run Apple request path
- Complete offline failure-path integration tests and **explicit
  user approval** to install a manual-only service or run a bounded
  Apple replay

The soft 1.5 MB sampled cutoff remains **advisory** and may overshoot.
The ordinary scanner and its 1,420 pending folders are unchanged.

## Dormant fd-relative replay receipt-channel lease (2026-11-10)

The workstation confirmed **APPLE-SYNC QA RUN #616** at commit
`52e2819`: **616/616 tests passed** in **4.645 seconds** with exit 0.
This covered the preceding injected-only root wiring stage.

Added `scripts/drive_batch_replay_channel.py`: a separate
**nonexecutable, root-only** allocator for a private replay receipt
directory. The root entrypoint (still **unimplemented**) would use
`allocate_root_channel()`, which opens the fixed
`/run/user/<uid>` path component-by-component using directory
file descriptors, `O_NOFOLLOW`, and ownership/permission checks.
It requires root-owned, non-group-writable ancestors and an exact
user-owned 0700 runtime directory. Creation is exclusive with a
random 24-byte URL-safe token, `mkdir(dir_fd=...)`, and
`fchown`/`fchmod` for a private user-owned 0700 channel.
All fd-relative creation operations are available separately to
offline tests through the internal `_create_at_fd` function,
which does not call Apple or systemd.

The returned `ReplayChannelLease` retains the parent and child
directory descriptors and the original `st_dev/st_ino` values.
`close()` only closes descriptors; it **never silently deletes**
the channel. `cleanup_after_stop(stopped_verified=True)` is an
explicit separate operation, intended only **after the trusted root
wrapper has confirmed the worker cgroup has stopped**. A false,
unknown or truthy-nonboolean confirmation fails closed. Cleanup
refuses changed inode/symlink/directory-mode references, unexpected
children, or an untrusted receipt; it removes at most one regular
0600 single-link `replay.json` and its own empty directory.
An interrupted/unconfirmed worker intentionally leaves the
channel intact for later operator reconciliation.

Added **22** offline tests
(`tests/test_drive_batch_replay_channel.py`) for unique names,
ownership, modes, verified-stop cleanup, unconfirmed preservation,
unexpected/symlinked/hardlinked/over-permissive receipts,
replaced directories, descriptor close behavior, bad IDs, untrusted
runtime paths, and inert root allocation. Tests use disposable
temporary runtime directories as an **unprivileged** account; they
never touch the machine's actual `/run/user` tree. The next
expected full suite is **638 tests** (616 last verified +22),
**not yet workstation-verified**.

**Still not approved or implemented:** Wiring the channel lease
into a real trusted root controller, a guarded live worker entrypoint,
explicit manual-only unit/policy installation and any Apple replay.
The runtime path is user-writable by design, so path checks alone
must not be treated as proof that a live controller is race-free;
additional final preflight and lifecycle security review is
necessary. No Apple traffic, user service changes, checkpoint writes
or cumulative ledger mutations were performed by this stage.

## Dormant private-channel lifecycle integration (2026-11-10)

The user's labeled **APPLE-SYNC QA RUN #638** fast-forwarded to
`aac6a7b` and verified **638/638 offline tests passed** in
**4.631 seconds** (exit 0). This includes the standalone fd-relative
channel allocator, not the new integration.

Added `scripts/drive_batch_replay_lifecycle.py`, an **uninstalled,
root-only, injected-only** wrapper around the existing
`ReplayChannelLease` and `drive_batch_replay_root_wiring.run_wired`.
It allocates the private channel, retains its descriptors while
the root metering logic would hold the cumulative budget lock, and
permits explicit cleanup **only** when the validated wiring returns
a known completed state (worker stop + counters + ledger settlement)
or the exact no-traffic/no-worker-launch defer state. Even normal
error outcomes require fixed schema fields that forbid checkpoint
writes or claims of production authorization. It never accepts an
unknown, contradictory, extra-field or malformed result as a safe
cleanup condition.

If wiring raises (e.g. shutdown unconfirmed, ledger reconciliation
failed), the lifecycle **preserves the entire receipt directory** and
closes file descriptors only. This intentionally avoids erasing
evidence or resetting an unresolved cumulative bandwidth ledger.
Directory replacement, unexpected children or unsafe receipt metadata
also block cleanup. The wrapper has no subprocess import or
executable entrypoint and cannot start Apple traffic without a
separate trusted root controller supplying immutable callbacks.
The existing replay worker's live CLI remains dormant.

Added **22** offline regression tests in
`tests/test_drive_batch_replay_lifecycle.py`, exercising successful
lease cleanup, settled worker failures, no-traffic deferral,
failed stop/ledger reconciliation, preservation of private evidence,
channel replacement and untrusted contents, strict outcome
allowlists, missing dependencies and root-only invocation.
The new expected full suite is **660 tests**
(638 last verified +22), **not yet workstation-verified**.

**Remaining:** A reviewed *non-injected* root-owned, manual-only
entrypoint binding trusted service operations and the existing
ledger; the exact disabled-by-default guarded replay worker
entrypoint; completion of all offline failure-path tests; and
explicit approval before installing or making any Apple requests.
No Apple traffic, timers, inventory checkpoint updates or budget
mutations were made by this development step. The 100 MB cumulative
budget is **measured**, not a kernel-enforced hard cap.

## Exact cgroup identity gate for future replay worker (2026-11-10)

The user's **APPLE-SYNC QA RUN #660** at commit `252695e`
verified **660/660 offline tests** in **4.634 seconds** with exit 0.
That verifies the channel-lifecycle wrapper, not this new gate.

Added the **pure, dormant** `scripts/drive_batch_replay_worker_gate.py`.
Its `check_launch` function accepts only a nonroot worker whose real
and effective user IDs match, a well-formed systemd `INVOCATION_ID`,
the replay metering marker, an exact
`apple-sync-drive-metered-replay-<pid>.service` unit name, and a
matching single unified cgroup v2 membership under
`/system.slice/` with no nested groups or extra lines. It also
checks that the receipt path has the exact
`/run/user/<uid>/.apple-sync-replay-<token>` shape. The pure
systemd argv planner now includes
`APPLE_SYNC_REPLAY_UNIT=<unit>` so the future worker can require
an exact match to its observed cgroup. **A normal user environment
variable alone is insufficient**, and the gate never treats it as
proof of trusted IPAccounting or available cumulative bandwidth.

This is an **identity prerequisite only**: it does not read the live
`/proc/self/cgroup`, access Apple, open an RC socket, inspect
permissions, modify a checkpoint, create a service or authorize a
network request. The replay worker's actual command-line entrypoint
is **still dormant**. A future worker wrapper must read its
*actual* proc cgroup, validate the private channel ownership and
existing saved checkpoint, and execute only inside a separately
reviewed and approved root-accounted unit. Root must still prove
the ledger reservation, live IPAccounting, complete cgroup shutdown
and durable metering **before** any observation receipt is accepted.

Added **25 offline tests** in
`tests/test_drive_batch_replay_worker_gate.py` covering cgroup
spoofing, unit mismatch, root/wrong-user refusal, strict private
channel shape, user.slice and legacy cgroup rejection, malformed
invocation markers, no IO and no auto-enabled execution path.
Expected total: **685 tests** (660 verified +25), not yet
workstation-tested. No Apple traffic, timer, root installation,
inventory checkpoint or bandwidth ledger change took place.

## Actual-process replay worker preflight (2026-11-10)

The user's labeled **QA RUN #685-FIX1** fast-forwarded to
`19b64e3` and verified **685/685 offline tests passed** in
**4.620 seconds** (exit 0). The prior failure was a missing-variable
error category and is fixed. No Apple requests were initiated.

Added `scripts/drive_batch_replay_worker_preflight.py`, a **dormant
read-only process validation helper** for the future replay worker.
Unlike the pure cgroup gate, this layer reads its own fixed
`/proc/self/cgroup` with `O_NOFOLLOW` and a 4096-byte bound. It
verifies real/effective unprivileged UIDs, invokes the strict replay
unit and `INVOCATION_ID` identity gate using the actual current
process membership, then validates ownership, mode and non-symlink
status of `/run`, `/run/user`, the user's runtime directory and
the private receipt channel. Every refusal uses a fixed category
without printing private paths or raw cgroup data.

**This is not a live worker entrypoint.** It does not call
`replay_once`, modify a checkpoint, launch `rclone` or systemd,
touch the bandwidth ledger, or authorize Apple traffic. In
particular, a valid process identity and directory ownership are
**necessary but insufficient** to prove the root controller's
durable reservation, actual IPAccounting, stopped cgroup or
cumulative 100 MB metering. The existing replay worker remains
dormant and the new helper is not wired into a live CLI.

Added **20 offline regression tests** in
`tests/test_drive_batch_replay_worker_preflight.py` for exact
`/proc/self/cgroup` acquisition and bounded error paths, UID
checks, forged markers, unknown cgroups, wrong receipt locations,
runtime component trust and no network/CLI behavior. The next
expected suite is **705 tests** (685 verified +20), **not yet
workstation-verified**.

The remaining work before considering one separately authorized
Apple replay is a reviewed root-owned manual controller with
immutable executable bindings, guarded worker entrypoint, final
offline integration QA, and explicit user approval for any
service installation or live account access. No new live
probe was performed.

## Dormant guarded replay worker body (2026-11-10)

The user's labeled **QA RUN #705** fast-forwarded to `9b6b861`
and confirmed **705/705 offline tests passed** in **4.649 seconds**
with exit 0. This verified the actual-process identity preflight.

Added `scripts/drive_batch_replay_worker_entry.py`, a new
**non-CLI, detached** worker-side body named `guarded_once()`.
It first calls the actual-process preflight, which reads its
`/proc/self/cgroup`, checks the exact transient replay unit and
nonroot UID, and verifies the private result channel.
A failure there returns a sanitized nonzero status without
calling the Apple transport or writing an untrusted receipt.
Only after that prerequisite passes does the body call the
existing read-only `replay_once()` exactly once.

A successful, validated bracketed observation is written via
`publish_observation` to the private 0600 channel. Failure
paths use `publish_failure` with a strict, fixed code from the
existing allowlist; unexpected exceptions are converted to
`REPLAY_LOCAL_FAILURE` without printing account information.
If the failure receipt cannot be written, the worker still
returns nonzero, leaving the controller to reject a missing or
invalid result. Even an exit-zero success merely means an
**observational receipt was published**; the separate root
controller must still stop the entire worker cgroup, durably
commit measured IP traffic, and validate the private receipt.
No observation authorizes checkpoint writes or metadata
equivalence.

The original `scripts/drive_batch_replay_worker.py`
**still prints `REPLAY_WORKER_DORMANT` for every CLI invocation**,
including `--execute`. The new guarded body is not referenced
by that CLI or any installed unit, nor is it an Apple traffic
permission. Actual worker activation remains blocked pending the
root-owned immutable entrypoint, installation review, full
offline safety QA and explicit user approval.

Added **20 mock-only tests** in
`tests/test_drive_batch_replay_worker_entry.py` verifying
preflight-first behavior, zero calls on failed preflight,
strict outcome categories, sanitized success/failure output,
receipt-write failures, missing channels, and the continued
inert CLI. The expected full suite is **725 tests**
(705 last verified +20), **not yet workstation-tested**.
No Apple traffic or service, checkpoint, timer or bandwidth
ledger changes occurred.

## Root-owned replay code trust staging (2026-11-10)

The user's labeled **APPLE-SYNC QA RUN #725** fast-forwarded to
`0f44d9e` and verified **725/725 tests passed** in **4.151 seconds**
(exit 0). This verified the detached guarded worker body, not this
new privileged-code verification stage.

Added `scripts/drive_batch_replay_root_trust.py`, a **read-only,
uninstalled** verification module intended for the future root
controller bootstrap. The existing, already approved installer
uses `/usr/local/libexec/apple-sync-drive-metered` to house
root-owned controller code, separately from the normal user's
writable Git checkout. A privileged replay controller **must not
import replay modules directly from that checkout**.

The new verifier pins an exact list of **10 root-side Python
modules** needed for the budget ledger, accounting logic, systemd
adapter, private-channel lease, strict receipt validation and
lifecycle. It opens each component of the fixed privileged
directory with fd-relative `O_DIRECTORY|O_NOFOLLOW` checks and
requires root ownership with no group/world write access.
It opens each required module by name with `O_NOFOLLOW`, and
requires a nonempty, regular, single-link, root-owned file
without group/world write access. Missing files, symlinks,
changed ownership and unsafe permissions fail closed.

`verify_root_installation()` is **root-only** and currently has
no CLI, installer hookup or executable replay path. A future
tiny root-owned bootstrap must start Python in isolated mode
(`/usr/bin/python3 -I`), perform the minimal trusted bootstrap
path check **before** importing this module, then verify the
root-owned code tree prior to adding it to Python's import path.
The existing installer was **not modified** to copy these modules
or create a replay service; it still installs only its previously
approved metered inventory and comparison units.

Added **25 offline tests** in
`tests/test_drive_batch_replay_root_trust.py` using disposable
unprivileged temporary module directories to check the same
file-descriptor verification, hardlink/symlink and permission
failures, bad filenames and read-only behavior. The expected
suite is **750 tests** (725 last verified +25), **not yet
workstation-verified**.

**Remaining security gates:** actual isolated root bootstrap,
trusted immutable executor and manual-only systemd service
binding, secure worker code loading, full offline end-to-end
review, and the user's separate explicit approval for any
installation or live Apple access. Root-side integrity
verification alone is neither a network quota nor permission
to execute the replay. No live Apple call, inventory checkpoint,
timer, service or budget was touched during this code-only stage.

## Diagnostic-only isolated root bootstrap (2026-11-10)

The user's labeled **APPLE-SYNC QA RUN #750** fast-forwarded to
`853399b` and **750/750 offline tests passed**, in **3.647 seconds**,
exit 0. This verifies the root-code trust helper, **not** the new
bootstrap.

Added `scripts/drive_batch_replay_root_bootstrap.py` as a
**diagnostic-only, not-installed** privileged-code bootstrap.
The sole supported action is exactly `doctor`; an argument such as
`run`, `start`, `install` or `--execute` is explicitly refused.
There is **no worker invocation, real command executor, ledger
operation, Apple request, or service installation** in this module.

A future separately approved root-owned static service may start
`/usr/bin/python3 -I -S -B` against the fixed root-owned
`/usr/local/libexec/apple-sync-drive-metered/drive_batch_replay_root_bootstrap.py`.
The bootstrap checks root EUID, exact interpreter/script path, and
isolated/no-site/no-bytecode Python flags. It then walks the installed
root-owned path via `O_NOFOLLOW` directory fds and checks that both
the bootstrap itself and its verifier are regular, single-link,
root-owned, non-group/world-writable files, **before** importing the
verifier from that fixed location. The verifier subsequently checks
the entire root-side module manifest. That manifest now includes
the bootstrap and has **11 required Python modules**.

The existing `install_metered_launcher.py`, existing service files,
Polkit permissions, and all installed services were deliberately left
unchanged. The bootstrap is not installed; no privileged action
can be initiated from this development step. It is a **trust and
diagnostics prerequisite only**, never permission for live Apple
traffic. The future replay worker and root controller execution
path are still disabled.

Added **27** new offline-only tests in
`tests/test_drive_batch_replay_root_bootstrap.py`, exercising strict
`doctor`-only policy, Python isolation, root UID and fixed locations,
missing or unsafe seed code, symlinks, permissions and hardlinks,
ordered verification, sanitized failures and no Apple or systemd
activation. Updated the existing root-trust test to require the
11-file manifest. The next **expected** full suite is **777 tests**
(750 last verified +27), **not yet workstation-verified**.

**Remaining before any manual replay:** a full offline safety review
of the root-controlled executable bindings, worker code path
immutability, controlled installation and complete failure recovery;
and explicit user approval before any installation or Apple network
request. The cumulative IP budget remains an advisory, measured
limit rather than a kernel-enforced hard cap.

## Dormant replay worker uses only proposed root-owned code (2026-11-10)

The user's **APPLE-SYNC QA RUN #777** at commit `6d5db43` verified
**777/777 offline tests** in **4.154 seconds** (exit 0). This confirms
the prior isolated `doctor` bootstrap, **not** this new worker path
hardening.

Security review found that the **inert systemd argv planner still
named the user-writable Git checkout as the worker executable path
and working directory**. Even though that worker CLI stayed dormant,
reusing those argv during future activation would have allowed
user-edited Python code into a system-accounted execution path.
The planner now pins the worker script and working directory to
`/usr/local/libexec/apple-sync-drive-metered`, the same protected
location intended for trusted root controller code.

The planned unprivileged Python worker is invoked with `-E -s -B`:
ignore `PYTHON*` environment options, disable user-site packages,
and avoid writing bytecode into the root-owned code directory.
The worker remains unprivileged (`User=sguzman`) in its
transient `IPAccounting=yes` system service. It still points at the
original **dormant** `drive_batch_replay_worker.py --execute` CLI,
which does **not** perform a replay.

The root code verifier now has two separate allowlists: **11
root-controller modules** and **21 worker/dependency modules**
covering the replay worker, the guarded body, cgroup preflight,
native transport, checkpoint-read and archive-lock dependencies.
It checks **both** sets for regular, non-symlinked, single-link,
root-owned, non-group/world-writable files before approving
a future read-only diagnostic. Both sets are required to pass.
This does not copy any module, authorize the `--execute` handler,
or make a command executor available. Existing installer, Polkit,
units and timers remain unchanged.

Added **18 new offline tests** in
`tests/test_drive_batch_replay_protected_worker.py`, including
closed local import dependencies, fixed worker arguments,
no user checkout or package injection, dormant worker CLI,
symlinks/hardlinks, file-mode and manifest failures, and
non-installation by the existing installer. Updated existing
systemd planner assertions without changing their test count.
The **next expected suite is 795 tests** (777 last verified +18);
this new stage has **not** been workstation-verified.

**Next gates:** complete manual-only root-owned installation
packaging, immutable executor/root controller binding, guarded
worker entrypoint, full failure-path QA, and explicit separate
approval before any Apple traffic or service installation.
The measured 100 MB ledger remains a sampled advisory budget;
a hard network byte cap has not been implemented.

## Exact root command allowlist staged, no executor (2026-11-10)

The user verified **APPLE-SYNC QA RUN #795** at commit
`eac94c6`: **795/795 offline tests passed**, **4.212 seconds**,
exit 0. That verifies the protected worker path and root-owned
module manifests, **not** this new command policy.

Added `scripts/drive_batch_replay_command_policy.py`, an
**uninstalled, pure input checker** intended for a future manual
root-owned controller. It accepts exactly six fixed argv/timeout
combinations for **one** validated uid, pid and private receipt
directory. All command shapes are generated by the existing
`drive_batch_replay_systemd_plan.py`: zero-traffic preflight
start/status/stop and worker start/status/stop. The preflight
and worker start/stop commands use exactly 15-second command
timeouts; the status queries use exactly 6 seconds.

The policy rejects arbitrary commands, new services or units,
`systemctl enable`/`restart`/`daemon-reload`, shell strings,
extra tokens or properties, substituted worker binaries, changed
private receipt paths, and timeout type/value substitutions. It
also rejects commands with non-string tokens. These are **checks
only**; the module contains no `subprocess` runner, invocation
CLI, command execution, root status change, or permission to
perform an Apple request. In particular, checking a command's
shape cannot establish that a trusted ledger reservation or
IPAccounting preflight has occurred; the unfinished root
controller must enforce the correct ordering separately.

The root-code trust manifest now includes this module (**12
root-controller code files**, alongside **21 worker files**).
Existing manifest-count tests were updated, and **30 new
offline-only tests** in
`tests/test_drive_batch_replay_command_policy.py` exercise
exact accepted plans and refusal paths.

The next expected full suite is **825 tests** (795 last verified
+30), **not yet workstation-verified**. The current installer,
systemd services, Polkit policy, worker CLI, timers and inventory
checkpoints were not modified or activated. Live Apple replay
requires a separately reviewed trusted executor and root
controller plus explicit user approval for installation and
network access. The 100 MB ledger remains a measured, sampled
advisory limit rather than a hard quota.

## Command policy enforced inside injected root wiring (2026-11-10)

The user's labeled **APPLE-SYNC QA RUN #825** fast-forwarded to
`a2d35a5` and verified **825/825 offline tests passed**, in
**4.717 seconds** (exit 0). This verifies the pure command
allowlist but **not** the new integration.

Previously `drive_batch_replay_root_wiring.run_wired` accepted
a caller-injected command executor directly. Although its
systemd adapter generated fixed command shapes, it did **not**
enforce the six-command allowlist immediately before calling
that executor. Root wiring now constructs
`ReplayCommandPolicy(uid,pid,receipt_dir)` before acquiring
the shared budget ledger and passes a locally wrapped executor
into `ReplaySystemdAdapter`. Each attempted command is checked
against the **exact tuple and timeout** first; anything else
fails closed before invoking the supplied executor. The policy
is fixed to the same immutable uid/pid/channel context as the
systemd adapter. Invalid context is refused before ledger
access. This is a second command-authority boundary; it does
**not** change the existing accounting state machine.

Added **22 new mock-only tests** in
`tests/test_drive_batch_replay_policy_wiring.py`. These verify
all six permitted operations pass, adapter-mediated start,
status, stop and zero-traffic preflight use the checked callback,
and foreign units, shell commands, `systemctl enable`,
`daemon-reload`, changed executable/receipt path, extra
systemd properties and invalid timeouts never reach the injected
executor. The tests also verify unsafe accounting results and
private backend exceptions are refused without exposure.

**Next expected suite: 847 tests** (825 last verified +22);
this stage has **not yet been tested on the workstation**.

This remains a deliberately **uninstalled, injection-only**
composition: there is no production command runner, no
root-owned manual service for replay, no live Apple invocation,
and the replay worker CLI still refuses `--execute`.
The existing one-pass scanner, probe, other services, timers,
archive, checkpoint and cumulative metering ledger were not
changed. Before any approved live diagnostic, the future
controller still needs its trusted root-owned command runner
with restricted environment, checked code tree and complete
failure-path validation. The 100 MB limit is measured,
not kernel-enforced.

## Dormant root command-transport facade (2026-11-10)

The user's labeled **APPLE-SYNC QA RUN #847** fast-forwarded to
`e4b133e` and verified **847/847 offline tests passed**, in
**4.209 seconds** with exit 0. That validates policy enforcement
inside the mocked root wiring, **not** the new facade.

Added `scripts/drive_batch_replay_root_executor.py`, an
**uninstalled and disconnected** root-only facade for the
separately authorized manual replay controller. It has
**no built-in subprocess runner, no executable CLI, no
systemd service and no ledger or Apple access**. It requires
a caller-supplied command callback, for which tests only
provide recording fakes. It checks `geteuid()==0` and
validates the exact argv/timeout against the six-command
`ReplayCommandPolicy` **before** forwarding anything to
that callback. It then transforms the two permitted command
programs to fixed absolute paths
`/usr/bin/systemd-run` and `/usr/bin/systemctl`, and
constructs a new fixed environment each time (`PATH`,
`HOME`, `LANG`, `LC_ALL` and `PYTHONNOUSERSITE`) rather
than inheriting root/system user environment, Python overrides
or cloud secrets.

Return handling requires an exact integer success exit code,
string stdout/stderr each bounded to 4096 characters and
NUL-free. For `systemctl show`, the existing exact-seven-field
systemd parser must accept the stdout before it is returned
to the existing accounting adapter; every other operation
returns empty stdout. The raw stderr is **never** forwarded
in the return object. Any callback exception, unsafe output,
failed status or untrusted command produces a fixed sanitized
error without including private command text or credentials.

**Important limitation:** an eventual *real* root subprocess
runner, if separately approved, must independently use
`shell=False`, `close_fds=True`, an exact supplied environment,
fixed executable paths, timeouts and **bounded capture while
the process is running**. The post-return bounds in this
facade alone do **not** prevent a malicious/hung subprocess
from producing unlimited output or consuming resources.
Neither this facade nor passing its input checks constitutes
live execution authorization, proof of IPAccounting, or
permission to resume the inventory.

The root-owned code trust manifest now includes the facade
(**13 root modules +21 worker modules**). Existing
manifest-count tests were updated; **30 new mock-only tests**
in `tests/test_drive_batch_replay_root_executor.py`
cover the fixed environment, allowlisted commands, path
substitution, hostile output and sanitized failures.
Expected next full suite: **877 tests** (847 previously
verified +30), **not yet workstation-verified**.

The original manual metered installer, systemd units, timers,
Polkit rules, shared metering ledger, checkpoint and replay
worker CLI were not changed. Root controller production
activation, approved installation, real bounded subprocess
runner and full failure-path QA remain unfinished. No live
Apple traffic or privilege installation was performed.

## Detached transport-to-accounting wiring (2026-11-10)

The user's **APPLE-SYNC QA RUN #877** fast-forwarded to
`01604ad` and verified **877/877 offline tests passed**
in **4.735 seconds**, exit 0. This verifies the
dormant transport facade, **not** the new wiring integration.

Added `drive_batch_replay_root_wiring.run_wired_with_transport`,
an **offline-injected, non-CLI** composition binding the
already-reviewed `ReplayExecutorBoundary` to
`run_wired` and its existing
`ReplayCommandPolicy` and accounting state machine.
This helper checks its own root EUID before creating the
boundary, opening the ledger, or contacting any callback.
It requires explicit caller-provided `invoke` and
uses no subprocess implementation or default runner.

When used in tests, this establishes both
(1) an exact six-command argv/timeout gate inside root
wiring and (2) the executor boundary's independent
root identity, command allowlist, pinned
`/usr/bin/systemd-run` or `/usr/bin/systemctl`
paths, freshly isolated environment and output validation.
The same fixed uid/pid/receipt directory context binds
both layers. Invalid context fails before the ledger is
opened; nonroot execution fails before the transport is
constructed. The result still goes through the existing
non-authorizing accounting result checks.

Added **20 mock-only integration tests** in
`tests/test_drive_batch_replay_transport_integration.py`,
covering six allowed operations, zero-traffic preflight
ordering, private channel and protected worker command,
fixed environment, no forwarded stderr, root refusal,
invalid context and foreign commands, secret-bearing
exceptions and invalid systemd property parsing.
These tests supply fake callbacks and fake ledgers only;
they do not start systemd or contact Apple.

**Expected next suite: 897 tests** (877 last verified
+20 new tests); **not yet workstation-verified**.

The original manual installer and services were unchanged.
No new root service, Polkit rule, recurring timer,
real subprocess executor, Apple network request, archive
mutation, checkpoint write or budget transaction was
performed. The replay worker's CLI remains intentionally
dormant. Before future approval of a live manual
experiment, additional security and failure-path
verification and a genuinely bounded trusted runner
are required; soft IP accounting is not a hard quota.

## Local acceptance sequence (no full scanner resume)

**Step A — offline tests only.** After pulling the current branch, run
`python3 -m unittest discover -s tests -q` and
`python3 scripts/drive_metered.py status`. Current expected count is
**897 tests**; confirm it from the actual workstation output. No Apple
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

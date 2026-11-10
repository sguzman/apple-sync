#!/usr/bin/env python3
"""Read-only, two-folder experimental iCloud Drive batch comparison worker.

Only a separately metered systemd service may execute a live probe. The
normal command is a preview. This worker NEVER saves inventory progress,
downloads file bodies, or writes a cloud object. Its private rclone RC socket
is temporary and archive-locked, and outputs are aggregate-only.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

import archive_batch as archive
import build_rclone_batch as build_identity
import diagnose_drive_downloads as diagnosis
import drive_batch_probe as comparison
import drive_batch_transport as transport
import drive_inventory as inventory
from drive_inventory_rc import RcloneRCReader, RcloneRCError

MAX_FOLDERS = 2
BATCH_SECONDS = 20
REFERENCE_SECONDS = 20
BINARY = (Path.home() / '.local' / 'share' / 'apple-sync' /
          'experimental' / 'rclone-batch-v1.75.2')


class WorkerError(Exception):
    """Fixed, privacy-safe read-only probe failure code."""


def validate_binary(binary: Path = BINARY) -> None:
    if binary.is_symlink() or not binary.is_file():
        raise WorkerError('EXPERIMENTAL_BINARY_MISSING_OR_UNSAFE')
    info = binary.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or not info.st_mode & stat.S_IXUSR
            or info.st_mode & 0o022):
        raise WorkerError('EXPERIMENTAL_BINARY_UNTRUSTED')
    # Never trust a similarly named executable merely because it is owned
    # by the user. Verify the pinned module, exact release and custom Go tag
    # before any live RC daemon is launched.
    try:
        build_identity.verify_binary_identity(binary)
    except (build_identity.BuildError, OSError):
        raise WorkerError('EXPERIMENTAL_BINARY_IDENTITY_MISMATCH') from None


def probe_once(*, binary: Path = BINARY, state_root: Path = archive.STATE) -> dict:
    """Execute from inside an IP-accounted systemd cgroup only.

    Only transient RC sockets live under state_root. The checkpoint file is
    read once and hashed before/after to reject concurrent modification.
    """
    validate_binary(binary)
    if diagnosis.backup_busy():
        raise WorkerError('PROBE_BACKUP_ACTIVE_OR_UNCERTAIN')
    if state_root.is_symlink() or not state_root.is_dir():
        raise WorkerError('UNSAFE_ARCHIVE_STATE')
    lock_path = archive.ROOT / '.apple-sync.lock'
    if lock_path.is_symlink() or not lock_path.is_file():
        raise WorkerError('ARCHIVE_LOCK_MISSING_OR_UNSAFE')
    if lock_path.stat().st_uid != os.getuid():
        raise WorkerError('ARCHIVE_LOCK_WRONG_OWNER')
    progress_path = inventory.progress_path(state_root)
    if progress_path.is_symlink() or not progress_path.is_file():
        raise WorkerError('INVENTORY_CHECKPOINT_MISSING')
    before = hashlib.sha256(progress_path.read_bytes()).digest()
    with lock_path.open('r+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkerError('OTHER_APPLE_ARCHIVE_ACTIVE') from None
        progress = inventory.load_progress(state_root)
        if progress is None:
            raise WorkerError('INVENTORY_CHECKPOINT_MISSING')
        if diagnosis.backup_busy():
            raise WorkerError('PROBE_BACKUP_ACTIVE_OR_UNCERTAIN')
        try:
            with RcloneRCReader(state_root, rclone_binary=str(binary)) as rc:
                result = comparison.compare_readonly(
                    progress, folder_limit=MAX_FOLDERS,
                    read_native=lambda batch: transport.fetch_batch_native(
                        rc.socket, batch, timeout=BATCH_SECONDS),
                    read_legacy=lambda folder: rc(folder,
                                                    timeout=REFERENCE_SECONDS),
                    backup_busy=diagnosis.backup_busy,
                    require_core_fields=True)
        except comparison.BatchProbeDeferred:
            raise WorkerError('PROBE_BACKUP_BECAME_ACTIVE') from None
        except comparison.BatchProbeError as exc:
            # BatchProbeError contains fixed, sanitized status codes only.
            # Preserve core-parity mismatch separately from identity mismatch
            # so the next troubleshooting step needs no raw Apple metadata.
            code = str(exc)
            if code in ('PROBE_CORE_FIELD_MISMATCH',
                        'PROBE_TIMESTAMP_MISMATCH',
                        'PROBE_TIMESTAMP_REPRESENTATION_MISMATCH',
                        'PROBE_TIMESTAMP_SUBSECOND_MISMATCH',
                        'PROBE_TIMESTAMP_FILE_VALUE_MISMATCH',
                        'PROBE_TIMESTAMP_DIRECTORY_VALUE_MISMATCH',
                        'PROBE_TIMESTAMP_UNVERIFIED',
                        'PROBE_REFERENCE_CORE_FIELD_MISSING',
                        'PROBE_NATIVE_CORE_METADATA_UNVERIFIED',
                        'PROBE_METADATA_MISMATCH',
                        'PROBE_NATIVE_READ_FAILED',
                        'PROBE_LEGACY_READ_FAILED'):
                raise WorkerError(code) from None
            raise WorkerError('PROBE_COMPARE_FAILED') from None
        except (transport.BatchTransportError, RcloneRCError):
            raise WorkerError('PROBE_COMPARE_FAILED') from None
        if hashlib.sha256(progress_path.read_bytes()).digest() != before:
            raise WorkerError('INVENTORY_CHECKPOINT_CHANGED')
    return result


RECEIPT_NAME = 'result.json'
MAX_RECEIPT_BYTES = 1024

# Report a category, never raw Apple responses, filenames, or tracebacks.
# The root-owned controller validates the same closed set.
FAILURE_CODES = frozenset((
    'PROBE_CORE_FIELD_MISMATCH', 'PROBE_TIMESTAMP_MISMATCH',
    'PROBE_TIMESTAMP_REPRESENTATION_MISMATCH',
    'PROBE_TIMESTAMP_SUBSECOND_MISMATCH',
    'PROBE_TIMESTAMP_FILE_VALUE_MISMATCH',
    'PROBE_TIMESTAMP_DIRECTORY_VALUE_MISMATCH',
    'PROBE_TIMESTAMP_UNVERIFIED',
    'PROBE_REFERENCE_CORE_FIELD_MISSING',
    'PROBE_NATIVE_CORE_METADATA_UNVERIFIED', 'PROBE_METADATA_MISMATCH',
    'PROBE_NATIVE_READ_FAILED', 'PROBE_LEGACY_READ_FAILED',
    'PROBE_COMPARE_FAILED', 'PROBE_BACKUP_BECAME_ACTIVE',
    'PROBE_BACKUP_ACTIVE_OR_UNCERTAIN', 'INVENTORY_CHECKPOINT_CHANGED',
    'INVENTORY_CHECKPOINT_MISSING', 'OTHER_APPLE_ARCHIVE_ACTIVE',
    'EXPERIMENTAL_BINARY_MISSING_OR_UNSAFE',
    'EXPERIMENTAL_BINARY_UNTRUSTED',
    'EXPERIMENTAL_BINARY_IDENTITY_MISMATCH',
    'UNSAFE_ARCHIVE_STATE', 'ARCHIVE_LOCK_MISSING_OR_UNSAFE',
    'ARCHIVE_LOCK_WRONG_OWNER', 'INVALID_PROBE_COMPARISON_RESULT',
    'PROBE_RESULT_TOO_LARGE', 'PROBE_RESULT_CHANNEL_UNTRUSTED',
    'LOCAL_CHECK_FAILED',
    'UNEXPECTED_WORKER_EXCEPTION',
))


def validate_result_channel(directory: Path) -> None:
    """Fail before Apple traffic if the private result channel is invalid."""
    if not directory.is_absolute() or directory.is_symlink():
        raise WorkerError('PROBE_RESULT_CHANNEL_UNTRUSTED')
    info = directory.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise WorkerError('PROBE_RESULT_CHANNEL_UNTRUSTED')


def publish_failure_receipt(code: str, *, directory: Path) -> None:
    """A tiny, exclusive private error receipt, never a success attestation."""
    if code not in FAILURE_CODES:
        code = 'UNEXPECTED_WORKER_EXCEPTION'
    validate_result_channel(directory)
    payload = (json.dumps({
        'version': 2, 'outcome': 'failed', 'code': code,
        'checkpoint_writes': 0,
    }, sort_keys=True) + '\n').encode('ascii')
    if len(payload) > MAX_RECEIPT_BYTES:
        raise WorkerError('PROBE_RESULT_TOO_LARGE')
    fd = os.open(directory / RECEIPT_NAME,
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def publish_receipt(result: dict, *, directory: Path) -> None:
    """Give the metered controller a private, explicit equivalence verdict.

    Journald delivery is not reliable enough to prove that comparison ran.
    The controller accepts this receipt only with a successful worker exit.
    No raw Apple metadata, folder names or identifiers are written.
    """
    fields = ('folders_matched', 'entries_matched', 'files_matched',
              'directories_matched', 'checkpoint_writes',
              'checkpoints_advanced')
    proof_fields = ('core_fields_verified', 'timestamps_verified')
    if (not isinstance(result, dict) or result.get('equivalent') is not True
            or any(type(result.get(key)) is not int for key in fields)
            or any(result.get(key) is not True for key in proof_fields)
            or not 1 <= result['folders_matched'] <= MAX_FOLDERS
            or not 0 <= result['entries_matched'] <= 1_000_000
            or not 0 <= result['files_matched'] <= result['entries_matched']
            or not 0 <= result['directories_matched'] <= result['entries_matched']
            or result['files_matched'] + result['directories_matched']
            != result['entries_matched']
            or result['checkpoint_writes'] != 0
            or result['checkpoints_advanced'] != 0):
        raise WorkerError('INVALID_PROBE_COMPARISON_RESULT')
    validate_result_channel(directory)
    receipt = {'version': 2, 'equivalent': True,
               'core_fields_verified': True, 'timestamps_verified': True}
    receipt.update({key: result[key] for key in fields})
    payload = (json.dumps(receipt, sort_keys=True) + '\n').encode('ascii')
    if len(payload) > MAX_RECEIPT_BYTES:
        raise WorkerError('PROBE_RESULT_TOO_LARGE')
    fd = os.open(directory / RECEIPT_NAME,
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true',
                        help='only valid inside separately metered systemd service')
    args = parser.parse_args()
    if not args.execute:
        print('PROBE_PREVIEW: no Apple network requests; at most 2 folders; '
              'no checkpoint writes; requires separate IP-accounted systemd unit')
        return 0
    if (os.geteuid() == 0 or
            not os.environ.get('INVOCATION_ID') or
            not os.environ.get('APPLE_SYNC_BATCH_METERED') == '1'):
        print('PROBE_REFUSED: METERED_SERVICE_REQUIRED', file=sys.stderr)
        return 2
    # Validate the receipt channel before a metered Apple read. Never do
    # network work and only then discover that failure reporting is missing.
    receipt_dir = os.environ.get('APPLE_SYNC_BATCH_RECEIPT_DIR')
    if not receipt_dir:
        print('PROBE_FAILED: PROBE_RESULT_CHANNEL_MISSING',
              file=sys.stderr, flush=True)
        return 2
    channel = Path(receipt_dir)
    try:
        validate_result_channel(channel)
        result = probe_once()
        publish_receipt(result, directory=channel)
    except Exception as exc:
        category = (str(exc) if isinstance(exc, WorkerError)
                    else 'LOCAL_CHECK_FAILED' if isinstance(
                        exc, (inventory.InventoryError, OSError))
                    else 'UNEXPECTED_WORKER_EXCEPTION')
        if category not in FAILURE_CODES:
            category = 'UNEXPECTED_WORKER_EXCEPTION'
        try:
            publish_failure_receipt(category, directory=channel)
        except (OSError, WorkerError):
            # The controller will correctly report a missing/untrusted
            # receipt. Never claim success or expose arbitrary exception text.
            pass
        print('PROBE_FAILED: ' + category, file=sys.stderr, flush=True)
        return 2
    print('PROBE_EQUIVALENCE: equivalent=' +
          ('yes' if result['equivalent'] else 'no') +
          f', folders={result["folders_matched"]}, ' +
          f'entries={result["entries_matched"]}, ' +
          f'files={result["files_matched"]}, ' +
          f'dirs={result["directories_matched"]}, ' +
          'checkpoint_writes=0; core_fields_verified=yes; '
          'timestamps_verified=yes; bandwidth evaluated by root ledger separately',
          flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

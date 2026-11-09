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
import os
from pathlib import Path
import stat
import sys

import archive_batch as archive
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
                    backup_busy=diagnosis.backup_busy)
        except comparison.BatchProbeDeferred:
            raise WorkerError('PROBE_BACKUP_BECAME_ACTIVE') from None
        except (comparison.BatchProbeError, transport.BatchTransportError,
                RcloneRCError):
            raise WorkerError('PROBE_COMPARE_FAILED') from None
        if hashlib.sha256(progress_path.read_bytes()).digest() != before:
            raise WorkerError('INVENTORY_CHECKPOINT_CHANGED')
    return result


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
    try:
        result = probe_once()
    except (WorkerError, inventory.InventoryError, OSError) as exc:
        category = str(exc) if isinstance(exc, WorkerError) else 'LOCAL_CHECK_FAILED'
        print('PROBE_FAILED: ' + category, file=sys.stderr, flush=True)
        return 2
    print('PROBE_EQUIVALENCE: equivalent=' +
          ('yes' if result['equivalent'] else 'no') +
          f', folders={result["folders_matched"]}, ' +
          f'entries={result["entries_matched"]}, ' +
          f'files={result["files_matched"]}, ' +
          f'dirs={result["directories_matched"]}, ' +
          'checkpoint_writes=0; bandwidth evaluated by root ledger separately',
          flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

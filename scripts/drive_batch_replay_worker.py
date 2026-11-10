#!/usr/bin/env python3
"""Dormant read-only Apple Drive replay worker; NO executable live entrypoint.

This module is not wired into any installed systemd unit or launcher.
A separately authorized, root-metered controller must be implemented and
offline-tested before it may call replay_once. It must stop the worker
cgroup and account bytes before accepting any private receipt. Even a
successful replay is NOT a metadata-equivalence or checkpoint-write proof.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

import archive_batch as archive
import diagnose_drive_downloads as diagnosis
import drive_batch_probe_worker as original_worker
import drive_batch_replay_protocol as protocol
import drive_batch_transport as transport
import drive_inventory as inventory
from drive_inventory_rc import RcloneRCReader, RcloneRCError

BINARY = original_worker.BINARY
REQUEST_TIMEOUT = 15
RESULT_NAME = 'replay.json'
MAX_RESULT_BYTES = 1024
REPLAY_OUTCOMES = frozenset((
    'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED',
    'REPLAY_SINGLE_READS_DIFFER_OBSERVED',
    'REPLAY_MIXED_DIFFERENCES_OBSERVED',
    'REPLAY_ALL_DIRECTORY_TIMESTAMPS_IDENTICAL',
))
REPLAY_FAILURES = frozenset((
    'REPLAY_BACKUP_ACTIVE_OR_UNCERTAIN',
    'REPLAY_BACKUP_INTERRUPTED',
    'REPLAY_ARCHIVE_LOCK_UNTRUSTED',
    'REPLAY_ARCHIVE_LOCK_BUSY',
    'REPLAY_STATE_UNTRUSTED',
    'REPLAY_CHECKPOINT_MISSING',
    'REPLAY_CHECKPOINT_CHANGED',
    'REPLAY_EXPERIMENTAL_BINARY_UNVERIFIED',
    'REPLAY_NATIVE_READ_FAILED',
    'REPLAY_INVALID_CHECKPOINT',
    'REPLAY_REQUIRES_TWO_ELIGIBLE_FOLDERS',
    'REPLAY_REQUIRES_TWO_FOLDERS',
    'REPLAY_UNVERIFIED_NATIVE_RESPONSE',
    'REPLAY_DUPLICATE_CHILD_ID',
    'REPLAY_CHILD_IDENTITY_CHANGED',
    'REPLAY_NON_TIME_METADATA_CHANGED',
    'REPLAY_NO_DIRECTORY_CHILDREN',
    'REPLAY_COMPARISON_FAILED',
    'REPLAY_RESULT_CHANNEL_UNTRUSTED',
    'REPLAY_RESULT_INVALID',
    'REPLAY_LOCAL_FAILURE',
))


class ReplayWorkerError(Exception):
    """Only fixed codes; no paths, cloud payloads or exception details."""


def replay_once(*, binary: Path = BINARY,
                state_root: Path = archive.STATE) -> dict:
    """Three native observations, inside a future dedicated metered cgroup.

    The controller, currently NOT IMPLEMENTED, owns the cumulative budget.
    This function never claims that a supplied callback or environment
    proves IPAccounting. The user must not invoke this function directly.
    """
    try:
        original_worker.validate_binary(binary)
    except (original_worker.WorkerError, OSError):
        raise ReplayWorkerError('REPLAY_EXPERIMENTAL_BINARY_UNVERIFIED') from None
    if diagnosis.backup_busy():
        raise ReplayWorkerError('REPLAY_BACKUP_ACTIVE_OR_UNCERTAIN')
    if state_root.is_symlink() or not state_root.is_dir():
        raise ReplayWorkerError('REPLAY_STATE_UNTRUSTED')
    lock_path = archive.ROOT / '.apple-sync.lock'
    if (lock_path.is_symlink() or not lock_path.is_file()
            or lock_path.stat().st_uid != os.getuid()):
        raise ReplayWorkerError('REPLAY_ARCHIVE_LOCK_UNTRUSTED')
    checkpoint_path = inventory.progress_path(state_root)
    if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
        raise ReplayWorkerError('REPLAY_CHECKPOINT_MISSING')
    checkpoint_hash = hashlib.sha256(checkpoint_path.read_bytes()).digest()
    with lock_path.open('r+') as archive_lock:
        try:
            fcntl.flock(archive_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReplayWorkerError('REPLAY_ARCHIVE_LOCK_BUSY') from None
        progress = inventory.load_progress(state_root)
        if progress is None:
            raise ReplayWorkerError('REPLAY_CHECKPOINT_MISSING')
        if diagnosis.backup_busy():
            raise ReplayWorkerError('REPLAY_BACKUP_ACTIVE_OR_UNCERTAIN')
        try:
            with RcloneRCReader(state_root, rclone_binary=str(binary)) as rc:
                result = protocol.run_bracketed(
                    progress,
                    read_native=lambda batch: transport.fetch_batch_native(
                        rc.socket, batch, timeout=REQUEST_TIMEOUT),
                    backup_busy=diagnosis.backup_busy)
        except protocol.ReplayProtocolDeferred:
            raise ReplayWorkerError('REPLAY_BACKUP_INTERRUPTED') from None
        except protocol.ReplayProtocolError as exc:
            code = str(exc)
            if code not in REPLAY_FAILURES:
                code = 'REPLAY_COMPARISON_FAILED'
            raise ReplayWorkerError(code) from None
        except (transport.BatchTransportError, RcloneRCError):
            raise ReplayWorkerError('REPLAY_NATIVE_READ_FAILED') from None
        if hashlib.sha256(checkpoint_path.read_bytes()).digest() != checkpoint_hash:
            raise ReplayWorkerError('REPLAY_CHECKPOINT_CHANGED')
    if diagnosis.backup_busy():
        raise ReplayWorkerError('REPLAY_BACKUP_INTERRUPTED')
    return result


def _trusted_channel(directory: Path) -> None:
    if not directory.is_absolute() or directory.is_symlink():
        raise ReplayWorkerError('REPLAY_RESULT_CHANNEL_UNTRUSTED')
    try:
        info = directory.lstat()
    except OSError:
        raise ReplayWorkerError('REPLAY_RESULT_CHANNEL_UNTRUSTED') from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ReplayWorkerError('REPLAY_RESULT_CHANNEL_UNTRUSTED')


def _write_receipt(directory: Path, payload: dict) -> None:
    _trusted_channel(directory)
    encoded = (json.dumps(payload, sort_keys=True, separators=(',', ':')) +
               '\n').encode('ascii')
    if len(encoded) > MAX_RESULT_BYTES:
        raise ReplayWorkerError('REPLAY_RESULT_INVALID')
    fd = os.open(directory / RESULT_NAME,
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())


def publish_observation(result: dict, *, directory: Path) -> None:
    """Version 1 OBSERVATION only; can never be read as equivalence proof."""
    counts = ('directory_children_checked', 'directory_children_stable',
              'directory_children_batch_only_difference',
              'directory_children_single_read_difference')
    expected = set(counts) | {
        'version', 'outcome', 'checkpoint_writes', 'checkpoints_advanced',
        'production_commit_authorized', 'replay_requests_completed',
        'replay_folders_selected', 'measured_ip_bytes',
        'ip_accounting_verified',
    }
    if (not isinstance(result, dict) or set(result) != expected
            or type(result['version']) is not int or result['version'] != 1
            or result['outcome'] not in REPLAY_OUTCOMES
            or any(type(result[field]) is not int for field in counts)
            or not 1 <= result['directory_children_checked'] <= 1_000_000
            or any(result[field] < 0 for field in counts[1:])
            or sum(result[field] for field in counts[1:])
            != result['directory_children_checked']
            or type(result['replay_requests_completed']) is not int
            or result['replay_requests_completed'] != 3
            or type(result['replay_folders_selected']) is not int
            or result['replay_folders_selected'] != 2
            or type(result['checkpoint_writes']) is not int
            or result['checkpoint_writes'] != 0
            or type(result['checkpoints_advanced']) is not int
            or result['checkpoints_advanced'] != 0
            or result['production_commit_authorized'] is not False
            or result['measured_ip_bytes'] is not None
            or result['ip_accounting_verified'] is not False):
        raise ReplayWorkerError('REPLAY_RESULT_INVALID')
    receipt = {
        'version': 1,
        'kind': 'observation',
        'outcome': result['outcome'],
        'checkpoint_writes': 0,
        'checkpoints_advanced': 0,
        'production_commit_authorized': False,
        'replay_requests_completed': 3,
        'replay_folders_selected': 2,
    }
    receipt.update({field: result[field] for field in counts})
    _write_receipt(directory, receipt)


def publish_failure(code: str, *, directory: Path) -> None:
    if code not in REPLAY_FAILURES:
        code = 'REPLAY_LOCAL_FAILURE'
    _write_receipt(directory, {
        'version': 1, 'kind': 'failure', 'code': code,
        'checkpoint_writes': 0, 'production_commit_authorized': False,
    })


if __name__ == '__main__':
    print('REPLAY_WORKER_DORMANT: no live entrypoint or metered launcher')

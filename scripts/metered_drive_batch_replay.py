#!/usr/bin/env python3
"""Dormant root-side receipt validator for a future metered replay controller.

No CLI, systemd launch, account access, Apple transport or ledger mutation.
The future root-owned controller must stop and confirm the entire transient
worker cgroup, durably reconcile the shared budget, and *only then* call the
receipt reader. A replay observation is never an equivalence attestation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import stat

USER = 'sguzman'
RESULT_NAME = 'replay.json'
MAX_RESULT_BYTES = 1024

OBSERVATION_OUTCOMES = frozenset((
    'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED',
    'REPLAY_SINGLE_READS_DIFFER_OBSERVED',
    'REPLAY_MIXED_DIFFERENCES_OBSERVED',
    'REPLAY_ALL_DIRECTORY_TIMESTAMPS_IDENTICAL',
))
FAILURE_CODES = frozenset((
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
COUNTS = (
    'directory_children_checked',
    'directory_children_stable',
    'directory_children_batch_only_difference',
    'directory_children_single_read_difference',
)
OBSERVATION_KEYS = frozenset(COUNTS) | frozenset((
    'version', 'kind', 'outcome',
    'checkpoint_writes', 'checkpoints_advanced',
    'production_commit_authorized',
    'replay_requests_completed', 'replay_folders_selected',
))
FAILURE_KEYS = frozenset((
    'version', 'kind', 'code', 'checkpoint_writes',
    'production_commit_authorized',
))


class ReplayMeterError(Exception):
    """Fixed controller failure, never private worker data."""


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    """Reject duplicate JSON properties instead of last-value-wins."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def read_private_receipt(directory: Path) -> dict:
    """Open user-owned 0700 directory and 0600 regular file by dirfd.

    The supplied path is a preselected temporary runtime channel, *never*
    an arbitrary user argument. O_NOFOLLOW and open-by-dirfd prevent a
    replacement symlink from being accepted after lstat.
    """
    if not isinstance(directory, Path) or not directory.is_absolute():
        raise ReplayMeterError('REPLAY_RECEIPT_CHANNEL_UNTRUSTED')
    try:
        user = pwd.getpwnam(USER)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY |
                              os.O_NOFOLLOW | os.O_CLOEXEC)
    except (OSError, KeyError):
        raise ReplayMeterError('REPLAY_RECEIPT_CHANNEL_UNTRUSTED') from None
    try:
        info = os.fstat(directory_fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != user.pw_uid
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ReplayMeterError('REPLAY_RECEIPT_CHANNEL_UNTRUSTED')
        try:
            fd = os.open(RESULT_NAME, os.O_RDONLY | os.O_NOFOLLOW |
                         os.O_CLOEXEC, dir_fd=directory_fd)
        except OSError:
            raise ReplayMeterError('REPLAY_RECEIPT_UNAVAILABLE') from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode)
                    or info.st_uid != user.pw_uid
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                    or not 0 < info.st_size <= MAX_RESULT_BYTES):
                raise ReplayMeterError('REPLAY_RECEIPT_UNTRUSTED')
            with os.fdopen(fd, 'rb', closefd=False) as inp:
                encoded = inp.read(MAX_RESULT_BYTES + 1)
            if not 0 < len(encoded) <= MAX_RESULT_BYTES:
                raise ReplayMeterError('REPLAY_RECEIPT_UNTRUSTED')
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)
    try:
        data = json.loads(encoded.decode('ascii'),
                          object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError):
        raise ReplayMeterError('REPLAY_RECEIPT_INVALID') from None
    if type(data) is not dict:
        raise ReplayMeterError('REPLAY_RECEIPT_INVALID')
    return data


def _counts_match_outcome(data: dict) -> bool:
    batch = data['directory_children_batch_only_difference']
    single = data['directory_children_single_read_difference']
    if batch and single:
        category = 'REPLAY_MIXED_DIFFERENCES_OBSERVED'
    elif batch:
        category = 'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED'
    elif single:
        category = 'REPLAY_SINGLE_READS_DIFFER_OBSERVED'
    else:
        category = 'REPLAY_ALL_DIRECTORY_TIMESTAMPS_IDENTICAL'
    return data['outcome'] == category


def validate_observation_data(data: object) -> dict:
    """Pure exact-schema check, also required after injected controller reads."""
    if (type(data) is not dict or set(data) != OBSERVATION_KEYS
            or type(data['version']) is not int or data['version'] != 1
            or data['kind'] != 'observation'
            or type(data['outcome']) is not str
            or data['outcome'] not in OBSERVATION_OUTCOMES
            or any(type(data[k]) is not int for k in COUNTS)
            or not 1 <= data['directory_children_checked'] <= 1_000_000
            or any(data[k] < 0 for k in COUNTS[1:])
            or sum(data[k] for k in COUNTS[1:])
            != data['directory_children_checked']
            or not _counts_match_outcome(data)
            or type(data['replay_requests_completed']) is not int
            or data['replay_requests_completed'] != 3
            or type(data['replay_folders_selected']) is not int
            or data['replay_folders_selected'] != 2
            or type(data['checkpoint_writes']) is not int
            or data['checkpoint_writes'] != 0
            or type(data['checkpoints_advanced']) is not int
            or data['checkpoints_advanced'] != 0
            or data['production_commit_authorized'] is not False):
        raise ReplayMeterError('REPLAY_RECEIPT_INVALID')
    return data



def read_observation(directory: Path) -> dict:
    """Read a private version-1 observation; never an equivalence proof."""
    return validate_observation_data(read_private_receipt(directory))


def read_failure(directory: Path) -> str:
    """Failure categories are never counted as observations or equivalence."""
    data = read_private_receipt(directory)
    if (set(data) != FAILURE_KEYS
            or type(data['version']) is not int or data['version'] != 1
            or data['kind'] != 'failure'
            or type(data['code']) is not str
            or data['code'] not in FAILURE_CODES
            or type(data['checkpoint_writes']) is not int
            or data['checkpoint_writes'] != 0
            or data['production_commit_authorized'] is not False):
        raise ReplayMeterError('REPLAY_RECEIPT_INVALID')
    return data['code']


if __name__ == '__main__':
    print('REPLAY_METER_DORMANT: receipt validation only; no service launch')

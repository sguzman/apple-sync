#!/usr/bin/env python3
"""Pure, read-only orchestration for an iCloud Drive batch equivalence probe.

This module intentionally has no command-line entrypoint, no subprocesses,
no archive writes and no credentials. A separate metered, backup-locked
controller is required before supplying any live request callbacks.

At most two pending folders are sampled, and their complete native batch
reply is compared against ordinary nonrecursive rclone listings. A match
does not by itself establish bandwidth savings, timestamps, or permission
to advance an inventory checkpoint.
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from drive_batch_contract import BatchContractError, plan_batches
from drive_batch_equivalence import BatchEquivalenceError, compare_with_legacy
from drive_batch_parity import BatchParityError, compare_core_fields

MAX_PROBE_FOLDERS = 2


class BatchProbeError(Exception):
    """Fixed diagnostic code, never raw paths, IDs or Apple responses."""


class BatchProbeDeferred(BatchProbeError):
    """A backup is running or backup status could not be established."""


def compare_readonly(
        progress: dict, *,
        read_native: Callable[[list[tuple[str, str]]], object],
        read_legacy: Callable[[str], list[dict]],
        backup_busy: Callable[[], bool],
        folder_limit: int = MAX_PROBE_FOLDERS,
        require_core_fields: bool = False) -> dict:
    """Compare exactly one batch with independently fetched legacy listings.

    Callers must perform outer systemd IPAccounting and root-owned budget
    reservation. This pure method does not and CANNOT meter live traffic.
    Its returned aggregate never includes folder paths or Drive IDs.
    """
    if type(folder_limit) is not int or not 1 <= folder_limit <= MAX_PROBE_FOLDERS:
        raise BatchProbeError('INVALID_PROBE_FOLDER_LIMIT')
    if type(require_core_fields) is not bool:
        raise BatchProbeError('INVALID_CORE_PARITY_MODE')
    try:
        # Copy so even a callback unexpectedly mutating its argument
        # cannot change the source checkpoint object held by the caller.
        selection = plan_batches(deepcopy(progress), size=folder_limit)
    except (BatchContractError, ValueError, TypeError, AttributeError):
        raise BatchProbeError('PROBE_INELIGIBLE_CHECKPOINT') from None
    if not selection:
        raise BatchProbeError('PROBE_NO_PENDING_FOLDERS')
    batch = selection[0]

    def require_idle() -> None:
        try:
            blocked = backup_busy()
        except Exception:
            raise BatchProbeDeferred('PROBE_BACKUP_STATE_UNKNOWN') from None
        if type(blocked) is not bool:
            raise BatchProbeDeferred('PROBE_BACKUP_STATE_UNKNOWN')
        if blocked:
            raise BatchProbeDeferred('PROBE_OTHER_BACKUP_ACTIVE')

    require_idle()
    try:
        raw = read_native(batch)
    except Exception:
        raise BatchProbeError('PROBE_NATIVE_READ_FAILED') from None
    require_idle()
    references: list[tuple[str, list[dict]]] = []
    for folder, _drive_id in batch:
        require_idle()
        try:
            listing = read_legacy(folder)
        except Exception:
            raise BatchProbeError('PROBE_LEGACY_READ_FAILED') from None
        require_idle()
        references.append((folder, listing))
    try:
        if require_core_fields:
            result = compare_core_fields(batch, raw, references)
        else:
            result = compare_with_legacy(batch, raw, references)
    except BatchParityError as exc:
        safe = {
            'BATCH_TIMESTAMP_MISMATCH': 'PROBE_TIMESTAMP_MISMATCH',
            'BATCH_TIMESTAMP_REPRESENTATION_MISMATCH':
                'PROBE_TIMESTAMP_REPRESENTATION_MISMATCH',
            'BATCH_TIMESTAMP_SUBSECOND_MISMATCH':
                'PROBE_TIMESTAMP_SUBSECOND_MISMATCH',
            'BATCH_TIMESTAMP_FILE_VALUE_MISMATCH':
                'PROBE_TIMESTAMP_FILE_VALUE_MISMATCH',
            'BATCH_TIMESTAMP_DIRECTORY_VALUE_MISMATCH':
                'PROBE_TIMESTAMP_DIRECTORY_VALUE_MISMATCH',
            'BATCH_TIMESTAMP_UNVERIFIED': 'PROBE_TIMESTAMP_UNVERIFIED',
            'BATCH_REFERENCE_FIELD_MISSING': 'PROBE_REFERENCE_CORE_FIELD_MISSING',
            'BATCH_NATIVE_CORE_METADATA_UNVERIFIED':
                'PROBE_NATIVE_CORE_METADATA_UNVERIFIED',
        }.get(str(exc), 'PROBE_CORE_FIELD_MISMATCH')
        raise BatchProbeError(safe) from None
    except (BatchEquivalenceError, BatchContractError):
        raise BatchProbeError('PROBE_METADATA_MISMATCH') from None
    require_idle()
    # Explicitly indicate that this is a comparison only. No output may
    # be presented as a completed inventory folder or full metadata proof.
    return {**result, 'equivalent': True,
            'sampled_pending_folders': len(batch),
            'checkpoints_advanced': 0,
            'measured_ip_bytes': None,
            'core_fields_verified': require_core_fields,
            'timestamps_verified': require_core_fields}

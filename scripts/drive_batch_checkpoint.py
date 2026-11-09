#!/usr/bin/env python3
"""Pure, fail-closed conversion and atomic batch checkpoint planning.

Experimental Apple batch results do not yet have write authority. This module
performs NO network requests and NO disk IO, and it cannot resume a service or
advance the installed scanner. A future, separately authorized metered worker
must verify timestamp/field fidelity before applying a planned checkpoint.
Never put private paths, names or IDs in error messages.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re

import drive_inventory as inventory
from drive_batch_contract import (BatchContractError, DIRECTORY_TYPES,
                                  plan_batches, validate_response)
from drive_pending_id_audit import DRIVE_ID

MAX_COMMIT_FOLDERS = 4
FILE_ID = re.compile(r'^FILE::[^:#/\s]+::[^#/\s]+$')
RFC3339 = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}'
    r'(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$')


class BatchCheckpointError(Exception):
    """Fixed, privacy-safe response/checkpoint validation failure."""


def valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        return False
    try:
        parsed = datetime.fromisoformat(
            value.replace('Z', '+00:00'))
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    except (ValueError, OverflowError):
        return False


def native_child_to_record(child: dict) -> dict:
    """Convert only fields whose rclone v1.75.2 semantics are understood.

    The tagged backend already normalizes the name and extension with
    ToStandardName. Do not re-encode them here. MIME, hashes and timestamp
    equivalence remain unproven and must be separately gated before use.
    """
    if not isinstance(child, dict):
        raise BatchCheckpointError('INVALID_BATCH_CHILD')
    kind = child.get('type')
    is_dir = kind in DIRECTORY_TYPES
    if kind != 'FILE' and not is_dir:
        raise BatchCheckpointError('UNSUPPORTED_BATCH_CHILD_TYPE')
    name = child.get('name')
    extension = child.get('extension', '')
    if (not isinstance(name, str) or not name or
            not isinstance(extension, str)):
        raise BatchCheckpointError('INVALID_BATCH_CHILD_NAME')
    full_name = name + ('.' + extension if extension else '')
    if (not inventory.diagnosis.valid_path(full_name) or '/' in full_name
            or '\\' in full_name):
        raise BatchCheckpointError('UNSAFE_BATCH_CHILD_NAME')
    identifier = child.get('drivewsid')
    if not isinstance(identifier, str) or '#' in identifier:
        raise BatchCheckpointError('INVALID_BATCH_CHILD_ID')
    if is_dir and (not DRIVE_ID.fullmatch(identifier)
                   or '#' in identifier):
        raise BatchCheckpointError('INVALID_BATCH_DIRECTORY_ID')
    if not is_dir and not FILE_ID.fullmatch(identifier):
        raise BatchCheckpointError('INVALID_BATCH_FILE_ID')
    modified = child.get('dateModified')
    if not valid_timestamp(modified):
        raise BatchCheckpointError('UNVERIFIED_BATCH_MODTIME')
    size = child.get('assetQuota') if is_dir else child.get('size')
    if type(size) is not int or size < -1:
        raise BatchCheckpointError('UNVERIFIED_BATCH_SIZE')
    if is_dir:
        etag = child.get('etag')
        if not isinstance(etag, str) or any(c in etag for c in ('#', '\r', '\n', '\x00')):
            raise BatchCheckpointError('UNVERIFIED_BATCH_DIRECTORY_ETAG')
        identifier += '#' + etag
    return {'Path': full_name, 'Name': full_name, 'Size': size,
            'ModTime': modified, 'IsDir': is_dir, 'ID': identifier}


def convert_batch(batch: list[tuple[str, str]], response: object
                  ) -> list[tuple[str, list[dict]]]:
    """Validate the complete native envelope before returning any records."""
    try:
        folders = validate_response(batch, response)
        converted = []
        for folder, children in folders:
            records = [native_child_to_record(child) for child in children]
            # Validate all immediate paths and directory uniqueness.
            inventory.rebase_folder(folder, records)
            converted.append((folder, records))
        return converted
    except (BatchContractError, inventory.InventoryError, BatchCheckpointError):
        raise BatchCheckpointError('BATCH_CONVERSION_NOT_VERIFIED') from None


def plan_checkpoint(progress: dict, response: object, *,
                    batch_size: int = MAX_COMMIT_FOLDERS) -> tuple[dict, dict]:
    """Compute a single atomic checkpoint representing several finished folders.

    No state is saved. A network controller must obtain any remote response
    inside the original exclusive archive lock and cumulative metered cgroup.
    Only a future explicit and separately tested gate may persist this plan.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_COMMIT_FOLDERS:
        raise BatchCheckpointError('INVALID_BATCH_CHECKPOINT_SIZE')
    try:
        if (not isinstance(progress, dict) or
                progress.get('version') != 1 or
                progress.get('source') != inventory.SOURCE or
                not isinstance(progress.get('pending'), list) or
                not isinstance(progress.get('finished'), list) or
                not isinstance(progress.get('entries'), list) or
                not progress['pending']):
            raise BatchCheckpointError('INVALID_BATCH_CHECKPOINT')
        # Deep copy protects caller checkpoint, even if intermediate
        # validation or conversion fails.
        updated = deepcopy(progress)
        batch = plan_batches(updated, size=batch_size)[0]
        converted = convert_batch(batch, response)
        if [folder for folder, _ in converted] != [folder for folder, _ in batch]:
            raise BatchCheckpointError('BATCH_FOLDER_ORDER_MISMATCH')
        scanned_files = scanned_dirs = 0
        for folder, children in converted:
            if not updated['pending'] or updated['pending'][0] != folder:
                raise BatchCheckpointError('BATCH_CHECKPOINT_ORDER_MISMATCH')
            records, subfolders = inventory.rebase_folder(folder, children)
            known_paths = set(updated['pending'] + updated['finished'])
            newly_discovered = [name for name in subfolders if name not in known_paths]
            updated['pending'] = updated['pending'][1:] + newly_discovered
            updated['finished'].append(folder)
            updated['entries'].extend(records)
            scanned_dirs += sum(1 for row in records if row['IsDir'])
            scanned_files += sum(1 for row in records if not row['IsDir'])
        if (len(updated['entries']) > inventory.MAX_TOTAL_RECORDS or
                len(updated['pending']) + len(updated['finished'])
                > inventory.MAX_PENDING_FOLDERS or
                len(set(updated['pending'] + updated['finished']))
                != len(updated['pending']) + len(updated['finished'])):
            raise BatchCheckpointError('BATCH_CHECKPOINT_LIMIT_EXCEEDED')
        if updated['entries']:
            inventory.inspect_entries(updated['entries'])
        if updated['pending']:
            # Any newly discovered directory must itself have a stable ID
            # before any future batch can be planned.
            plan_batches(updated, size=batch_size)
    except (BatchContractError, inventory.InventoryError, TypeError, ValueError,
            KeyError, AttributeError):
        raise BatchCheckpointError('BATCH_CHECKPOINT_NOT_ELIGIBLE') from None
    return updated, {
        'folders_prepared': len(converted),
        'entries_prepared': scanned_files + scanned_dirs,
        'files_prepared': scanned_files,
        'directories_prepared': scanned_dirs,
        'checkpoints_written': 0,
        'production_commit_authorized': False,
        'timestamps_verified': False,
        'full_field_parity_verified': False,
    }

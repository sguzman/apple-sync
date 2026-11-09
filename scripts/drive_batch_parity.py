#!/usr/bin/env python3
"""Pure parity audit for converting Apple batch metadata into rclone records.

The first live probe proved IDs, names, types, ETags and sizes matched on a
two-folder sample. This stricter offline gate additionally compares ModTime
and Name to independently read rclone entries. It never writes a checkpoint
or performs network calls. Unmodeled fields remain explicitly unverified.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

from drive_batch_checkpoint import (BatchCheckpointError,
                                    convert_batch, valid_timestamp)
from drive_batch_equivalence import (BatchEquivalenceError,
                                     compare_with_legacy)

CORE_FIELDS = frozenset(('Path', 'Name', 'Size', 'ModTime', 'IsDir', 'ID'))


class BatchParityError(Exception):
    """Fixed privacy-safe failure; never reveal private cloud metadata."""


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FRACTION = re.compile(r'\\.(\\d{1,9})(?:Z|[+-]\\d{2}:\\d{2})        batch: list[tuple[str, str]], native: object,
        references: list[tuple[str, list[dict]]]) -> dict:
    """Require the same fields on all records before approving field parity.

    This is intentionally stricter than the first two-folder identity probe.
    It compares the exact RFC3339 timestamp string, without silently
    discarding nanoseconds or offset information. If the two rclone paths
    serialize equivalent time instants differently, defer adoption until
    that difference can be explained and tested.
    """
    try:
        identity = compare_with_legacy(batch, native, references)
        converted = convert_batch(batch, native)
        if (not isinstance(references, list) or
                len(references) != len(converted)):
            raise BatchParityError('BATCH_REFERENCE_FOLDERS_MISSING')
        reference_by_folder = {}
        for folder, entries in references:
            if (not isinstance(folder, str) or folder in reference_by_folder
                    or not isinstance(entries, list)):
                raise BatchParityError('BATCH_REFERENCE_FOLDERS_INVALID')
            reference_by_folder[folder] = entries
        if set(reference_by_folder) != {name for name, _ in converted}:
            raise BatchParityError('BATCH_REFERENCE_FOLDER_SET_MISMATCH')
        checked = 0
        for folder, records in converted:
            source = reference_by_folder[folder]
            if len(source) != len(records):
                raise BatchParityError('BATCH_REFERENCE_COUNT_MISMATCH')
            native_by_id = {row['ID']: row for row in records}
            if len(native_by_id) != len(records):
                raise BatchParityError('BATCH_DUPLICATE_CONVERTED_ID')
            observed_ids = set()
            for row in source:
                if not isinstance(row, dict) or not CORE_FIELDS <= row.keys():
                    raise BatchParityError('BATCH_REFERENCE_FIELD_MISSING')
                if row['ID'] in observed_ids:
                    raise BatchParityError('BATCH_DUPLICATE_REFERENCE_ID')
                observed_ids.add(row['ID'])
                expected = native_by_id.get(row['ID'])
                if expected is None:
                    raise BatchParityError('BATCH_REFERENCE_ID_MISMATCH')
                # Report categories only, not the private values.
                # Exact timestamp differences are a separate migration
                # blocker from other metadata disagreements.
                if (not valid_timestamp(row['ModTime'])
                        or type(row['ModTime']) is not type(expected['ModTime'])
                        or row['ModTime'] != expected['ModTime']):
                    # The classification is diagnostic-only. Even equivalent
                    # instants with different strings remain a failed gate.
                    reason = classify_modtime_mismatch(
                        expected['ModTime'], row['ModTime'],
                        is_dir=expected['IsDir'])
                    raise BatchParityError(reason)
                if any(type(row[key]) is not type(expected[key])
                       or row[key] != expected[key]
                       for key in CORE_FIELDS - {'ModTime'}):
                    raise BatchParityError('BATCH_CORE_FIELD_MISMATCH')
                checked += 1
            if observed_ids != set(native_by_id):
                raise BatchParityError('BATCH_REFERENCE_INCOMPLETE')
        if checked != identity['entries_matched']:
            raise BatchParityError('BATCH_CORE_COUNT_MISMATCH')
    except BatchCheckpointError:
        raise BatchParityError('BATCH_NATIVE_CORE_METADATA_UNVERIFIED') from None
    except (BatchEquivalenceError, TypeError, ValueError, KeyError,
            AttributeError):
        raise BatchParityError('BATCH_CORE_PARITY_UNVERIFIED') from None
    return {
        'folders_matched': identity['folders_matched'],
        'entries_matched': checked,
        'files_matched': identity['files_matched'],
        'directories_matched': identity['directories_matched'],
        'identity_verified': True,
        'core_fields_verified': True,
        'timestamps_verified': True,
        'mime_types_verified': False,
        'unmodeled_fields_verified': False,
        'checkpoint_writes': 0,
        'production_commit_authorized': False,
    }
)


def _instant_parts(value: object) -> tuple[int, int] | None:
    """UTC seconds and nanoseconds; no float or microsecond truncation.

    Diagnostic-only. Never substitute this classification for the exact
    timestamp match required to authorize a batch migration.
    """
    if not valid_timestamp(value):
        return None
    try:
        raw = datetime.fromisoformat(value.replace('Z', '+00:00'))
        whole_utc = raw.replace(microsecond=0).astimezone(timezone.utc)
        delta = whole_utc - _EPOCH
    except (ValueError, OverflowError, TypeError):
        return None
    fraction = _FRACTION.search(value)
    nanos = int(fraction.group(1).ljust(9, '0')) if fraction else 0
    return delta.days * 86400 + delta.seconds, nanos


def classify_modtime_mismatch(native: object, reference: object, *,
                              is_dir: bool) -> str:
    """Return a fixed privacy-safe code; never report timestamps or IDs."""
    if type(is_dir) is not bool:
        return 'BATCH_TIMESTAMP_UNVERIFIED'
    native_parts = _instant_parts(native)
    reference_parts = _instant_parts(reference)
    if native_parts is None or reference_parts is None:
        return 'BATCH_TIMESTAMP_UNVERIFIED'
    if native_parts == reference_parts:
        return 'BATCH_TIMESTAMP_REPRESENTATION_MISMATCH'
    if native_parts[0] == reference_parts[0]:
        return 'BATCH_TIMESTAMP_PRECISION_MISMATCH'
    return ('BATCH_TIMESTAMP_DIRECTORY_VALUE_MISMATCH' if is_dir
            else 'BATCH_TIMESTAMP_FILE_VALUE_MISMATCH')


def compare_core_fields(
        batch: list[tuple[str, str]], native: object,
        references: list[tuple[str, list[dict]]]) -> dict:
    """Require the same fields on all records before approving field parity.

    This is intentionally stricter than the first two-folder identity probe.
    It compares the exact RFC3339 timestamp string, without silently
    discarding nanoseconds or offset information. If the two rclone paths
    serialize equivalent time instants differently, defer adoption until
    that difference can be explained and tested.
    """
    try:
        identity = compare_with_legacy(batch, native, references)
        converted = convert_batch(batch, native)
        if (not isinstance(references, list) or
                len(references) != len(converted)):
            raise BatchParityError('BATCH_REFERENCE_FOLDERS_MISSING')
        reference_by_folder = {}
        for folder, entries in references:
            if (not isinstance(folder, str) or folder in reference_by_folder
                    or not isinstance(entries, list)):
                raise BatchParityError('BATCH_REFERENCE_FOLDERS_INVALID')
            reference_by_folder[folder] = entries
        if set(reference_by_folder) != {name for name, _ in converted}:
            raise BatchParityError('BATCH_REFERENCE_FOLDER_SET_MISMATCH')
        checked = 0
        for folder, records in converted:
            source = reference_by_folder[folder]
            if len(source) != len(records):
                raise BatchParityError('BATCH_REFERENCE_COUNT_MISMATCH')
            native_by_id = {row['ID']: row for row in records}
            if len(native_by_id) != len(records):
                raise BatchParityError('BATCH_DUPLICATE_CONVERTED_ID')
            observed_ids = set()
            for row in source:
                if not isinstance(row, dict) or not CORE_FIELDS <= row.keys():
                    raise BatchParityError('BATCH_REFERENCE_FIELD_MISSING')
                if row['ID'] in observed_ids:
                    raise BatchParityError('BATCH_DUPLICATE_REFERENCE_ID')
                observed_ids.add(row['ID'])
                expected = native_by_id.get(row['ID'])
                if expected is None:
                    raise BatchParityError('BATCH_REFERENCE_ID_MISMATCH')
                # Report categories only, not the private values.
                # Exact timestamp differences are a separate migration
                # blocker from other metadata disagreements.
                if (not valid_timestamp(row['ModTime'])
                        or type(row['ModTime']) is not type(expected['ModTime'])
                        or row['ModTime'] != expected['ModTime']):
                    raise BatchParityError('BATCH_TIMESTAMP_MISMATCH')
                if any(type(row[key]) is not type(expected[key])
                       or row[key] != expected[key]
                       for key in CORE_FIELDS - {'ModTime'}):
                    raise BatchParityError('BATCH_CORE_FIELD_MISMATCH')
                checked += 1
            if observed_ids != set(native_by_id):
                raise BatchParityError('BATCH_REFERENCE_INCOMPLETE')
        if checked != identity['entries_matched']:
            raise BatchParityError('BATCH_CORE_COUNT_MISMATCH')
    except BatchCheckpointError:
        raise BatchParityError('BATCH_NATIVE_CORE_METADATA_UNVERIFIED') from None
    except (BatchEquivalenceError, TypeError, ValueError, KeyError,
            AttributeError):
        raise BatchParityError('BATCH_CORE_PARITY_UNVERIFIED') from None
    return {
        'folders_matched': identity['folders_matched'],
        'entries_matched': checked,
        'files_matched': identity['files_matched'],
        'directories_matched': identity['directories_matched'],
        'identity_verified': True,
        'core_fields_verified': True,
        'timestamps_verified': True,
        'mime_types_verified': False,
        'unmodeled_fields_verified': False,
        'checkpoint_writes': 0,
        'production_commit_authorized': False,
    }

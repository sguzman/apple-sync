#!/usr/bin/env python3
"""Offline identity/metadata comparison for a proposed batched Drive reader.

Compare already obtained Apple batch envelopes against rclone lsjson listings.
No network, output files, checkpoint mutation, or credential handling.
This is a diagnostic gate, NOT permission to commit batch results.
The experimental Go adapter applies rclone's name encoding first; compare the resulting names as well as stable Drive IDs.
"""
from __future__ import annotations

from drive_batch_contract import (BatchContractError, DIRECTORY_TYPES,
                                  validate_response)

FILE_TYPES = frozenset(('FILE',))
ALLOWED_TYPES = DIRECTORY_TYPES | FILE_TYPES


class BatchEquivalenceError(Exception):
    """A fixed privacy-safe code; never interpolate a remote path or ID."""


def _normalized_id(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or not value:
        raise BatchEquivalenceError('MISSING_RCLONE_ID')
    parts = value.split('#', 1)
    if not parts[0]:
        raise BatchEquivalenceError('INVALID_RCLONE_ID')
    return parts[0], parts[1] if len(parts) == 2 else ''


def compare_with_legacy(
        batch: list[tuple[str, str]],
        native_response: object,
        reference: list[tuple[str, list[dict]]]) -> dict:
    """Return anonymous summary only when each folder is exactly equivalent.

    'native_response' is the raw Apple API response. Each 'reference' listing
    is an existing, *independent*, nonrecursive rclone lsjson result for the
    matching pending folder. These should be compared on a deliberately tiny
    live sample before any checkpoint writes are even considered.
    """
    try:
        validated = validate_response(batch, native_response)
    except BatchContractError:
        raise BatchEquivalenceError('INVALID_NATIVE_BATCH_ENVELOPE') from None

    if not isinstance(reference, list) or len(reference) != len(batch):
        raise BatchEquivalenceError('REFERENCE_FOLDER_COUNT_MISMATCH')
    existing: dict[str, list[dict]] = {}
    for entry in reference:
        if (not isinstance(entry, tuple) or len(entry) != 2
                or not isinstance(entry[0], str) or entry[0] in existing
                or not isinstance(entry[1], list)):
            raise BatchEquivalenceError('INVALID_REFERENCE_FOLDER')
        existing[entry[0]] = entry[1]
    if set(existing) != {folder for folder, _ in batch}:
        raise BatchEquivalenceError('REFERENCE_FOLDER_SET_MISMATCH')

    files = directories = 0
    for folder, native_children in validated:
        reference_children = existing[folder]
        if len(native_children) != len(reference_children):
            raise BatchEquivalenceError('REFERENCE_CHILD_COUNT_MISMATCH')
        raw_by_id: dict[str, dict] = {}
        for raw in native_children:
            identifier = raw.get('drivewsid')
            kind = raw.get('type')
            if (not isinstance(identifier, str) or not identifier
                    or '#' in identifier or identifier in raw_by_id):
                raise BatchEquivalenceError('INVALID_NATIVE_CHILD_ID')
            if kind not in ALLOWED_TYPES:
                raise BatchEquivalenceError('UNSUPPORTED_NATIVE_CHILD_TYPE')
            if kind in FILE_TYPES:
                size = raw.get('size')
                if type(size) is not int or size < 0:
                    raise BatchEquivalenceError('UNVERIFIED_NATIVE_FILE_SIZE')
            raw_by_id[identifier] = raw

        seen = set()
        for row in reference_children:
            if not isinstance(row, dict):
                raise BatchEquivalenceError('INVALID_REFERENCE_ENTRY')
            label = row.get('Path')
            if not isinstance(label, str) or not label or '/' in label or '\\' in label:
                raise BatchEquivalenceError('UNSAFE_REFERENCE_NAME')
            identifier, etag = _normalized_id(row.get('ID'))
            if identifier in seen or identifier not in raw_by_id:
                raise BatchEquivalenceError('REFERENCE_IDENTITY_MISMATCH')
            seen.add(identifier)
            raw = raw_by_id[identifier]
            # The experimental Go adapter uses ToStandardName on both
            # components, just like rclone's existing List implementation.
            native_name = raw.get('name')
            native_extension = raw.get('extension', '')
            if (not isinstance(native_name, str) or not native_name
                    or not isinstance(native_extension, str)):
                raise BatchEquivalenceError('MISSING_NATIVE_NAME')
            expected_name = native_name + (
                '.' + native_extension if native_extension else '')
            if label != expected_name:
                raise BatchEquivalenceError('REFERENCE_NAME_MISMATCH')
            expected_dir = raw['type'] in DIRECTORY_TYPES
            if type(row.get('IsDir')) is not bool or row['IsDir'] != expected_dir:
                raise BatchEquivalenceError('REFERENCE_TYPE_MISMATCH')
            # Rclone joins ETags to *directory* IDs. File objects expose
            # only the bare Drive ID, even when Apple supplies an ETag.
            if expected_dir:
                if '#' not in row['ID']:
                    raise BatchEquivalenceError('REFERENCE_DIRECTORY_ETAG_MISSING')
                if 'etag' in raw and (not isinstance(raw['etag'], str)
                                      or raw['etag'] != etag):
                    raise BatchEquivalenceError('REFERENCE_ETAG_MISMATCH')
            elif '#' in row['ID']:
                raise BatchEquivalenceError('UNEXPECTED_REFERENCE_FILE_ETAG')
            if expected_dir:
                directories += 1
            else:
                if type(row.get('Size')) is not int or row['Size'] != raw['size']:
                    raise BatchEquivalenceError('REFERENCE_FILE_SIZE_MISMATCH')
                files += 1
        if seen != set(raw_by_id):
            raise BatchEquivalenceError('REFERENCE_INCOMPLETE')

    return {'folders_matched': len(validated),
            'entries_matched': files + directories,
            'files_matched': files,
            'directories_matched': directories,
            'equivalent': True,
            'checkpoint_writes': 0}

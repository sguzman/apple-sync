#!/usr/bin/env python3
"""Offline contract for a proposed batched iCloud Drive folder reader.

This module neither sends Apple requests nor mutates an inventory. It must
not be used to commit folders: an authenticated transport, metadata converter,
and live equivalence/bandwidth verification remain separate prerequisites.
Never include raw IDs, names, or paths in a raised error.
"""
from __future__ import annotations

from drive_pending_id_audit import DRIVE_ID, summarize

MAX_BATCH_FOLDERS = 8
DIRECTORY_TYPES = frozenset(('FOLDER', 'APP_CONTAINER', 'APP_LIBRARY'))


class BatchContractError(Exception):
    """Privacy-safe rejection of an unverified batched metadata response."""


def plan_batches(progress: dict, *, size: int = 4) -> list[list[tuple[str, str]]]:
    """Pair pending folder paths with cached bare Drive IDs, in checkpoint order.

    Returned private values are for in-memory transport only; never log them.
    """
    if type(size) is not int or not 1 <= size <= MAX_BATCH_FOLDERS:
        raise BatchContractError('INVALID_BATCH_SIZE')
    try:
        audit = summarize(progress)
        if not audit['all_pending_have_distinct_candidate_ids']:
            raise BatchContractError('INELIGIBLE_PENDING_CHECKPOINT')
        pending = progress['pending']
        pending_set = set(pending)
        found = {}
        for entry in progress['entries']:
            if entry.get('IsDir') is True and entry.get('Path') in pending_set:
                raw_id = entry['ID']
                if not isinstance(raw_id, str) or not DRIVE_ID.fullmatch(raw_id):
                    raise BatchContractError('INVALID_CACHED_DRIVE_ID')
                found[entry['Path']] = raw_id.split('#', 1)[0]
        ordered = [(folder, found[folder]) for folder in pending]
    except (TypeError, ValueError, KeyError, AttributeError):
        raise BatchContractError('INVALID_PENDING_CHECKPOINT') from None
    return [ordered[i:i + size] for i in range(0, len(ordered), size)]


def validate_response(batch: list[tuple[str, str]], response: object) -> list[tuple[str, list[dict]]]:
    """Match each folder result by ID and reject incomplete/ambiguous batches.

    This validates only the outer API envelope. It does not convert child
    metadata into rclone lsjson records or prove equivalence/completeness.
    """
    if not isinstance(batch, list) or not 1 <= len(batch) <= MAX_BATCH_FOLDERS:
        raise BatchContractError('INVALID_BATCH_REQUEST')
    requested = {}
    requested_folders = set()
    for entry in batch:
        if (not isinstance(entry, tuple) or len(entry) != 2 or
                not isinstance(entry[0], str) or not entry[0] or
                not isinstance(entry[1], str) or not DRIVE_ID.fullmatch(entry[1]) or
                '#' in entry[1] or entry[1] in requested or
                entry[0] in requested_folders):
            raise BatchContractError('INVALID_BATCH_REQUEST')
        requested[entry[1]] = entry[0]
        requested_folders.add(entry[0])
    if not isinstance(response, list) or len(response) != len(batch):
        raise BatchContractError('BATCH_RESPONSE_CARDINALITY_MISMATCH')
    results = {}
    for item in response:
        if not isinstance(item, dict):
            raise BatchContractError('INVALID_BATCH_ITEM')
        identifier = item.get('drivewsid')
        if (not isinstance(identifier, str) or
                identifier not in requested or identifier in results):
            raise BatchContractError('UNEXPECTED_OR_DUPLICATE_BATCH_ID')
        if item.get('status') not in (None, '', 'OK'):
            raise BatchContractError('BATCH_FOLDER_STATUS_NOT_OK')
        if item.get('type') not in DIRECTORY_TYPES:
            raise BatchContractError('BATCH_TARGET_NOT_DIRECTORY')
        children = item.get('items')
        if not isinstance(children, list):
            raise BatchContractError('MISSING_BATCH_CHILDREN')
        counts = [item[key] for key in ('directChildrenCount', 'numberOfItems')
                  if key in item]
        if not counts or any(type(count) is not int or count != len(children)
                             for count in counts):
            raise BatchContractError('BATCH_CHILD_COUNT_UNVERIFIED')
        seen_children = set()
        for child in children:
            if not isinstance(child, dict):
                raise BatchContractError('INVALID_BATCH_CHILD')
            child_id = child.get('drivewsid')
            if (not isinstance(child_id, str) or not child_id or
                    child_id in seen_children):
                raise BatchContractError('INVALID_OR_DUPLICATE_CHILD_ID')
            seen_children.add(child_id)
        results[identifier] = children
    return [(folder, results[drive_id]) for folder, drive_id in batch]

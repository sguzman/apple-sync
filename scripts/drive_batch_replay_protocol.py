#!/usr/bin/env python3
"""Pure, read-only S0/B/S1 request sequencer for future metered replay QA.

This module has NO CLI, subprocess, socket or credentials. It cannot be used
to start a live service by itself. A future worker must provide a private,
cumulative-IP-metered, archive-locked native callback and enforce timeouts.
No response data, paths, IDs, or time values are logged or persisted.
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from drive_batch_contract import BatchContractError, plan_batches
from drive_batch_checkpoint import BatchCheckpointError, convert_batch
from drive_batch_replay import ReplayError, compare_triplet

REPLAY_REQUESTS = 3
REPLAY_FOLDERS = 2


class ReplayProtocolError(Exception):
    """Fixed diagnostic codes only, never callback messages or cloud data."""


class ReplayProtocolDeferred(ReplayProtocolError):
    """Backups active or status unknown: abort without further reads."""


def run_bracketed(
        progress: dict, *,
        read_native: Callable[[list[tuple[str, str]]], object],
        backup_busy: Callable[[], bool]) -> dict:
    """Plan exactly one folder bracketing a genuine two-folder batch.

    In order: read [A], [A,B], [A]. The target A is the first eligible
    pending folder. Do not fall back to one target if fewer than two remain.
    This sequence is intentionally separate from the existing legacy parity
    experiment. A classification is observational only and never grants a
    scanner checkpoint write.
    """
    try:
        batches = plan_batches(deepcopy(progress), size=REPLAY_FOLDERS)
    except (BatchContractError, TypeError, ValueError, KeyError,
            AttributeError):
        raise ReplayProtocolError('REPLAY_INVALID_CHECKPOINT') from None
    if not batches or len(batches[0]) != REPLAY_FOLDERS:
        raise ReplayProtocolError('REPLAY_REQUIRES_TWO_ELIGIBLE_FOLDERS')
    selected = batches[0]
    single = [selected[0]]

    def require_idle() -> None:
        try:
            blocked = backup_busy()
        except Exception:
            raise ReplayProtocolDeferred('REPLAY_BACKUP_STATUS_UNKNOWN') from None
        if type(blocked) is not bool:
            raise ReplayProtocolDeferred('REPLAY_BACKUP_STATUS_UNKNOWN')
        if blocked:
            raise ReplayProtocolDeferred('REPLAY_BACKUP_ACTIVE')

    def request(subset: list[tuple[str, str]]) -> object:
        require_idle()
        try:
            response = read_native(subset.copy())
        except Exception:
            raise ReplayProtocolError('REPLAY_NATIVE_READ_FAILED') from None
        require_idle()
        # Reject untrusted/partial metadata before issuing another metered
        # request, rather than spending bandwidth on a doomed replay.
        try:
            convert_batch(subset, response)
        except (BatchCheckpointError, TypeError, ValueError, KeyError,
                AttributeError):
            raise ReplayProtocolError(
                'REPLAY_UNVERIFIED_NATIVE_RESPONSE') from None
        return response

    before = request(single)
    batched = request(selected)
    after = request(single)
    try:
        result = compare_triplet(
            selected, single_before=before, batched=batched,
            single_after=after)
    except ReplayError as exc:
        allowed = frozenset((
            'REPLAY_REQUIRES_TWO_FOLDERS',
            'REPLAY_UNVERIFIED_NATIVE_RESPONSE',
            'REPLAY_DUPLICATE_CHILD_ID',
            'REPLAY_CHILD_IDENTITY_CHANGED',
            'REPLAY_NON_TIME_METADATA_CHANGED',
            'REPLAY_NO_DIRECTORY_CHILDREN',
        ))
        category = str(exc)
        if category not in allowed:
            category = 'REPLAY_COMPARISON_FAILED'
        raise ReplayProtocolError(category) from None
    require_idle()
    return {
        **result,
        'replay_requests_completed': REPLAY_REQUESTS,
        'replay_folders_selected': REPLAY_FOLDERS,
        'measured_ip_bytes': None,
        'ip_accounting_verified': False,
        'checkpoint_writes': 0,
        'checkpoints_advanced': 0,
        'production_commit_authorized': False,
    }

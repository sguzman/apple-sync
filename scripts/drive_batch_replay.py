#!/usr/bin/env python3
"""In-memory replay of one-folder/batched/one-folder iCloud Drive responses.

NO network access, filesystem writes, subprocesses or checkpoint mutation.
All caller-provided paths, IDs and timestamps stay in memory; outputs and
errors contain fixed codes and anonymous counts only. This diagnostic is
NOT an authorization to use batched metadata for production inventory.
"""
from __future__ import annotations

from drive_batch_checkpoint import BatchCheckpointError, convert_batch

NON_TIME_FIELDS = frozenset(('Path', 'Name', 'Size', 'IsDir', 'ID'))


class ReplayError(Exception):
    """Privacy-safe, fixed diagnostic; never interpolate source metadata."""


def compare_triplet(batch: list[tuple[str, str]], *,
                    single_before: object, batched: object,
                    single_after: object) -> dict:
    """Compare child records in the *first* of exactly two requested folders.

    The native replies must be ordered in time:
      1. single-folder response (before)
      2. two-folder response (batch)
      3. the same single-folder response again (after)

    The batch envelope also contains the second folder to verify that this
    is a true multi-ID request. This function does not make those requests.

    A batch-only difference with equal bracketing singles is an OBSERVATION,
    not proof of an Apple batching bug: a transient change/reversion remains
    possible. Differing bracketing singles similarly do not prove mutation
    rather than API inconsistency. No outcome grants checkpoint-write access.
    """
    if (not isinstance(batch, list) or len(batch) != 2
            or any(not isinstance(item, tuple) or len(item) != 2
                   for item in batch)):
        raise ReplayError('REPLAY_REQUIRES_TWO_FOLDERS')
    try:
        before = convert_batch([batch[0]], single_before)[0][1]
        multi = convert_batch(batch, batched)[0][1]
        after = convert_batch([batch[0]], single_after)[0][1]
    except (BatchCheckpointError, TypeError, ValueError, KeyError,
            AttributeError):
        raise ReplayError('REPLAY_UNVERIFIED_NATIVE_RESPONSE') from None

    def by_id(records: list[dict]) -> dict[str, dict]:
        items: dict[str, dict] = {}
        for record in records:
            identifier = record['ID']
            if identifier in items:
                raise ReplayError('REPLAY_DUPLICATE_CHILD_ID')
            items[identifier] = record
        return items

    left, center, right = by_id(before), by_id(multi), by_id(after)
    if set(left) != set(center) or set(left) != set(right):
        raise ReplayError('REPLAY_CHILD_IDENTITY_CHANGED')

    stable = batch_only = single_changed = 0
    for identifier in left:
        prior, middle, later = left[identifier], center[identifier], right[identifier]
        if any(type(prior[key]) is not type(middle[key])
               or type(prior[key]) is not type(later[key])
               or prior[key] != middle[key] or prior[key] != later[key]
               for key in NON_TIME_FIELDS):
            raise ReplayError('REPLAY_NON_TIME_METADATA_CHANGED')
        if not prior['IsDir']:
            continue
        if prior['ModTime'] == middle['ModTime'] == later['ModTime']:
            stable += 1
        elif prior['ModTime'] == later['ModTime']:
            batch_only += 1
        else:
            single_changed += 1

    checked = stable + batch_only + single_changed
    if checked == 0:
        raise ReplayError('REPLAY_NO_DIRECTORY_CHILDREN')
    if batch_only and not single_changed:
        outcome = 'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED'
    elif single_changed and not batch_only:
        outcome = 'REPLAY_SINGLE_READS_DIFFER_OBSERVED'
    elif batch_only and single_changed:
        outcome = 'REPLAY_MIXED_DIFFERENCES_OBSERVED'
    else:
        outcome = 'REPLAY_ALL_DIRECTORY_TIMESTAMPS_IDENTICAL'

    return {
        'version': 1,
        'outcome': outcome,
        'directory_children_checked': checked,
        'directory_children_stable': stable,
        'directory_children_batch_only_difference': batch_only,
        'directory_children_single_read_difference': single_changed,
        'checkpoints_advanced': 0,
        'checkpoint_writes': 0,
        'production_commit_authorized': False,
    }

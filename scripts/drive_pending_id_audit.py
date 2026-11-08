#!/usr/bin/env python3
"""Offline, path-free audit of cached IDs for potential batched Drive listing.

Reads the existing private checkpoint. Makes no Apple/rclone requests and
never prints paths, names, IDs, account information or individual records.
A valid-looking ID only supports further design work; it does not establish
that an API batch is safe, supported, equivalent or bandwidth-efficient.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import re


# Rclone's iCloud Drive directory IDs are Drive IDs, optionally joined with
# an ETag after '#'. Validation here is conservative and entirely local.
DRIVE_ID = re.compile(r'^(?:FOLDER|APP_CONTAINER|APP_LIBRARY)::[^:#/\s]+::[^#/\s]+(?:#[^/\s]*)?$')


def summarize(progress: dict) -> dict:
    """Aggregate structural prerequisites without exposing private metadata."""
    pending = progress['pending']
    records = defaultdict(list)
    for entry in progress['entries']:
        if entry.get('IsDir') is True:
            records[entry['Path']].append(entry.get('ID'))

    counts = Counter()
    candidate_ids = []
    for folder in pending:
        if folder == '':
            # Only possible before the root listing has completed.
            counts['root_pending'] += 1
            continue
        matches = records.get(folder, ())
        if not matches:
            counts['missing_directory_record'] += 1
        elif len(matches) != 1:
            counts['ambiguous_directory_record'] += 1
        elif not isinstance(matches[0], str) or not matches[0].strip():
            counts['missing_id'] += 1
        elif not DRIVE_ID.fullmatch(matches[0]):
            counts['unrecognized_id'] += 1
        else:
            counts['candidate_id'] += 1
            candidate_ids.append(matches[0].split('#', 1)[0])

    duplicate_candidate_ids = sum(n - 1 for n in Counter(candidate_ids).values() if n > 1)
    return {
        'pending_folders': len(pending),
        'candidate_ids': counts['candidate_id'],
        'missing_directory_records': counts['missing_directory_record'],
        'ambiguous_directory_records': counts['ambiguous_directory_record'],
        'missing_ids': counts['missing_id'],
        'unrecognized_ids': counts['unrecognized_id'],
        'duplicate_candidate_ids': duplicate_candidate_ids,
        'root_pending': counts['root_pending'],
        'all_pending_have_distinct_candidate_ids': (
            counts['candidate_id'] == len(pending) and duplicate_candidate_ids == 0),
    }


def main() -> int:
    # Defer imports so tests of the pure audit do not need the entire scanner.
    import archive_batch as archive
    import drive_inventory as inventory

    try:
        progress = inventory.load_progress(archive.STATE)
        if progress is None:
            print('PENDING ID AUDIT: no partial checkpoint; no network call')
            return 0
        result = summarize(progress)
    except (OSError, ValueError, inventory.InventoryError):
        print('PENDING ID AUDIT: local checkpoint unavailable or invalid; no network call')
        return 2
    print('PENDING ID AUDIT: read-only; NO APPLE NETWORK; NO METADATA DISCLOSED')
    print('PENDING ID AUDIT ' + json.dumps(result, sort_keys=True))
    print('PENDING ID AUDIT: structural check only; no live API batch verified')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

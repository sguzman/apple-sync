#!/usr/bin/env python3
"""Local-only iCloud Drive checkpoint workload profiler.

Reads the existing private metadata checkpoint. Groups the original records
by their immediate parent folder, then compares ranges of completed folders.
No remote calls, rclone, network requests, files downloaded, or state writes.
Never prints cloud paths, filenames, file IDs or any individual metadata.
The byte estimate is the *local saved JSON size*, not measured IP traffic.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics

import archive_batch as archive
import drive_inventory as inventory


def count_folders(progress: dict) -> list[dict]:
    """Return per-finished-folder aggregate metadata, in checkpoint order."""
    finished = progress['finished']
    per_parent = defaultdict(lambda: {'records': 0, 'dirs': 0,
                                      'serialized_bytes': 0})
    for item in progress['entries']:
        path = item['Path']
        parent = path.rsplit('/', 1)[0] if '/' in path else ''
        values = per_parent[parent]
        values['records'] += 1
        values['dirs'] += int(item['IsDir'] is True)
        # This is saved JSON representation, NOT remote traffic.
        values['serialized_bytes'] += len(
            json.dumps(item, ensure_ascii=False,
                       separators=(',', ':')).encode('utf-8'))
    # Entries must have come from a folder the scanner finished.
    if not set(per_parent).issubset(set(finished)):
        raise inventory.InventoryError('CHECKPOINT_ENTRY_PARENT_NOT_FINISHED')
    return [dict(per_parent[folder],
                 depth=0 if not folder else folder.count('/') + 1)
            for folder in finished]


def window_summary(folders: list[dict], start: int, end: int) -> dict:
    """Private-path-free aggregate for one finished-folder index range."""
    if not (0 <= start < end <= len(folders)):
        raise ValueError('INVALID_PROFILE_RANGE')
    selected = folders[start:end]
    n = len(selected)
    records = [item['records'] for item in selected]
    total_records = sum(records)
    total_dir_entries = sum(item['dirs'] for item in selected)
    estimated_bytes = sum(item['serialized_bytes'] for item in selected)
    ordered = sorted(records)
    return {
        'start': start, 'end': end, 'folders': n,
        'records': total_records,
        'records_per_folder': round(total_records / n, 2),
        'empty_folders': sum(v == 0 for v in records),
        'median_records': round(statistics.median(ordered), 2),
        'p90_records': ordered[(9 * (n - 1)) // 10],
        'largest_folder_records': ordered[-1],
        'directory_records': total_dir_entries,
        'depth_mean': round(
            sum(item['depth'] for item in selected) / n, 2),
        'local_record_json_bytes': estimated_bytes,
        'local_json_bytes_per_folder': round(estimated_bytes / n, 1),
        'local_json_bytes_per_record': round(
            estimated_bytes / total_records, 1) if total_records else 0,
    }


def profile(progress: dict, cuts: list[int]) -> list[dict]:
    folders = count_folders(progress)
    if len(cuts) < 2 or cuts != sorted(set(cuts)):
        raise ValueError('PROFILE_CUTS_MUST_ASCEND_WITHOUT_DUPLICATES')
    if cuts[0] < 0 or cuts[-1] > len(folders):
        raise ValueError('PROFILE_CUTS_OUTSIDE_CHECKPOINT')
    return [window_summary(folders, a, b)
            for a, b in zip(cuts, cuts[1:])]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--cuts', nargs='+', type=int, required=True,
        help='increasing completed-folder counts from previous summaries, '
             'e.g. --cuts 1236 1409 1599')
    args = parser.parse_args()
    progress = inventory.load_progress(archive.STATE)
    if progress is None:
        print('LOCAL PROFILE: no partial checkpoint; inspect complete snapshot',
              flush=True)
        return 0
    try:
        groups = profile(progress, args.cuts)
    except ValueError as exc:
        parser.error(str(exc))
    print('LOCAL CHECKPOINT PROFILE: '
          f'{len(progress["finished"])} completed folders; '
          f'{len(progress["pending"])} pending; '
          f'{len(progress["entries"])} metadata records', flush=True)
    print('LOCAL CHECKPOINT PROFILE: read-only; NO APPLE NETWORK; '
          'local JSON bytes are not IP traffic', flush=True)
    for group in groups:
        print('LOCAL PROFILE ' + json.dumps(group, sort_keys=True),
              flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

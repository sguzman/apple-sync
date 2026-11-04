#!/usr/bin/env python3
"""Report private iCloud Drive inventory coverage without remote calls or filenames.

Works with a partial folder checkpoint or a completed inventory. Does not
read file contents, traverse the local archive, modify state, or access rclone.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import drive_inventory as inventory
import diagnose_drive_downloads as diagnosis
import archive_batch as archive

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB
MIN_SMALL = 64 * KIB
MAX_SMALL = 8 * MIB


def summarize(entries: list[dict]) -> dict[str, int]:
    """Aggregate metadata-only categories; never expose individual paths."""
    # Both inventory.load and inventory.load_progress validate private
    # records before this function receives them.
    if entries:
        inventory.inspect_entries(entries)
    counts = Counter(item['Path'] for item in entries)
    totals = Counter()
    for item in entries:
        path = item['Path']
        if item.get('IsDir', False):
            totals['directories'] += 1
            continue
        totals['files'] += 1
        native = diagnosis.is_iwork(path)
        ambiguous = counts[path] != 1
        size = item.get('Size')
        if native:
            totals['apple_native'] += 1
        if ambiguous:
            totals['ambiguous'] += 1
        if size is None or size == -1:
            totals['unknown_size'] += 1
        else:
            totals['known_bytes'] += size
            totals['known_size_files'] += 1
            if size == 0:
                totals['empty'] += 1
            elif size < MIN_SMALL:
                totals['under_64k'] += 1
            elif size <= MAX_SMALL:
                totals['64k_to_8m'] += 1
            elif size <= 64 * MIB:
                totals['8m_to_64m'] += 1
            elif size <= GIB:
                totals['64m_to_1g'] += 1
            else:
                totals['over_1g'] += 1
        if (not native and not ambiguous and type(size) is int
                and MIN_SMALL <= size <= MAX_SMALL):
            totals['eligible_files'] += 1
            totals['eligible_bytes'] += size
    return dict(totals)


def report(*, state_root: Path | None = None) -> str:
    """No filesystem reads outside the manifest and its expected parent."""
    if state_root is None:
        state_root = archive.STATE
    progress = inventory.load_progress(state_root)
    if progress is not None:
        phase = 'PARTIAL_CHECKPOINT'
        entries = progress['entries']
        completed = len(progress['finished'])
        pending = len(progress['pending'])
    else:
        try:
            entries, _ = inventory.load(state_root, max_age=None)
        except inventory.InventoryError as exc:
            if str(exc) == 'INVENTORY_NOT_CREATED':
                return ('Drive coverage: NO_INVENTORY; no network calls, '
                        'no archive writes')
            raise
        phase = 'COMPLETE_SNAPSHOT'
        completed = pending = None
    counts = summarize(entries)
    keys = (
        ('files', 'file records'),
        ('directories', 'directory records'),
        ('known_size_files', 'known-size files'),
        ('unknown_size', 'unknown-size files'),
        ('empty', 'empty files'),
        ('under_64k', 'files under 64 KiB'),
        ('64k_to_8m', 'files 64 KiB–8 MiB'),
        ('8m_to_64m', 'files above 8 MiB–64 MiB'),
        ('64m_to_1g', 'files above 64 MiB–1 GiB'),
        ('over_1g', 'files above 1 GiB'),
        ('apple_native', 'Apple-native records (overlapping category)'),
        ('ambiguous', 'repeated-path file records (overlapping category)'),
        ('eligible_files', 'potential 64 KiB–8 MiB ordinary unique files'),
    )
    lines = [f'Drive coverage: {phase} (local metadata only)',
             f'Discovered records: {len(entries)}']
    if completed is not None:
        lines.append(f'Folder checkpoint: {completed} complete; '
                     f'{pending} pending; counts are PARTIAL, not totals')
    lines.extend(f'{label}: {counts.get(key, 0)}' for key, label in keys)
    lines.append(f'Known-size bytes: {counts.get("known_bytes", 0)}')
    lines.append(f'Potential small-file bytes: {counts.get("eligible_bytes", 0)}')
    lines.append('No Apple requests, media downloads or archive writes')
    return '\n'.join(lines)


def main() -> int:
    try:
        print(report(), flush=True)
        return 0
    except (inventory.InventoryError, OSError, ValueError):
        print('Drive coverage: invalid or inaccessible private metadata; '
              'no network calls attempted', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

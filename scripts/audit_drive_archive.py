#!/usr/bin/env python3
"""Private, read-only inventory-to-archive audit (no Apple API requests).

An existing destination file is NOT automatically a verified backup. Without
a durable independent SHA-256 receipt, this audit can only confirm local file
type/size; it must not claim remote content integrity or full coverage.
"""
from __future__ import annotations

from collections import Counter
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_batch as archive
import drive_inventory as inventory
import drive_receipts as receipts
import diagnose_drive_downloads as diagnosis


class AuditError(Exception):
    """No private names in user-facing failure output."""


def inventory_records(state_root: Path) -> tuple[list[dict], bool, int, int]:
    """Prefer a partial checkpoint when present; otherwise use final snapshot."""
    progress = inventory.load_progress(state_root)
    if progress is not None:
        return (progress['entries'], False,
                len(progress['finished']), len(progress['pending']))
    try:
        entries, _ = inventory.load(state_root)
    except inventory.InventoryError as exc:
        if str(exc) == 'INVENTORY_NOT_CREATED':
            return [], False, 0, 0
        raise
    return entries, True, 0, 0


def check_local(destination: Path, path: str) -> tuple[str, int | None]:
    """Inspect no symlink targets, including intermediate archive directories."""
    rel = archive.safe_path(path)
    if rel is None:
        return 'unsafe_path', None
    # Parent and leaf checks are intentionally conservative; never follow a
    # symlink to count a file as backed up. No files are opened for writing.
    try:
        target = archive.validate_target(destination, rel)
        mode = target.lstat().st_mode
        if not stat.S_ISREG(mode):
            return 'not_regular', None
        return 'regular', target.stat(follow_symlinks=False).st_size
    except FileNotFoundError:
        return 'missing', None
    except (RuntimeError, OSError, ValueError):
        return 'unsafe_or_inaccessible', None


def summarize(entries: list[dict], destination: Path,
              *, state_root: Path | None = None) -> dict:
    """Aggregate the discovered records, not the unknowable whole Drive."""
    if entries:
        inventory.inspect_entries(entries)
    same_paths = Counter(e['Path'] for e in entries)
    results = Counter()
    for item in entries:
        if item.get('IsDir', False):
            results['directories'] += 1
            continue
        results['discovered_files'] += 1
        path = item['Path']
        if same_paths[path] != 1:
            results['ambiguous'] += 1
            continue
        outcome, local_size = check_local(destination, path)
        if outcome != 'regular':
            results[outcome] += 1
            continue
        results['present_regular'] += 1
        size = item.get('Size')
        if type(size) is not int or size < 0:
            results['unknown_size_present'] += 1
        elif local_size != size:
            results['size_mismatch'] += 1
        else:
            results['bytes_size_matching'] += local_size
            receipt_result = ('missing' if state_root is None
                              else receipts.check_receipt(state_root, item,
                                                          destination / path))
            if receipt_result == 'verified':
                results['receipt_verified'] += 1
                results['receipt_verified_bytes'] += local_size
            else:
                results['size_matching_unverified'] += 1
                if receipt_result == 'invalid':
                    results['invalid_receipt'] += 1
                elif receipt_result == 'hash_mismatch':
                    results['receipt_hash_mismatch'] += 1
                if diagnosis.is_iwork(path):
                    results['native_size_matching_unverified'] += 1
    return dict(results)


def report(*, state_root: Path | None = None,
           destination: Path | None = None) -> str:
    if state_root is None:
        state_root = archive.STATE
    if destination is None:
        destination = archive.SOURCES['drive'][1]
    if destination.is_symlink() or not destination.is_dir():
        raise AuditError('INVALID_ARCHIVE_ROOT')
    entries, complete, finished, pending = inventory_records(state_root)
    if not entries:
        return ('Drive archive audit: NO_INVENTORY; '
                'nothing classified; no remote calls or writes')
    summary = summarize(entries, destination, state_root=state_root)
    rows = [
        'Drive archive audit: ' +
        ('COMPLETE_SOURCE_SNAPSHOT' if complete else 'PARTIAL_SOURCE_CHECKPOINT'),
        f'Known inventory records: {len(entries)}',
    ]
    if not complete:
        rows.append(f'Folders checkpointed: {finished}; pending: {pending}; '
                    'archive counts are PARTIAL')
    for key, label in (
        ('discovered_files', 'Discovered file records'),
        ('directories', 'Directory records'),
        ('ambiguous', 'Ambiguous file records (excluded)'),
        ('missing', 'Destination files missing'),
        ('present_regular', 'Destination regular files present'),
        ('receipt_verified', 'Files with valid dual-whole-read receipt and matching local SHA-256'),
        ('size_matching_unverified', 'Size-matching files without a valid verification receipt'),
        ('invalid_receipt', 'Invalid/inaccessible receipt records'),
        ('receipt_hash_mismatch', 'Receipt SHA-256 mismatches'),
        ('size_mismatch', 'Existing files with incorrect size'),
        ('unknown_size_present', 'Existing files with unknown cloud size'),
        ('not_regular', 'Existing nonregular destination objects'),
        ('unsafe_path', 'Unsafe source paths'),
        ('unsafe_or_inaccessible', 'Symlink/inaccessible destination paths'),
        ('native_size_matching_unverified', 'Size-matching native documents (NOT content verified)'),
    ):
        rows.append(f'{label}: {summary.get(key, 0)}')
    rows.append(f'Bytes of size-matching files: '
                f'{summary.get("bytes_size_matching", 0)}')
    rows.append(f'Bytes verified by receipt and current local SHA-256: '
                f'{summary.get("receipt_verified_bytes", 0)}')
    rows.append('Local receipts attest two matching whole-file Apple-visible reads, '
                'not provider-signed authenticity.')
    rows.append('Size alone is not a cryptographic verification receipt.')
    rows.append('No Apple API calls, remote downloads or archive writes')
    return '\n'.join(rows)


def main() -> int:
    try:
        # Fail closed on unmounted paths; otherwise a disconnected drive
        # would be mistakenly reported as an empty archive.
        archive.safe_directory(archive.SOURCES['drive'][1])
        print(report(), flush=True)
    except (AuditError, inventory.InventoryError, RuntimeError,
            OSError, ValueError):
        print('Drive archive audit: invalid or inaccessible local state; '
              'no remote calls attempted', file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

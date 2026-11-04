#!/usr/bin/env python3
"""Opt-in iCloud Drive archival batch using ONLY a complete private inventory.

Never rescans the Drive root per batch. Safely downloads two independent
whole-file copies and requires exact agreement plus live parent-folder metadata.
Files above 64 MiB, unknown/native/ambiguous files remain excluded. The
separate empty-file profile requires a successful zero-byte rclone read.
Existing unrestricted archive_batch.py drive --execute remains blocked.
"""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
from pathlib import Path, PurePosixPath
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_batch as archive
import diagnose_drive_downloads as diagnosis
import drive_inventory as inventory
import pilot_drive_archive as pilot

MAX_FILE_SIZE = pilot.MAX_PILOT_BYTES
MIB = 1024 * 1024
MAX_LARGE_FILE_SIZE = 64 * MIB
# Whole-file verification does not depend on a 64-KiB lower bound.
# Exclude true zero-byte files until explicit empty-object semantics are tested.
MIN_FILE_SIZE = 1
MAX_FILES = 2
MAX_ATTEMPTS = 3
MAX_SOURCE_BYTES = 24 * MIB
MAX_LARGE_SOURCE_BYTES = 2 * MAX_LARGE_FILE_SIZE
RUN_SECONDS = 12 * 60
MAX_CACHE_AGE = 30 * 24 * 60 * 60


def candidates(entries: list[dict], destination: Path, *,
               min_file_size: int = MIN_FILE_SIZE,
               max_file_size: int = MAX_FILE_SIZE) -> tuple[list[dict], dict]:
    """Select unambiguous ordinary files without touching remote network."""
    counts = Counter(entry['Path'] for entry in entries
                     if isinstance(entry, dict) and 'Path' in entry)
    available = []
    stats = dict(ambiguous=0, excluded=0, existing=0)
    for entry in entries:
        path = entry['Path']
        if entry.get('IsDir', False) is True:
            continue
        if counts[path] > 1:
            stats['ambiguous'] += 1
            continue
        size = entry.get('Size')
        if (type(size) is not int or size < min_file_size
                or size > max_file_size
                or diagnosis.is_iwork(path)
                or not diagnosis.valid_path(path)):
            stats['excluded'] += 1
            continue
        rel = archive.safe_path(path)
        if rel is None:
            stats['excluded'] += 1
            continue
        target = archive.validate_target(destination, rel)
        if target.exists():
            stats['existing'] += 1
            continue
        available.append(entry)
    available.sort(key=lambda e: (e['Size'], e['Path']))
    return available, stats


def folder_parts(entry: dict) -> tuple[str, dict, str]:
    """Use one shallow listing to recheck a cached file's exact identity."""
    relative = PurePosixPath(entry['Path'])
    folder = relative.parent.as_posix()
    if folder == '.':
        folder = ''
    return ('icloud:' + folder, {**entry, 'Path': relative.name}, folder)


def safe_recheck(_scope: str, deadline: float, folder: str) -> list[dict]:
    remaining = deadline - time.monotonic()
    if remaining < 15:
        raise pilot.PilotError('TIME_BUDGET')
    return inventory.list_one_folder(folder, timeout=min(60, remaining - 5))


def run(*, execute: bool = False, wait_idle: bool = False,
        large: bool = False, empty: bool = False) -> int:
    if large and empty:
        print('Drive cached batch BLOCKED: conflicting profiles', flush=True)
        return 2
    # Critically: no calls to rclone, no Apple listing, no archival writes
    # until a fully validated inventory snapshot has been loaded.
    try:
        entries, inv_stats = inventory.load(archive.STATE, max_age=MAX_CACHE_AGE)
    except inventory.InventoryError as exc:
        if str(exc) in ('INVENTORY_NOT_CREATED', 'INVENTORY_TOO_OLD'):
            print('Drive cached batch BLOCKED: complete and recent inventory '
                  'required; partial checkpoints are not sufficient', flush=True)
            return 0 if not execute else 2
        print('Drive cached batch BLOCKED: inventory validation failed',
              flush=True)
        return 2

    destination = archive.SOURCES['drive'][1]
    if execute:
        archive.safe_directory(destination)
    if empty:
        min_bytes = max_bytes = 0
        max_files = 1
        max_attempts = 2
        max_source_bytes = 0
        profile = 'empty'
    else:
        min_bytes = MAX_FILE_SIZE + 1 if large else MIN_FILE_SIZE
        max_bytes = MAX_LARGE_FILE_SIZE if large else MAX_FILE_SIZE
        max_files = 1 if large else MAX_FILES
        max_attempts = 2 if large else MAX_ATTEMPTS
        max_source_bytes = MAX_LARGE_SOURCE_BYTES if large else MAX_SOURCE_BYTES
        profile = 'large' if large else 'small'
    files, stats = candidates(entries, destination,
                              min_file_size=min_bytes, max_file_size=max_bytes)
    print(f'Drive cached batch ({profile}): {inv_stats["count"]} cached records; '
          f'{len(files)} eligible missing ordinary files; '
          f'{stats["ambiguous"]} ambiguous records excluded; '
          f'{stats["excluded"]} ineligible records; '
          f'{stats["existing"]} already archived; no remote root listing',
          flush=True)
    if not execute:
        print('Drive cached batch DRY RUN: no remote calls or archive writes',
              flush=True)
        return 0

    if wait_idle and not diagnosis.wait_for_idle():
        return 0
    if diagnosis.backup_busy():
        print('Drive cached batch DEFER: another backup busy or unknown',
              flush=True)
        return 0
    if archive.STATE.is_symlink():
        print('Drive cached batch BLOCKED: unsafe state directory', flush=True)
        return 2
    lockpath = archive.ROOT / '.apple-sync.lock'
    if lockpath.is_symlink():
        print('Drive cached batch BLOCKED: unsafe lock path', flush=True)
        return 2
    archive.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + RUN_SECONDS
    archived = attempted = requested_budget = 0
    with lockpath.open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Drive cached batch DEFER: another Apple archive owns lock',
                  flush=True)
            return 0
        for entry in files:
            if archived >= max_files or attempted >= max_attempts:
                break
            size = entry['Size']
            if requested_budget + 2 * size > max_source_bytes:
                continue
            if deadline - time.monotonic() < 30 or diagnosis.backup_busy():
                print('Drive cached batch DEFER: busy or insufficient time; '
                      'completed files retained', flush=True)
                break
            scope, relative, folder = folder_parts(entry)
            # Count both full-copy source requests even when one fails.
            attempted += 1
            requested_budget += 2 * size
            try:
                # The default 8-MiB whole-file cap stays unchanged for
                # ordinary batches. Large mode explicitly extends the
                # bound to 64 MiB while retaining the same two-copy checks.
                whole_reader = None
                if large:
                    def whole_reader(remote, target, remaining):
                        return pilot.verification.whole_file(
                            remote, target, remaining,
                            max_file_bytes=MAX_LARGE_FILE_SIZE)
                result = pilot.publish_verified(
                    scope, relative, destination, archive.STATE, deadline,
                    whole_reader=whole_reader,
                    list_reader=lambda _scope, left, folder=folder:
                        safe_recheck(_scope, left, folder),
                    record_receipt=True)
            except (pilot.PilotError, pilot.receipts.ReceiptError,
                    pilot.verification.VerificationError,
                    pilot.verification.Deferred, inventory.InventoryError,
                    RuntimeError, OSError, ValueError):
                result = 'DEFER_VERIFICATION_ERROR'
            if result == 'PUBLISHED_VERIFIED':
                archived += 1
            print(f'Drive cached batch attempt {attempted}: {result}',
                  flush=True)
            if result == 'DEFER_BUSY':
                break
    print(f'Drive cached batch: {archived} verified files published; '
          f'{attempted} attempted; source-read budget reserved '
          f'{requested_budget} bytes; full Drive timer remains DISABLED',
          flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true',
                        help='archive up to two small verified files, or one 8–64 MiB file with --large')
    parser.add_argument('--wait-idle', action='store_true',
                        help='wait for other backups to be idle')
    parser.add_argument('--large', action='store_true',
                        help='separately opt in to one 8–64 MiB file, '
                        'two whole-file verification reads, 128-MiB limit')
    parser.add_argument('--empty', action='store_true',
                        help='separately opt in to one verified zero-byte file')
    args = parser.parse_args()
    if args.empty and args.large:
        parser.error('--empty and --large are mutually exclusive')
    if args.wait_idle and not args.execute:
        parser.error('--wait-idle requires --execute')
    return run(execute=args.execute, wait_idle=args.wait_idle,
               large=args.large, empty=args.empty)


if __name__ == '__main__':
    raise SystemExit(main())

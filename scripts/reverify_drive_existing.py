#!/usr/bin/env python3
"""Opt-in proof for already archived Drive files without modifying their bytes.

Uses a *complete* recent cached Drive inventory, one shallow parent-folder
metadata recheck, and two independently successful whole remote reads.
Only the private receipt may be added, after rehashing the unchanged local
file and matching both remote copies. No root enumeration or archive writes.
"""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
from pathlib import Path
import stat
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_batch as archive
import diagnose_drive_downloads as diagnosis
import drive_inventory as inventory
import drive_receipts as receipts
import drive_cached_batch as cached
import pilot_drive_archive as pilot

MAX_FILES = 1
MAX_SOURCE_BYTES = 2 * pilot.MAX_PILOT_BYTES
RUN_SECONDS = 12 * 60
MAX_CACHE_AGE = 30 * 24 * 60 * 60


def candidates(entries: list[dict], destination: Path,
               state_root: Path) -> tuple[list[dict], dict[str, int]]:
    """Find existing ordinary archive files missing valid receipts."""
    counts = Counter(e['Path'] for e in entries)
    stats = Counter()
    selected = []
    for entry in entries:
        if entry.get('IsDir') is not False:
            continue
        path = entry['Path']
        size = entry.get('Size')
        if (counts[path] != 1
                or type(size) is not int or not 0 <= size <= pilot.MAX_PILOT_BYTES
                or diagnosis.is_iwork(path)
                or not diagnosis.valid_path(path)):
            stats['excluded'] += 1
            continue
        relative = archive.safe_path(path)
        if relative is None:
            stats['excluded'] += 1
            continue
        try:
            target = archive.validate_target(destination, relative)
            meta = target.lstat()
            if not stat.S_ISREG(meta.st_mode):
                stats['unsafe'] += 1
                continue
            if meta.st_size != size:
                stats['size_mismatch'] += 1
                continue
        except FileNotFoundError:
            stats['missing'] += 1
            continue
        except (OSError, RuntimeError, ValueError):
            stats['unsafe'] += 1
            continue
        receipt_status = receipts.check_receipt(state_root, entry, target)
        if receipt_status == 'missing':
            selected.append(entry)
        else:
            stats['with_receipt_or_invalid'] += 1
    selected.sort(key=lambda e: (e['Size'], e['Path']))
    return selected, dict(stats)


def verify_one(entry: dict, destination: Path, state_root: Path,
               deadline: float, *, whole_reader=None,
               list_reader=None) -> str:
    """Never modify an existing destination file; add proof if all checks pass."""
    if whole_reader is None:
        whole_reader = pilot.verification.whole_file
    if list_reader is None:
        list_reader = cached.safe_recheck
    path = entry['Path']
    size = entry.get('Size')
    if (entry.get('IsDir') is not False or type(size) is not int
            or not 0 <= size <= pilot.MAX_PILOT_BYTES
            or archive.safe_path(path) is None or diagnosis.is_iwork(path)):
        raise receipts.ReceiptError('INVALID_EXISTING_ENTRY')
    if diagnosis.backup_busy():
        return 'DEFER_BUSY'
    rel = archive.safe_path(path)
    target = archive.validate_target(destination, rel)
    try:
        before = target.lstat()
    except FileNotFoundError:
        return 'DEFER_LOCAL_MISSING'
    if not stat.S_ISREG(before.st_mode) or before.st_size != size:
        return 'DEFER_LOCAL_SIZE_OR_TYPE'
    if receipts.check_receipt(state_root, entry, target) != 'missing':
        return 'SKIP_EXISTING_RECEIPT'
    local_sha = receipts._digest_file(target)
    scope, relative, folder = cached.folder_parts(entry)
    remote = scope + ('' if scope.endswith(':') else '/') + relative['Path']
    if deadline - time.monotonic() < 30:
        return 'DEFER_TIME'
    with tempfile.TemporaryDirectory(prefix='.drive-reverify-', dir=state_root) as tmp:
        working = Path(tmp)
        remote_size1, remote_sha1 = whole_reader(
            remote, working / 'first', deadline)
        if diagnosis.backup_busy():
            return 'DEFER_BUSY'
        remote_size2, remote_sha2 = whole_reader(
            remote, working / 'second', deadline)
        if remote_size1 != size or remote_size2 != size:
            return 'DEFER_REMOTE_SIZE'
        if remote_sha1 != remote_sha2 or local_sha != remote_sha1:
            return 'DEFER_CONTENT_MISMATCH'
        if diagnosis.backup_busy():
            return 'DEFER_BUSY'
        if not pilot.still_same(
                relative, list_reader(scope, deadline, folder)):
            return 'DEFER_SOURCE_CHANGED'
        # The local object must remain the SAME inode, type, size, and
        # bytes after remote reads. A concurrent mutation never gets proof.
        final_target = archive.validate_target(destination, rel)
        after = final_target.lstat()
        if (not stat.S_ISREG(after.st_mode)
                or after.st_size != size
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or receipts._digest_file(final_target) != local_sha):
            return 'DEFER_LOCAL_CHANGED'
        if diagnosis.backup_busy():
            return 'DEFER_BUSY'
        # The receipt is created only now. The archived file is never
        # opened for writing, renamed, removed or replaced.
        receipts.create_receipt(state_root, entry, local_sha)
        return 'REVERIFIED_RECEIPTED'


def run(*, execute: bool = False, wait_idle: bool = False) -> int:
    try:
        entries, stats = inventory.load(archive.STATE, max_age=MAX_CACHE_AGE)
    except inventory.InventoryError as exc:
        if str(exc) in ('INVENTORY_NOT_CREATED', 'INVENTORY_TOO_OLD'):
            print('Drive reverify BLOCKED: complete recent inventory required; '
                  'partial checkpoint insufficient', flush=True)
            return 2 if execute else 0
        print('Drive reverify BLOCKED: unsafe or invalid inventory', flush=True)
        return 2
    destination = archive.SOURCES['drive'][1]
    if execute:
        archive.safe_directory(destination)
    options, summary = candidates(entries, destination, archive.STATE)
    print(f'Drive reverify: {stats["count"]} cached records; '
          f'{len(options)} existing files needing first receipt; '
          f'{summary.get("size_mismatch", 0)} size mismatches; '
          f'{summary.get("with_receipt_or_invalid", 0)} already have '
          'receipt or require manual receipt review', flush=True)
    if not execute:
        print('Drive reverify DRY RUN: local-only status; '
              'no remote calls or file/receipt writes', flush=True)
        return 0
    if wait_idle and not diagnosis.wait_for_idle():
        return 0
    if diagnosis.backup_busy():
        print('Drive reverify DEFER: backups busy or unknown', flush=True)
        return 0
    state_root = archive.STATE
    if state_root.is_symlink() or not state_root.is_dir():
        print('Drive reverify BLOCKED: unsafe state directory', flush=True)
        return 2
    lockpath = archive.ROOT / '.apple-sync.lock'
    if lockpath.is_symlink():
        print('Drive reverify BLOCKED: unsafe archive lock', flush=True)
        return 2
    with lockpath.open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Drive reverify DEFER: another Apple process owns the lock',
                  flush=True)
            return 0
        deadline = time.monotonic() + RUN_SECONDS
        for entry in options[:MAX_FILES]:
            if diagnosis.backup_busy() or deadline - time.monotonic() < 30:
                print('Drive reverify DEFER: busy or insufficient time',
                      flush=True)
                break
            try:
                result = verify_one(entry, destination, state_root, deadline)
            except (receipts.ReceiptError, pilot.PilotError,
                    pilot.verification.VerificationError,
                    pilot.verification.Deferred, inventory.InventoryError,
                    RuntimeError, OSError, ValueError):
                result = 'DEFER_VERIFICATION_ERROR'
            print(f'Drive reverify: {result}; '
                  'existing archive content unchanged', flush=True)
            break
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true',
                        help='explicitly verify at most one existing file')
    parser.add_argument('--wait-idle', action='store_true',
                        help='defer while Photos/Google backups are active')
    args = parser.parse_args()
    if args.wait_idle and not args.execute:
        parser.error('--wait-idle requires --execute')
    return run(execute=args.execute, wait_idle=args.wait_idle)


if __name__ == '__main__':
    raise SystemExit(main())

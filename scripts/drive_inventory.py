#!/usr/bin/env python3
"""Private, explicit iCloud Drive metadata snapshot; no file downloads.

An ordinary backup batch must load the saved inventory, NEVER enumerate the
entire iCloud Drive before downloading. Drive's own cache/change-token behavior
is not assumed to match the iCloud Photos backend. Refresh is opt-in only.
"""
from __future__ import annotations

import argparse
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_batch as archive
import diagnose_drive_downloads as diagnosis

SOURCE = 'icloud:'
NAME = 'drive-file-inventory.json'
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
LIST_SECONDS = 25 * 60
PROGRESS_NAME = 'drive-inventory-progress.json'
WALK_SECONDS = 12 * 60
FOLDER_SECONDS = 120
MAX_FOLDER_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_TOTAL_RECORDS = 250000
MAX_PENDING_FOLDERS = 100000


class InventoryError(Exception):
    """A sanitized error that must not expose private Drive filenames."""


class InventoryDeferred(Exception):
    """Do not contend with an active cloud backup."""


def inventory_path(state_root: Path) -> Path:
    return state_root / NAME


def inspect_entries(entries: object) -> dict:
    """Preserve unknown-size records and directories; reject unsafe metadata."""
    if not isinstance(entries, list) or not entries:
        raise InventoryError('EMPTY_OR_INVALID_LISTING')
    counts = Counter()
    identities = set()
    known_bytes = known = unknown = dirs = 0
    for item in entries:
        if not isinstance(item, dict) or not diagnosis.valid_path(item.get('Path')):
            raise InventoryError('INVALID_OR_UNSAFE_RECORD')
        path = item['Path']
        if archive.safe_path(path) is None:
            raise InventoryError('INVALID_OR_UNSAFE_RECORD')
        is_dir = item.get('IsDir', False)
        if type(is_dir) is not bool:
            raise InventoryError('INVALID_DIRECTORY_FLAG')
        size = item.get('Size')
        if is_dir:
            dirs += 1
        elif type(size) is int and size >= 0:
            known += 1
            known_bytes += size
        elif size is None or (type(size) is int and size == -1):
            # Apple-native documents can have an unknown/virtual size.
            # Keep metadata but never approve these records for a download.
            unknown += 1
        else:
            raise InventoryError('INVALID_FILE_SIZE')
        counts[path] += 1
        identities.add((path, str(item.get('ID')) if item.get('ID') is not None
                        else None, is_dir))
    collision_paths = sum(count > 1 for count in counts.values())
    collision_records = sum(count for count in counts.values() if count > 1)
    return dict(count=len(entries), known_files=known, unknown_files=unknown,
                directories=dirs, known_bytes=known_bytes,
                collision_paths=collision_paths,
                collision_records=collision_records,
                identities=identities)


def public_stats(stats: dict) -> dict:
    return {key: value for key, value in stats.items() if key != 'identities'}


def load(state_root: Path, *, now: float | None = None,
         max_age: float | None = None) -> tuple[list[dict], dict]:
    path = inventory_path(state_root)
    if state_root.is_symlink() or path.is_symlink():
        raise InventoryError('UNSAFE_INVENTORY_PATH')
    if not path.is_file():
        raise InventoryError('INVENTORY_NOT_CREATED')
    if path.stat().st_mode & 0o077:
        raise InventoryError('INVENTORY_PERMISSIONS_NOT_PRIVATE')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise InventoryError('UNREADABLE_INVENTORY') from exc
    if not isinstance(data, dict) or data.get('version') != 1 or data.get('source') != SOURCE:
        raise InventoryError('INVENTORY_VERSION_OR_REMOTE_MISMATCH')
    created = data.get('created')
    current = time.time() if now is None else now
    if type(created) not in (int, float) or created > current + 60:
        raise InventoryError('INVALID_INVENTORY_TIME')
    if max_age is not None and current - created > max_age:
        raise InventoryError('INVENTORY_TOO_OLD')
    entries = data.get('entries')
    stats = inspect_entries(entries)
    if data.get('stats') != public_stats(stats):
        raise InventoryError('INVENTORY_STATS_MISMATCH')
    return entries, stats


def save(state_root: Path, entries: list[dict], *, now: float | None = None) -> dict:
    stats = inspect_entries(entries)
    path = inventory_path(state_root)
    if state_root.is_symlink() or path.is_symlink():
        raise InventoryError('UNSAFE_INVENTORY_DESTINATION')
    if path.exists():
        earlier, previous_stats = load(state_root, now=now)
        missing = previous_stats['identities'] - stats['identities']
        if missing:
            # A deletion/rename or incomplete pagination needs operator review.
            # We never replace a complete known snapshot with a silently
            # smaller, potentially partial result.
            raise InventoryError('PREVIOUSLY_KNOWN_RECORDS_MISSING')
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.drive-inventory-', dir=state_root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump({'version': 1, 'source': SOURCE,
                       'created': time.time() if now is None else now,
                       'stats': public_stats(stats), 'entries': entries},
                      output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return stats


def fetch_listing_to_file(state_root: Path, *,
                          timeout: float = LIST_SECONDS) -> list[dict]:
    """Stream bounded metadata to a private temp file, avoiding huge stdout RAM."""
    fd, temp = tempfile.mkstemp(prefix='.drive-listing-', dir=state_root)
    cmd = ['rclone', 'lsjson', SOURCE, '--recursive',
           '--checkers', '1', '--tpslimit', '2', '--log-level', 'ERROR']
    process = None
    deadline = time.monotonic() + timeout
    count = 0
    try:
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise InventoryError('RCLONE_START_FAILED') from exc
        with os.fdopen(fd, 'wb') as output, selectors.DefaultSelector() as selector:
            fd = -1
            selector.register(process.stdout, selectors.EVENT_READ)
            next_health_check = time.monotonic()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InventoryError('LISTING_TIMEOUT')
                if time.monotonic() >= next_health_check:
                    if diagnosis.backup_busy():
                        raise InventoryDeferred()
                    next_health_check = time.monotonic() + 15
                events = selector.select(min(5, remaining))
                if not events:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                count += len(chunk)
                if count > MAX_OUTPUT_BYTES:
                    raise InventoryError('LISTING_TOO_LARGE')
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        try:
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise InventoryError('LISTING_TIMEOUT') from exc
        if code != 0:
            raise InventoryError('LISTING_FAILED')
        with open(temp, encoding='utf-8') as source:
            try:
                entries = json.load(source)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise InventoryError('INVALID_LISTING_JSON') from exc
        return entries
    finally:
        if fd >= 0:
            os.close(fd)
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
        Path(temp).unlink(missing_ok=True)


def progress_path(state_root: Path) -> Path:
    return state_root / PROGRESS_NAME


def safe_folder(folder: object) -> bool:
    return folder == '' or diagnosis.valid_path(folder)


def load_progress(state_root: Path) -> dict | None:
    path = progress_path(state_root)
    if state_root.is_symlink() or path.is_symlink():
        raise InventoryError('UNSAFE_PROGRESS_PATH')
    if not path.exists():
        return None
    if path.stat().st_mode & 0o077:
        raise InventoryError('PROGRESS_PERMISSIONS_NOT_PRIVATE')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise InventoryError('INVALID_SAVED_PROGRESS') from exc
    if not isinstance(data, dict) or data.get('version') != 1 or data.get('source') != SOURCE:
        raise InventoryError('INVALID_SAVED_PROGRESS')
    pending, finished, entries = (data.get('pending'), data.get('finished'),
                                  data.get('entries'))
    if not (isinstance(pending, list) and isinstance(finished, list)
            and isinstance(entries, list)
            and len(pending) <= MAX_PENDING_FOLDERS
            and len(finished) <= MAX_PENDING_FOLDERS
            and len(entries) <= MAX_TOTAL_RECORDS
            and all(safe_folder(v) for v in pending + finished)
            and len(set(pending + finished)) == len(pending + finished)):
        raise InventoryError('INVALID_SAVED_PROGRESS')
    if entries:
        inspect_entries(entries)
    return data


def save_progress(state_root: Path, data: dict) -> None:
    """Atomically save a complete folder checkpoint with restrictive permissions."""
    path = progress_path(state_root)
    if state_root.is_symlink() or path.is_symlink():
        raise InventoryError('UNSAFE_PROGRESS_PATH')
    if (len(data['entries']) > MAX_TOTAL_RECORDS or
            len(data['pending']) + len(data['finished']) > MAX_PENDING_FOLDERS):
        raise InventoryError('PROGRESS_LIMIT_EXCEEDED')
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = json.dumps(data, ensure_ascii=False).encode('utf-8')
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise InventoryError('PROGRESS_TOO_LARGE')
    fd, temp = tempfile.mkstemp(prefix='.drive-checkpoint-', dir=state_root)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def list_one_folder(folder: str, *, timeout: float = FOLDER_SECONDS) -> list[dict]:
    """Nonrecursive, bounded rclone listing for exactly one Drive directory."""
    if not safe_folder(folder):
        raise InventoryError('UNSAFE_FOLDER')
    remote = SOURCE + folder
    command = ['rclone', 'lsjson', remote, '--checkers', '1',
               '--tpslimit', '2', '--log-level', 'ERROR']
    deadline = time.monotonic() + timeout
    process = None
    payload = bytearray()
    try:
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise InventoryError('RCLONE_START_FAILED') from exc
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InventoryError('FOLDER_LIST_TIMEOUT')
                if diagnosis.backup_busy():
                    raise InventoryDeferred()
                if not selector.select(min(5, remaining)):
                    continue
                block = os.read(process.stdout.fileno(), 65536)
                if not block:
                    break
                payload.extend(block)
                if len(payload) > MAX_FOLDER_OUTPUT_BYTES:
                    raise InventoryError('FOLDER_LIST_TOO_LARGE')
        try:
            exit_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise InventoryError('FOLDER_LIST_TIMEOUT') from exc
        if exit_code:
            raise InventoryError('FOLDER_LIST_FAILED')
        try:
            results = json.loads(payload)
        except (ValueError, UnicodeError) as exc:
            raise InventoryError('INVALID_FOLDER_JSON') from exc
        if not isinstance(results, list):
            raise InventoryError('INVALID_FOLDER_LISTING')
        return results
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()


def rebase_folder(folder: str, children: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate immediate children, preserve duplicate files, enqueue unique dirs."""
    entries, subfolders = [], []
    directory_paths = set()
    for item in children:
        if not isinstance(item, dict):
            raise InventoryError('INVALID_FOLDER_ENTRY')
        name = item.get('Path')
        if (not diagnosis.valid_path(name) or '/' in name):
            raise InventoryError('UNSAFE_OR_RECURSIVE_FOLDER_ENTRY')
        full_name = (folder + '/' if folder else '') + name
        entry = {**item, 'Path': full_name}
        if entry.get('IsDir') is True:
            if full_name in directory_paths:
                raise InventoryError('AMBIGUOUS_DIRECTORY_PATH')
            directory_paths.add(full_name)
            subfolders.append(full_name)
        entries.append(entry)
    if entries:
        inspect_entries(entries)
    return entries, subfolders


def walk_resumable(state_root: Path, *, timeout: float = WALK_SECONDS,
                   reader=None) -> tuple[bool, dict]:
    """Commit each completely listed folder; resume without repeating it."""
    if reader is None:
        reader = list_one_folder
    progress = load_progress(state_root)
    if progress is None:
        progress = {'version': 1, 'source': SOURCE, 'pending': [''],
                    'finished': [], 'entries': []}
        save_progress(state_root, progress)
    deadline = time.monotonic() + timeout
    while progress['pending'] and deadline - time.monotonic() > 15:
        if diagnosis.backup_busy():
            return False, progress
        folder = progress['pending'][0]
        try:
            children = reader(folder, timeout=min(FOLDER_SECONDS,
                                                 deadline - time.monotonic()))
        except InventoryDeferred:
            return False, progress
        entries, subfolders = rebase_folder(folder, children)
        already = set(progress['pending'] + progress['finished'])
        next_folders = [name for name in subfolders if name not in already]
        next_progress = {'version': 1, 'source': SOURCE,
                         'pending': progress['pending'][1:] + next_folders,
                         'finished': progress['finished'] + [folder],
                         'entries': progress['entries'] + entries}
        save_progress(state_root, next_progress)
        progress = next_progress
    if progress['pending']:
        return False, progress
    stats = save(state_root, progress['entries'])
    progress_path(state_root).unlink()
    return True, stats


def refresh(state_root: Path, *, timeout: float = WALK_SECONDS) -> tuple[bool, dict]:
    """Resumable folder walk; never throw away completed folder listings."""
    if state_root.is_symlink():
        raise InventoryError('UNSAFE_INVENTORY_DESTINATION')
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return walk_resumable(state_root, timeout=timeout)


def format_summary(prefix: str, stats: dict) -> str:
    return (f'{prefix}: {stats["count"]} records, '
            f'{stats["known_files"]} known-size files, '
            f'{stats["unknown_files"]} unknown-size files, '
            f'{stats["directories"]} directories, '
            f'{stats["known_bytes"]} known media bytes, '
            f'{stats["collision_paths"]} repeated paths covering '
            f'{stats["collision_records"]} records')


def run(*, execute: bool = False, wait_idle: bool = False) -> int:
    if not execute:
        try:
            _, stats = load(archive.STATE)
            print(format_summary('Drive cached inventory (no network call)', stats),
                  flush=True)
        except InventoryError as exc:
            if str(exc) != 'INVENTORY_NOT_CREATED':
                raise
            print('Drive inventory: NO_COMPLETE_SNAPSHOT', flush=True)
        progress = load_progress(archive.STATE)
        if progress is not None:
            print(f'Drive inventory checkpoint: '
                  f'{len(progress["finished"])} folders completed; '
                  f'{len(progress["pending"])} pending; '
                  f'{len(progress["entries"])} records saved; '
                  'no network call', flush=True)
        return 0
    archive.safe_directory(archive.SOURCES['drive'][1])
    if archive.STATE.is_symlink():
        raise InventoryError('UNSAFE_STATE_DIRECTORY')
    if (archive.ROOT / '.apple-sync.lock').is_symlink():
        raise InventoryError('UNSAFE_ARCHIVE_LOCK')
    if wait_idle and not diagnosis.wait_for_idle():
        return 0
    if diagnosis.backup_busy():
        print('Drive inventory DEFER: another backup active or uncertain; '
              'no Drive listing requested', flush=True)
        return 0
    archive.STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    configured = subprocess.run(
        ['rclone', 'listremotes'], capture_output=True,
        text=True, timeout=15, check=False)
    if configured.returncode or SOURCE not in configured.stdout.splitlines():
        raise InventoryError('MISSING_ICLOUD_REMOTE')
    with (archive.ROOT / '.apple-sync.lock').open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Drive inventory DEFER: another Apple archive owns the lock',
                  flush=True)
            return 0
        if diagnosis.backup_busy():
            print('Drive inventory DEFER: another backup active before listing',
                  flush=True)
            return 0
        print('Drive inventory: resumable folder-by-folder metadata listing; '
              'no file downloads', flush=True)
        complete, details = refresh(archive.STATE)
        if complete:
            print(format_summary('Drive inventory saved privately', details),
                  flush=True)
        else:
            print(f'Drive inventory PARTIAL: '
                  f'{len(details["finished"])} folders completed; '
                  f'{len(details["pending"])} pending; '
                  f'{len(details["entries"])} records checkpointed; '
                  'continue with the same command after backups go idle',
                  flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true',
                        help='explicit opt-in full metadata enumeration')
    parser.add_argument('--wait-idle', action='store_true',
                        help='wait up to twelve minutes for other backups to finish')
    args = parser.parse_args()
    if args.wait_idle and not args.execute:
        parser.error('--wait-idle requires --execute')
    try:
        return run(execute=args.execute, wait_idle=args.wait_idle)
    except (InventoryError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print('Drive inventory: ' +
              (str(exc) if isinstance(exc, InventoryError)
               else 'LOCAL_OR_REMOTE_ERROR') +
              '; previous snapshot preserved', file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

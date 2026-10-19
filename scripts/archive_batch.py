#!/usr/bin/env python3
"""Bounded, download-only iCloud archival. Requires explicit --execute for writes."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time

MOUNT = Path('/drive')
ROOT = MOUNT / 'icloud'
MAX_FILES = 2
MAX_BYTES = 128 * 1024 * 1024
RATE = '512K'  # Rclone KiB/s: one transfer at a time
TOTAL_SECONDS = 15 * 60
LIST_SECONDS = 5 * 60

SOURCES = {
    'drive': ('icloud:', ROOT / 'drive'),
    # All Photos avoids downloading the same asset again for each album membership.
    'photos': ('icloudphotos:PrimarySync/All Photos', ROOT / 'photos' / 'primary'),
}


def fail(message: str) -> None:
    raise RuntimeError(message)


def check_mount_and_destination(destination: Path) -> None:
    found = subprocess.run(
        ['findmnt', '-n', '--mountpoint', str(MOUNT), '-o', 'TARGET,FSTYPE,OPTIONS'],
        capture_output=True, text=True, check=False, timeout=10,
    )
    fields = found.stdout.strip().split()
    if (found.returncode != 0 or len(fields) < 3 or
            fields[0] != str(MOUNT) or fields[1] != 'ext4' or
            'rw' not in fields[2].split(',')):
        fail('/drive is not the expected writable ext4 mount; refusing to run')
    if not destination.is_dir() or not os.access(destination, os.W_OK):
        fail(f'{destination} must exist and be writable; refusing to run')
    # Avoid treating symlinks as directories or writing to unexpected mounts.
    for part in [ROOT, *destination.parents]:
        if part == Path('/'):
            continue
        if part.is_symlink():
            fail(f'refusing symlink in destination ancestry: {part}')
    if destination.is_symlink():
        fail(f'refusing symlink destination: {destination}')


def safe_relative_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value or '\x00' in value or '\n' in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(x in ('', '.', '..') for x in value.split('/')):
        return None
    return Path(*path.parts)


def plan(entries: list[dict], destination: Path, max_files: int = MAX_FILES,
         max_bytes: int = MAX_BYTES) -> tuple[list[tuple[str, int]], int]:
    """Return bounded new relative file paths, and count of oversized candidates."""
    selected: list[tuple[str, int]] = []
    total = 0
    oversized = 0
    for entry in sorted(entries, key=lambda e: str(e.get('Path', ''))):
        rel = safe_relative_path(entry.get('Path'))
        size = entry.get('Size')
        if rel is None or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            continue  # Uncertain sizes/paths cannot be safely budgeted.
        target = destination / rel
        if target.exists() or target.is_symlink():
            continue  # Never overwrite or replace existing local content.
        # Do not traverse symlinked intermediate directories.
        if any((destination / Path(*rel.parts[:i])).is_symlink()
               for i in range(1, len(rel.parts))):
            continue
        if size > max_bytes:
            oversized += 1
            continue
        if size <= max_bytes - total:
            selected.append((rel.as_posix(), size))
            total += size
        if len(selected) == max_files:
            break
    return selected, oversized


def copy_file(source: str, rel: str, dest: Path, seconds: float) -> bool:
    remote_file = source.rstrip('/') + '/' + rel
    dest_file = dest / Path(*PurePosixPath(rel).parts)
    # Each parent must remain free of symlinks. No existing file is replaced.
    if dest_file.exists() or dest_file.is_symlink():
        print(f'SKIP already exists: {rel}', flush=True)
        return True
    current = dest
    for component in Path(*PurePosixPath(rel).parts).parts[:-1]:
        current = current / component
        if current.is_symlink():
            fail(f'refusing symlink directory: {current}')
        current.mkdir(exist_ok=True)
    cmd = [
        'rclone', 'copyto', remote_file, str(dest_file),
        '--ignore-existing', '--transfers', '1', '--checkers', '1',
        '--multi-thread-streams', '1', '--bwlimit', RATE,
        '--retries', '1', '--low-level-retries', '1',
        '--log-level', 'ERROR',
    ]
    print(f'DOWNLOAD {rel}', flush=True)
    try:
        result = subprocess.run(cmd, timeout=max(1.0, seconds), check=False)
    except subprocess.TimeoutExpired:
        print(f'TIMED OUT {rel}', file=sys.stderr, flush=True)
        return False
    if result.returncode != 0:
        print(f'FAILED {rel} (rclone exit {result.returncode})', file=sys.stderr, flush=True)
        return False
    return True


def run(kind: str, execute: bool) -> int:
    if shutil.which('rclone') is None:
        fail('rclone is missing')
    source, destination = SOURCES[kind]
    check_mount_and_destination(destination)
    config = subprocess.run(['rclone', 'listremotes'], capture_output=True,
                            text=True, timeout=15, check=True)
    if source.split(':', 1)[0] + ':' not in config.stdout.splitlines():
        fail(f'{source.split(":", 1)[0]}: is not configured; authenticate locally first')
    # Lock is shared by both units, so the two downloaders never overlap.
    lock_path = ROOT / '.apple-sync.lock'
    if lock_path.is_symlink():
        fail('refusing symlink lock file')
    with lock_path.open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another apple-sync pass is active; skipping this hour')
            return 0
        started = time.monotonic()
        listing = subprocess.run(
            ['rclone', 'lsjson', source, '--recursive', '--files-only',
             '--checkers', '1', '--tpslimit', '2'],
            capture_output=True, text=True, timeout=LIST_SECONDS, check=False,
        )
        if listing.returncode != 0:
            fail('Remote listing failed; no download attempted. ' + listing.stderr[-800:])
        entries = json.loads(listing.stdout)
        if not isinstance(entries, list):
            fail('Unexpected remote listing; refusing downloads')
        selected, oversized = plan(entries, destination)
        total = sum(size for _, size in selected)
        print(f'{kind}: planned {len(selected)} files / {total} bytes; '
              f'{oversized} oversize candidates observed; mode={"EXECUTE" if execute else "DRY RUN"}')
        for rel, size in selected:
            print(f'  {size:>12}  {rel}')
        if not execute:
            print('Dry run only. No files downloaded. Enable timers explicitly after review.')
            return 0
        failures = 0
        for rel, _ in selected:
            remaining = TOTAL_SECONDS - (time.monotonic() - started)
            if remaining < 10:
                print('Time budget exhausted; stopping')
                break
            if not copy_file(source, rel, destination, remaining):
                failures += 1
                break  # Fail closed: do not keep trying alternatives this hour.
        return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=sorted(SOURCES))
    parser.add_argument('--execute', action='store_true', help='actually download selected files')
    args = parser.parse_args()
    try:
        return run(args.kind, args.execute)
    except (OSError, subprocess.SubprocessError, RuntimeError, json.JSONDecodeError) as exc:
        print(f'apple-sync: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

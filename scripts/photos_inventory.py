#!/usr/bin/env python3
"""Private, persistent full-library iCloud Photos metadata inventory.

Refreshes use rclone's own on-disk Photos cache and Apple change check.
Ordinary bounded archive passes read this snapshot without listing remotely.
No photo/video contents are downloaded by this module.
"""
from __future__ import annotations

import json
from collections import Counter
import os
from pathlib import Path
import subprocess
import tempfile
import time

NAME = 'photos-primary-inventory.json'
REFRESH_TIMEOUT = 30 * 60
MAX_AGE = 48 * 60 * 60


def valid_path(raw):
    return (isinstance(raw, str) and raw and not raw.startswith('/')
            and not any(p in ('', '.', '..') for p in raw.split('/'))
            and not any(c in raw for c in ('\x00', '\r', '\n', '\\')))


def inspect_entries(entries):
    """Reject partial, colliding or ill-formed manifests rather than dropping files."""
    if not isinstance(entries, list) or not entries:
        raise RuntimeError('Empty or invalid All Photos inventory')
    paths, total = set(), 0
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('IsDir'):
            raise RuntimeError('Malformed All Photos inventory entry')
        name, size = entry.get('Path'), entry.get('Size')
        if not valid_path(name) or type(size) is not int or size < 0:
            raise RuntimeError('Unsafe path or missing size in All Photos inventory')
        # Apple permits distinct assets with the same visible filename.
        # Retain ALL entries, including collisions. Downloading by filename
        # is ambiguous; the archive worker will exclude these paths until
        # an ID-targeted download mechanism is validated.
        paths.add(name)
        total += size
    return paths, total


def unambiguous_entries(entries):
    """Retain every record in the inventory, but never guess at an ambiguous URL."""
    counts = Counter(entry['Path'] for entry in entries)
    safe = [entry for entry in entries if counts[entry['Path']] == 1]
    conflict_paths = sum(count > 1 for count in counts.values())
    conflict_records = sum(count for count in counts.values() if count > 1)
    return safe, conflict_paths, conflict_records


def entry_keys(entries):
    """Reject disappearing IDs on refresh, including colliding assets."""
    return {(item['Path'], str(item.get('ID'))) if item.get('ID') is not None
            else (item['Path'], None) for item in entries}


def cache_path(state_root):
    return state_root / NAME


def load(state_root: Path, remote: str, *, now=None, max_age=MAX_AGE):
    path = cache_path(state_root)
    if state_root.is_symlink() or path.is_symlink():
        raise RuntimeError('Refusing symlink in Photos inventory')
    if not path.is_file():
        raise RuntimeError('All Photos inventory missing: run photos-inventory --execute first')
    if path.stat().st_mode & 0o077:
        raise RuntimeError('All Photos inventory permissions are not private (0600 required)')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or payload.get('version') != 1 or payload.get('source') != remote:
        raise RuntimeError('Invalid All Photos inventory format or remote')
    created = payload.get('created')
    now = time.time() if now is None else now
    if type(created) not in (int, float) or created > now + 60:
        raise RuntimeError('Invalid All Photos inventory timestamp')
    if max_age is not None and now - created > max_age:
        raise RuntimeError('All Photos inventory older than 48 hours; refresh metadata first')
    entries = payload.get('entries')
    paths, total = inspect_entries(entries)
    if payload.get('count') != len(entries) or payload.get('total_bytes') != total:
        raise RuntimeError('All Photos inventory count/byte-total mismatch')
    return entries, total


def save(state_root: Path, remote: str, entries: list[dict], *, now=None):
    paths, total = inspect_entries(entries)
    path = cache_path(state_root)
    if state_root.is_symlink() or path.is_symlink():
        raise RuntimeError('Refusing symlink in Photos inventory destination')
    if path.exists():
        old, _ = load(state_root, remote, max_age=None)
        missing = entry_keys(old) - entry_keys(entries)
        if missing:
            raise RuntimeError('Apple Photos listing omitted previously known assets; '
                               'previous inventory kept for manual review')
    state_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.photos-inventory-', dir=state_root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'version': 1, 'source': remote,
                       'created': time.time() if now is None else now,
                       'count': len(entries), 'total_bytes': total,
                       'entries': entries}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return len(entries), total


def refresh(state_root: Path, remote: str, *, runner=subprocess.run,
            timeout=REFRESH_TIMEOUT):
    """One bounded remote listing; never replaces a valid snapshot on error."""
    args = ['rclone', 'lsjson', remote, '--recursive', '--files-only',
            '--checkers', '1', '--tpslimit', '2', '--log-level', 'ERROR']
    try:
        result = runner(args, capture_output=True, text=True,
                        timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('All Photos metadata listing timed out after 30 minutes; '
                           'previous inventory preserved') from exc
    if result.returncode:
        raise RuntimeError(f'All Photos metadata listing failed (exit {result.returncode}); '
                           'previous inventory preserved')
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError('Invalid iCloud Photos JSON; previous inventory preserved') from exc
    return save(state_root, remote, entries)

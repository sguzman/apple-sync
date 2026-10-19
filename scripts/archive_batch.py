#!/usr/bin/env python3
"""Safely archive iCloud data by persistent, bounded byte-range transfers."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time

MOUNT = Path('/drive')
ROOT = MOUNT / 'icloud'
STATE = ROOT / '.apple-sync-state'
MAX_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 8 * 1024 * 1024
RATE = '512K'
RUN_SECONDS = 15 * 60
LIST_SECONDS = 5 * 60
SOURCES = {
    'drive': ('icloud:', ROOT / 'drive'),
    'photos': ('icloudphotos:PrimarySync/All Photos', ROOT / 'photos' / 'primary'),
}


def fail(msg: str) -> None:
    raise RuntimeError(msg)


def safe_path(raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw or any(c in raw for c in ('\x00', '\n', '\r')):
        return None
    parts = raw.split('/')
    if raw.startswith('/') or any(part in ('', '.', '..') for part in parts):
        return None
    return Path(*parts)


def source_identity(entry: dict) -> dict:
    """Conservatively pin remote metadata to reject changed source files."""
    return {k: entry.get(k) for k in ('Path', 'Size', 'ModTime', 'ID', 'Hashes')}


def key_for(kind: str, rel: str) -> str:
    return hashlib.sha256((kind + '\0' + rel).encode('utf-8')).hexdigest()


def state_paths(state_root: Path, kind: str, rel: str) -> tuple[Path, Path]:
    key = key_for(kind, rel)
    folder = state_root / kind / key[:2]
    return folder / (key + '.part'), folder / (key + '.json')


def write_json_atomic(path: Path, data: dict) -> None:
    fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def new_state(entry: dict) -> dict:
    return {'version': 1, 'identity': source_identity(entry),
            'committed': 0, 'last_start': 0, 'last_length': 0, 'last_sha256': None}


def read_state(part: Path, meta: Path, entry: dict) -> dict:
    if part.is_symlink() or meta.is_symlink():
        fail(f'Refusing symlink state {part}')
    if part.exists() != meta.exists():
        fail(f'Orphaned partial/manifest; manual review needed: {part}')
    if not meta.exists():
        return new_state(entry)
    state = json.loads(meta.read_text(encoding='utf-8'))
    if state.get('version') != 1 or state.get('identity') != source_identity(entry):
        fail(f'Source changed or invalid state; preserving partial for review: {part}')
    committed = state.get('committed')
    if not isinstance(committed, int) or committed < 0 or committed > entry['Size']:
        fail(f'Invalid partial progress: {part}')
    actual = part.stat().st_size
    if actual < committed:
        fail(f'Partial shortened unexpectedly: {part}')
    if actual > committed:
        # A crash between appending bytes and committing the manifest: discard only
        # the uncommitted tail. The previously committed prefix is preserved.
        with part.open('r+b') as stream:
            stream.truncate(committed)
            os.fsync(stream.fileno())
    last_len = state.get('last_length', 0)
    if last_len:
        start = state.get('last_start')
        if not isinstance(start, int) or start < 0 or start + last_len != committed:
            fail(f'Invalid last chunk metadata: {part}')
        with part.open('rb') as stream:
            stream.seek(start)
            digest = hashlib.sha256(stream.read(last_len)).hexdigest()
        if digest != state.get('last_sha256'):
            fail(f'Last stored chunk failed checksum: {part}')
    return state


def safe_directory(destination: Path) -> None:
    mounted = subprocess.run(['findmnt', '-n', '--mountpoint', str(MOUNT),
                              '-o', 'TARGET,FSTYPE,OPTIONS'], capture_output=True,
                             text=True, timeout=10, check=False)
    columns = mounted.stdout.strip().split()
    if (mounted.returncode or len(columns) < 3 or columns[0] != '/drive' or
            columns[1] != 'ext4' or 'rw' not in columns[2].split(',')):
        fail('/drive must be a writable ext4 mount')
    for root in (ROOT, destination):
        if not root.is_dir() or not os.access(root, os.W_OK) or root.is_symlink():
            fail(f'Archive directory missing, unwritable, or symlink: {root}')
    for parent in destination.parents:
        if parent.is_symlink():
            fail(f'Symlink in archive path: {parent}')


def validate_target(destination: Path, rel: Path) -> Path:
    current = destination
    for segment in rel.parts[:-1]:
        current = current / segment
        if current.is_symlink():
            fail(f'Refusing symlink parent: {current}')
    target = destination / rel
    if target.is_symlink():
        fail(f'Refusing existing symlink: {target}')
    return target


def stream_range(remote: str, offset: int, count: int, temp: Path,
                 remaining_seconds: float) -> str:
    """Request only the specified range. A short or failed response is rejected."""
    cmd = ['rclone', 'cat', remote, '--offset', str(offset), '--count', str(count),
           '--bwlimit', RATE, '--transfers', '1', '--checkers', '1',
           '--retries', '1', '--low-level-retries', '1', '--log-level', 'ERROR']
    with temp.open('wb') as output:
        result = subprocess.run(cmd, stdout=output, stderr=subprocess.PIPE,
                                timeout=max(1, remaining_seconds), check=False)
        output.flush()
        os.fsync(output.fileno())
    if result.returncode or temp.stat().st_size != count:
        fail(f'Range read failed or wrong length at offset {offset}; '
             f'exit={result.returncode}; actual={temp.stat().st_size}; wanted={count}')
    digest = hashlib.sha256()
    with temp.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def commit_chunk(part: Path, meta: Path, state: dict, temp: Path,
                 offset: int, count: int, digest: str) -> None:
    part.parent.mkdir(parents=True, exist_ok=True)
    if part.stat().st_size != offset:
        fail(f'Unexpected partial size: {part}')
    with part.open('ab') as out, temp.open('rb') as source:
        shutil.copyfileobj(source, out, 1024 * 1024)
        out.flush()
        os.fsync(out.fileno())
    state.update(committed=offset + count, last_start=offset,
                 last_length=count, last_sha256=digest)
    write_json_atomic(meta, state)


def finish(part: Path, meta: Path, destination: Path, rel: Path, expected: int) -> None:
    target = validate_target(destination, rel)
    if target.exists():
        print(f'SKIP: existing final file {rel}', flush=True)
        return
    if part.stat().st_size != expected:
        fail(f'Incomplete partial must not be published: {rel}')
    target.parent.mkdir(parents=True, exist_ok=True)
    # Hardlink publication is atomic and refuses to overwrite an existing file.
    try:
        os.link(part, target, follow_symlinks=False)
    except FileExistsError:
        print(f'SKIP: existing final file {rel}', flush=True)
        return
    part.unlink()
    meta.unlink()
    print(f'COMPLETE: {rel} ({expected} bytes)', flush=True)


def candidates(entries: list[dict], destination: Path, state_root: Path,
               kind: str) -> list[dict]:
    selected = []
    for entry in entries:
        rel = safe_path(entry.get('Path'))
        size = entry.get('Size')
        if rel is None or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            continue
        target = validate_target(destination, rel)
        if target.exists():
            continue
        part, meta = state_paths(state_root, kind, rel.as_posix())
        try:
            current = read_state(part, meta, entry)
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            print(f'DEFER: {rel}: {error}', file=sys.stderr, flush=True)
            continue
        selected.append((current['committed'] == 0, rel.as_posix(), entry, current))
    selected.sort(key=lambda row: (row[0], row[1]))  # Existing partials first
    return [dict(rel=rel, entry=e, state=s) for _, rel, e, s in selected]


def run(kind: str, execute: bool) -> int:
    source, destination = SOURCES[kind]
    safe_directory(destination)
    configured = subprocess.run(['rclone', 'listremotes'], check=True,
                                capture_output=True, text=True, timeout=15)
    if source.split(':', 1)[0] + ':' not in configured.stdout.splitlines():
        fail(f'Missing local rclone remote for {kind}')
    if ROOT.joinpath('.apple-sync.lock').is_symlink() or STATE.is_symlink():
        fail('Refusing symlink lock/state directory')
    if execute:
        STATE.mkdir(exist_ok=True)
    lockpath = ROOT / '.apple-sync.lock'
    if not execute and not lockpath.exists():
        # Pure preview mode must not create even a lock file.
        return preview(kind, source, destination, STATE, execute=False)
    with lockpath.open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another apple-sync pass is active; skipping')
            return 0
        return preview(kind, source, destination, STATE, execute=execute)


def preview(kind: str, source: str, destination: Path, state_root: Path,
            execute: bool) -> int:
    started = time.monotonic()
    listing = subprocess.run(['rclone', 'lsjson', source, '--recursive',
                              '--files-only', '--checkers', '1', '--tpslimit', '2'],
                             capture_output=True, text=True, timeout=LIST_SECONDS,
                             check=False)
    if listing.returncode:
        fail(f'Unable to list {kind}; no downloads attempted (exit {listing.returncode})')
    entries = json.loads(listing.stdout)
    if not isinstance(entries, list):
        fail('Unexpected listing format')
    todo = candidates(entries, destination, state_root, kind)
    print(f'{kind}: {len(todo)} missing/in-progress files; per-run budget '
          f'{MAX_BYTES // (1024 * 1024)} MiB, {RUN_SECONDS // 60} min; '
          f'{"EXECUTE" if execute else "DRY RUN"}')
    for item in todo[:6]:
        print(f'  {item["state"]["committed"]}/{item["entry"]["Size"]} '
              f'{item["rel"]}', flush=True)
    if not execute:
        print('Dry run: no files downloaded')
        return 0
    budget = MAX_BYTES
    for item in todo:
        if budget <= 0 or RUN_SECONDS - (time.monotonic() - started) < 15:
            break
        rel = safe_path(item['rel'])
        entry = item['entry']
        part, meta = state_paths(state_root, kind, item['rel'])
        part.parent.mkdir(parents=True, exist_ok=True)
        if part.is_symlink() or meta.is_symlink():
            fail(f'Unsafe partial path {part}')
        state = item['state']
        if not part.exists():
            with part.open('xb'):
                pass
            write_json_atomic(meta, state)
        while state['committed'] < entry['Size'] and budget > 0:
            remaining = RUN_SECONDS - (time.monotonic() - started)
            if remaining < 15:
                break
            offset = state['committed']
            count = min(CHUNK_BYTES, budget, entry['Size'] - offset)
            remote = source.rstrip('/') + '/' + item['rel']
            fd, filename = tempfile.mkstemp(prefix='.range-', dir=part.parent)
            os.close(fd)
            temp = Path(filename)
            try:
                digest = stream_range(remote, offset, count, temp, remaining - 2)
                commit_chunk(part, meta, state, temp, offset, count, digest)
                budget -= count
            finally:
                temp.unlink(missing_ok=True)
        if state['committed'] == entry['Size']:
            finish(part, meta, destination, rel, entry['Size'])
    print(f'{kind}: complete pass; source payload budget remaining {budget} bytes',
          flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=sorted(SOURCES))
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        return run(args.kind, args.execute)
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError,
            subprocess.SubprocessError) as exc:
        print(f'apple-sync: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

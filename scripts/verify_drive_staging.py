#!/usr/bin/env python3
"""Quarantined iCloud Drive archive verification; no permanent archive writes.

Compare two independent whole-file reads against the *actual* archive chunk
staging/commit/resume/publish pipeline using temporary synthetic file names.
Never print private remote names, file bytes, or hashes. No timer changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_batch as archive
import diagnose_drive_downloads as diagnosis

MAX_FILE_BYTES = 8 * 1024 * 1024
MIN_FILE_BYTES = 64 * 1024
MAX_SAMPLES = 2
MAX_SECONDS = 8 * 60
LIST_SECONDS = 25
CHUNK_BYTES = 1024 * 1024
RATE = '512K'


class Deferred(Exception):
    """Another backup started or the bounded time budget expired."""


class VerificationError(Exception):
    """A deliberately sanitized remote or integrity failure."""


def candidates(entries: object) -> list[dict]:
    if not isinstance(entries, list):
        raise VerificationError('INVALID_LISTING')
    files = [entry for entry in entries
             if isinstance(entry, dict)
             and diagnosis.valid_path(entry.get('Path'))
             and entry.get('IsDir') is False
             and type(entry.get('Size')) is int
             and MIN_FILE_BYTES <= entry['Size'] <= MAX_FILE_BYTES]
    # Prefer files larger than 2 MiB to exercise several genuine chunks.
    # Leave original path names and IDs solely in memory.
    return sorted(files, key=lambda e: (e['Size'] < 2 * 1024 * 1024,
                                        -e['Size']))


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def whole_file(remote: str, target: Path, deadline: float) -> tuple[int, str]:
    """Bounded direct cat; never trust rclone's advertised file size blindly."""
    if diagnosis.backup_busy():
        raise Deferred()
    if deadline - time.monotonic() < 15:
        raise Deferred()
    cmd = ['rclone', 'cat', remote, '--bwlimit', RATE, '--retries', '1',
           '--low-level-retries', '1', '--log-level', 'ERROR']
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise VerificationError('COMMAND_START_FAILED') from exc
    total = 0
    try:
        with target.open('xb') as destination, selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = min(120, deadline - time.monotonic())
                if remaining <= 0 or not selector.select(remaining):
                    raise VerificationError('WHOLE_DOWNLOAD_TIMEOUT')
                data = os.read(proc.stdout.fileno(), 65536)
                if not data:
                    break
                total += len(data)
                if total > MAX_FILE_BYTES:
                    raise VerificationError('WHOLE_OVERSIZE')
                destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
        try:
            code = proc.wait(timeout=max(0.1, min(15, deadline - time.monotonic())))
        except subprocess.TimeoutExpired as exc:
            raise VerificationError('WHOLE_DOWNLOAD_TIMEOUT') from exc
        if code:
            raise VerificationError('WHOLE_DOWNLOAD_ERROR')
        return total, file_digest(target)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def reconstruct(remote: str, size: int, workspace: Path,
                deadline: float, *, reader=None) -> Path:
    """Exercise real archive chunk code, including checkpoint re-reading."""
    state_root = workspace / 'state'
    entry = {'Path': 'sample.bin', 'Size': size, 'ID': 'temporary-verification'}
    part, manifest = archive.state_paths(state_root, 'drive-test', entry['Path'])
    part.parent.mkdir(parents=True)
    part.touch(exist_ok=False)
    archive.write_json_atomic(manifest, archive.new_state(entry))
    state = archive.read_state(part, manifest, entry, repair=False)
    first_chunk = True
    while state['committed'] < size:
        if diagnosis.backup_busy() or deadline - time.monotonic() < 15:
            raise Deferred()
        offset = state['committed']
        count = min(CHUNK_BYTES, size - offset)
        temp, digest, accepted, _requested, _exhausted = archive.fetch_chunk_with_retry(
            remote, offset, count, part.parent, deadline,
            budget=min(16 * 1024 * 1024, MAX_FILE_BYTES * 2),
            reader=reader)
        if temp is None:
            raise VerificationError('RANGE_CHUNK_FAILED')
        try:
            archive.commit_chunk(part, manifest, state, temp, offset, accepted, digest)
        finally:
            temp.unlink(missing_ok=True)
        # Simulate a new process after the first chunk, verifying the
        # persisted checkpoint rather than trusting in-memory state.
        if first_chunk:
            first_chunk = False
            state = archive.read_state(part, manifest, entry, repair=False)
    publication = workspace / 'published'
    publication.mkdir()
    archive.finish(part, manifest, publication, Path('sample.bin'), size)
    return publication / 'sample.bin'


def verify(remote: str, size: int, deadline: float,
           *, reader=None, whole_reader=None) -> str:
    """All writes remain inside a disposable temporary directory."""
    if whole_reader is None:
        whole_reader = whole_file
    with tempfile.TemporaryDirectory(prefix='icloud-drive-verify-') as directory:
        folder = Path(directory)
        first_size, first_sha = whole_reader(remote, folder / 'first', deadline)
        second_size, second_sha = whole_reader(remote, folder / 'second', deadline)
        if first_size != size or second_size != size:
            return 'WHOLE_SIZE_MISMATCH'
        if first_sha != second_sha:
            return 'WHOLE_COPIES_DISAGREE'
        staged = reconstruct(remote, size, folder, deadline, reader=reader)
        if staged.stat().st_size != size:
            return 'STAGING_SIZE_MISMATCH'
        if file_digest(staged) != first_sha:
            return 'STAGING_DIFFERS_FROM_WHOLE'
        return 'STAGING_MATCHES_BOTH_WHOLE_READS'


def run(*, wait_idle: bool = False) -> int:
    if wait_idle and not diagnosis.wait_for_idle():
        return 0
    if diagnosis.backup_busy():
        print('Drive staging verification DEFER: another backup active or '
              'state unknown; no remote reads attempted', flush=True)
        return 0
    deadline = time.monotonic() + MAX_SECONDS
    verified = 0
    tried = 0
    for scope in diagnosis.SCOPES:
        if tried >= MAX_SAMPLES or deadline - time.monotonic() < 15:
            break
        if diagnosis.backup_busy():
            print('Drive staging verification DEFER: another backup started; '
                  'stopping without touching actual archive', flush=True)
            return 0
        print('Drive staging: inspecting a limited source folder; names withheld',
              flush=True)
        try:
            listing = diagnosis.run_rclone(
                ['lsjson', scope, '--files-only', '--recursive', '--max-depth', '2',
                 '--checkers', '1', '--tpslimit', '2'],
                min(LIST_SECONDS, deadline - time.monotonic()))
            files = candidates(json.loads(listing))
        except (diagnosis.ReadFailure, VerificationError, ValueError,
                json.JSONDecodeError):
            print('Drive staging: source folder inconclusive', flush=True)
            continue
        for item in files[:2]:
            if tried >= MAX_SAMPLES or deadline - time.monotonic() < 15:
                break
            if diagnosis.backup_busy():
                print('Drive staging verification DEFER: another backup started; '
                      'stopping before another file', flush=True)
                return 0
            tried += 1
            remote = scope.rstrip('/') + '/' + item['Path']
            category = 'iWork-like' if diagnosis.is_iwork(item['Path']) else 'ordinary'
            try:
                outcome = verify(remote, item['Size'], deadline)
            except Deferred:
                print('Drive staging verification DEFER: busy or time budget; '
                      'only disposable temporary state affected', flush=True)
                return 0
            except (VerificationError, RuntimeError, subprocess.TimeoutExpired,
                    OSError, ValueError) as exc:
                outcome = (str(exc) if isinstance(exc, VerificationError)
                           else 'STAGING_ERROR')
            verified += int(outcome == 'STAGING_MATCHES_BOTH_WHOLE_READS')
            print(f'Drive staging sample {tried} ({category}): {outcome}',
                  flush=True)
    print(f'Drive staging: {tried} examined; {verified} reconstructed and '
          'matched both whole reads; permanent Drive archive remains BLOCKED',
          flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wait-idle', action='store_true')
    args = parser.parse_args()
    return run(wait_idle=args.wait_idle)


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Opt-in pilot: safely archive ONE small ordinary iCloud Drive file.

Full downloads only, independent duplicate verification, source identity check,
global archive lock, atomic no-overwrite publication. No timer modification and
no recursive enumeration of the Drive root. Does not enable unrestricted Drive.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive_batch as archive
import diagnose_drive_downloads as diagnosis
import verify_drive_staging as verification

PILOT_SCOPES = ('icloud:Desktop', 'icloud:Downloads', 'icloud:read')
MAX_PILOT_BYTES = verification.MAX_FILE_BYTES
RUN_SECONDS = 8 * 60
LIST_TIMEOUT = 25


class PilotError(Exception):
    """Safe, filename-free error for the opt-in Drive pilot."""


def eligible(entries: object) -> list[dict]:
    if not isinstance(entries, list):
        raise PilotError('INVALID_REMOTE_LISTING')
    files = [entry for entry in entries
             if isinstance(entry, dict) and diagnosis.valid_path(entry.get('Path'))
             and archive.safe_path(entry['Path']) is not None
             and entry.get('IsDir') is False
             and type(entry.get('Size')) is int
             and verification.MIN_FILE_BYTES <= entry['Size'] <= MAX_PILOT_BYTES
             and not diagnosis.is_iwork(entry['Path'])]
    # Prefer smaller files to limit the very first permanent write.
    return sorted(files, key=lambda entry: (entry['Size'], entry['Path']))


def listing(scope: str, deadline: float) -> list[dict]:
    remaining = deadline - time.monotonic()
    if remaining < 5:
        raise PilotError('TIME_BUDGET')
    try:
        output = diagnosis.run_rclone(
            ['lsjson', scope, '--recursive', '--max-depth', '2',
             '--files-only', '--checkers', '1', '--tpslimit', '2'],
            min(LIST_TIMEOUT, remaining))
        result = json.loads(output)
    except (diagnosis.ReadFailure, ValueError, json.JSONDecodeError) as exc:
        raise PilotError('LISTING_FAILED') from exc
    if not isinstance(result, list):
        raise PilotError('INVALID_REMOTE_LISTING')
    return result


def identity(entry: dict) -> dict:
    return archive.source_identity(entry)


def still_same(entry: dict, current: list[dict]) -> bool:
    matches = [candidate for candidate in current
               if candidate.get('Path') == entry['Path']]
    return len(matches) == 1 and identity(matches[0]) == identity(entry)


def publish_verified(scope: str, entry: dict, destination: Path,
                     state_root: Path, deadline: float,
                     *, whole_reader=None, list_reader=None) -> str:
    """Only an independently verified, unchanged source may be published."""
    if whole_reader is None:
        whole_reader = verification.whole_file
    if list_reader is None:
        list_reader = listing
    if diagnosis.backup_busy():
        return 'DEFER_BUSY'
    prefix = scope.split(':', 1)[1].strip('/')
    raw = (prefix + '/' if prefix else '') + entry['Path']
    rel = archive.safe_path(raw)
    if rel is None or not diagnosis.valid_path(raw):
        raise PilotError('UNSAFE_REMOTE_PATH')
    final = archive.validate_target(destination, rel)
    if final.exists():
        return 'SKIP_EXISTING'

    remote = scope.rstrip('/') + '/' + entry['Path']
    with tempfile.TemporaryDirectory(prefix='.drive-pilot-', dir=state_root) as temp_dir:
        workspace = Path(temp_dir)
        size1, sha1 = whole_reader(remote, workspace / 'first', deadline)
        if diagnosis.backup_busy():
            return 'DEFER_BUSY'
        size2, sha2 = whole_reader(remote, workspace / 'second', deadline)
        if size1 != entry['Size'] or size2 != entry['Size']:
            return 'DEFER_SIZE_MISMATCH'
        if sha1 != sha2:
            return 'DEFER_WHOLE_MISMATCH'
        if diagnosis.backup_busy():
            return 'DEFER_BUSY'
        if not still_same(entry, list_reader(scope, deadline)):
            return 'DEFER_SOURCE_CHANGED'

        # Create a real checkpoint, then reread it just as resumed jobs do.
        # No remote range requests are used in this cautious pilot.
        stage = workspace / 'stage'
        stage.mkdir()
        part, meta = archive.state_paths(stage, 'drive-pilot', raw)
        part.parent.mkdir(parents=True)
        part.touch(exist_ok=False)
        state = archive.new_state({**entry, 'Path': raw})
        archive.write_json_atomic(meta, state)
        archive.commit_chunk(part, meta, state, workspace / 'first',
                             0, size1, sha1)
        inspected = archive.read_state(part, meta, {**entry, 'Path': raw},
                                       repair=False)
        if inspected['committed'] != size1 or verification.file_digest(part) != sha1:
            return 'DEFER_STAGE_CHECK_FAILED'

        if diagnosis.backup_busy():
            return 'DEFER_BUSY'
        final = archive.validate_target(destination, rel)
        if final.exists():
            return 'SKIP_EXISTING'
        final.parent.mkdir(parents=True, exist_ok=True)
        # archive.finish uses atomic hardlink publication and refuses
        # overwrites. Suppress private remote paths in human-facing output.
        with redirect_stdout(io.StringIO()):
            archive.finish(part, meta, destination, rel, size1)
        if not final.exists():
            return 'DEFER_PUBLICATION_FAILED'
        if final.stat().st_size != size1 or verification.file_digest(final) != sha1:
            raise PilotError('PUBLISHED_FILE_VERIFICATION_FAILED')
        return 'PUBLISHED_VERIFIED'


def run(*, execute: bool = False, wait_idle: bool = False) -> int:
    if execute and wait_idle and not diagnosis.wait_for_idle():
        return 0
    if diagnosis.backup_busy():
        print('Drive pilot DEFER: other backup active or state uncertain; '
              'no source reads attempted', flush=True)
        return 0
    deadline = time.monotonic() + RUN_SECONDS
    destination = archive.SOURCES['drive'][1]
    archive.safe_directory(destination)
    if archive.STATE.is_symlink():
        raise PilotError('UNSAFE_STATE_DIRECTORY')
    if execute:
        archive.STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    lockpath = archive.ROOT / '.apple-sync.lock'
    if lockpath.is_symlink():
        raise PilotError('UNSAFE_ARCHIVE_LOCK')

    # Preview is read-only and never creates a lock or archived data.
    def work() -> int:
        for scope in PILOT_SCOPES:
            if diagnosis.backup_busy():
                print('Drive pilot DEFER: another backup became active', flush=True)
                return 0
            try:
                options = eligible(listing(scope, deadline))
            except PilotError:
                print('Drive pilot: limited scope unavailable; trying next',
                      flush=True)
                continue
            for entry in options:
                rel = archive.safe_path(scope.split(':', 1)[1].strip('/') + '/' +
                                        entry['Path'])
                if rel is None:
                    continue
                if archive.validate_target(destination, rel).exists():
                    continue
                if not execute:
                    print('Drive pilot PREVIEW: eligible ordinary file available; '
                          'no downloads or writes attempted', flush=True)
                    return 0
                try:
                    result = publish_verified(scope, entry, destination,
                                              archive.STATE, deadline)
                except (verification.Deferred, PilotError,
                        verification.VerificationError, RuntimeError,
                        OSError, ValueError) as exc:
                    # Do not emit filenames or exception text from rclone.
                    result = ('DEFER_' + str(exc) if isinstance(exc, PilotError)
                              else 'DEFER_READ_OR_STAGING_ERROR')
                print(f'Drive pilot: {result}; unrestricted Drive backup '
                      'remains BLOCKED', flush=True)
                return 0
        print('Drive pilot: no eligible new ordinary file in limited scopes; '
              'unrestricted Drive backup remains BLOCKED', flush=True)
        return 0

    if not execute:
        return work()
    with lockpath.open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Drive pilot DEFER: another Apple archive has the lock',
                  flush=True)
            return 0
        return work()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true',
                        help='opt-in publication of at most one verified ordinary file')
    parser.add_argument('--wait-idle', action='store_true',
                        help='wait for a safe idle window without stopping backups')
    args = parser.parse_args()
    if args.wait_idle and not args.execute:
        parser.error('--wait-idle requires --execute')
    try:
        return run(execute=args.execute, wait_idle=args.wait_idle)
    except (PilotError, OSError, RuntimeError) as exc:
        # Do not leak private remote paths or rclone error output.
        print('Drive pilot error: validation or local storage unavailable; '
              'unrestricted Drive backup remains BLOCKED', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

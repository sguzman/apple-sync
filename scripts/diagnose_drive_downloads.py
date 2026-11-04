#!/usr/bin/env python3
"""Read-only iCloud Drive diagnosis: whole-file versus ranged downloads.

Only samples small files in selected folders. Never writes to the archive,
modifies iCloud, prints private file names, logs file bytes or hashes, or
changes systemd units. This is a diagnosis, NOT approval to enable backups.
"""
from __future__ import annotations

import argparse
import json
import os
import selectors
import subprocess
import time
from pathlib import PurePosixPath

SCOPES = ('icloud:Desktop', 'icloud:Downloads', 'icloud:read',
          'icloud:Pages', 'icloud:Numbers', 'icloud:Keynote')
MAX_CANDIDATES = 4
MAX_PER_SCOPE = 1
MAX_FILE_BYTES = 1024 * 1024
MIN_FILE_BYTES = 4097
SAMPLE_BYTES = 4096
TOTAL_SECONDS = 240
LIST_SECONDS = 25
READ_SECONDS = 40
RATE = '512K'
MAX_LISTING_OUTPUT = 8 * 1024 * 1024
MAX_DOWNLOAD_OUTPUT = 2 * 1024 * 1024
EXTENDED_MAX_CANDIDATES = 8
EXTENDED_MAX_PER_SCOPE = 4
EXTENDED_TOTAL_SECONDS = 420
EXTENDED_SAMPLE_BYTES = 16 * 1024
BUSY_UNITS = ('google-drive-rclone.service', 'apple-sync-photos.service')


class ReadFailure(Exception):
    """Expected probe failure; do not leak rclone stderr or remote filename."""


def valid_path(path: object) -> bool:
    return (isinstance(path, str) and bool(path) and not path.startswith('/')
            and all(part not in ('', '.', '..') for part in path.split('/'))
            and not any(char in path for char in ('\x00', '\r', '\n'))
            and PurePosixPath(path).as_posix() == path)


def select_files(entries: object) -> list[dict]:
    if not isinstance(entries, list):
        raise ValueError('Unexpected iCloud Drive listing format')
    selected = [entry for entry in entries
                if isinstance(entry, dict) and valid_path(entry.get('Path'))
                and type(entry.get('Size')) is int
                and MIN_FILE_BYTES <= entry['Size'] <= MAX_FILE_BYTES
                and entry.get('IsDir') is False]
    # Prefer ordinary files for control samples; do not assume that all
    # iWork packages are represented as ordinary files by the backend.
    return sorted(selected, key=lambda e: (is_iwork(e['Path']), e['Size']))


def varied_files(entries: list[dict], limit: int) -> list[dict]:
    """Spread selected samples over source sizes without logging path names."""
    if not entries or limit < 1:
        return []
    sizes = sorted(entries, key=lambda item: item['Size'])
    indexes = [0, len(sizes) - 1, len(sizes) // 2,
               len(sizes) // 4, 3 * len(sizes) // 4]
    choices = [sizes[index] for index in indexes]
    choices += [item for item in sizes if is_iwork(item['Path'])]
    choices += sizes
    selected, seen = [], set()
    for item in choices:
        if item['Path'] in seen:
            continue
        selected.append(item)
        seen.add(item['Path'])
        if len(selected) == limit:
            break
    return selected


def backup_busy() -> bool:
    """Fail closed on uncertainty before competing with other download jobs."""
    for unit in BUSY_UNITS:
        try:
            result = subprocess.run(
                ['systemctl', '--user', 'show', unit,
                 '--property=ActiveState', '--value', '--no-pager'],
                capture_output=True, text=True, check=False, timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            return True
        if result.returncode or result.stdout.strip() not in ('inactive', 'failed'):
            return True
    return False


def is_iwork(path: str) -> bool:
    return path.lower().endswith(('.key', '.numbers', '.pages', '.playground'))


def run_rclone(args: list[str], seconds: float) -> bytes:
    """Memory- and time-bounded rclone read; suppress sensitive stderr."""
    if seconds <= 2:
        raise ReadFailure('TIME_BUDGET')
    command = ['rclone', *args, '--bwlimit', RATE, '--retries', '1',
               '--low-level-retries', '1', '--log-level', 'ERROR']
    output_limit = (MAX_LISTING_OUTPUT if args[0] == 'lsjson'
                    else MAX_DOWNLOAD_OUTPUT)
    deadline = time.monotonic() + min(seconds, READ_SECONDS)
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise ReadFailure('LOCAL_COMMAND_ERROR') from exc
    data = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ReadFailure('READ_TIMEOUT')
                piece = os.read(proc.stdout.fileno(), 65536)
                if not piece:
                    break
                data.extend(piece)
                if len(data) > output_limit:
                    raise ReadFailure('OUTPUT_TOO_LARGE')
        try:
            code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise ReadFailure('READ_TIMEOUT') from exc
        if code:
            raise ReadFailure('RCLONE_READ_ERROR')
        return bytes(data)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def inspect(whole_first: bytes, whole_again: bytes,
            zero_first: bytes, one: bytes, zero_again: bytes,
            listed_size: int, *, sample_bytes: int = SAMPLE_BYTES) -> tuple[str, str]:
    """Two independent whole downloads are the reference, not ground truth."""
    if whole_first != whole_again:
        whole_status = 'WHOLE_DOWNLOADS_DISAGREE'
    elif len(whole_first) != listed_size:
        whole_status = 'WHOLE_SIZE_DIFFERS_FROM_LISTING'
    else:
        whole_status = 'WHOLE_STABLE_MATCHES_SIZE'

    n = min(sample_bytes, listed_size - 1)
    if any(len(data) != n for data in (zero_first, one, zero_again)):
        range_status = 'RANGE_WRONG_LENGTH'
    elif zero_first != zero_again:
        range_status = 'RANGE_SAME_OFFSET_UNSTABLE'
    elif zero_first == one and len(set(zero_first)) >= 2:
        range_status = 'RANGE_OFFSET_IGNORED_OR_STALE'
    elif zero_first[1:] != one[:-1]:
        range_status = 'RANGE_OVERLAP_MISMATCH'
    elif zero_first != whole_first[:n] or one != whole_first[1:n + 1]:
        range_status = 'RANGE_DISAGREES_WITH_WHOLE'
    elif len(set(zero_first)) < 2:
        range_status = 'RANGE_UNIFORM_INCONCLUSIVE'
    else:
        range_status = 'RANGE_AGREES_WITH_WHOLE'

    return whole_status, range_status


def examine(remote: str, size: int, deadline: float,
            *, extended: bool = False) -> tuple[str, str]:
    """Bounded, in-memory reads; raw content never reaches output."""
    def read(offset: int | None = None, count: int | None = None) -> bytes:
        args = ['cat', remote]
        if offset is not None:
            args.extend(['--offset', str(offset)])
        if count is not None:
            args.extend(['--count', str(count)])
        return run_rclone(args, deadline - time.monotonic())

    n = min(EXTENDED_SAMPLE_BYTES if extended else SAMPLE_BYTES, size - 1)
    try:
        whole_first = read()
        whole_again = read()
        first = read(0, n)
        second = read(1, n)
        again = read(0, n)
        middle_offset = max(0, (size - min(SAMPLE_BYTES, size)) // 2)
        middle_count = min(SAMPLE_BYTES, size - middle_offset)
        middle = read(middle_offset, middle_count) if extended else None
    except ReadFailure as exc:
        return ('INCONCLUSIVE_READ_ERROR', str(exc))
    whole, ranges = inspect(whole_first, whole_again, first, second, again,
                            size, sample_bytes=n)
    if extended and middle is not None and ranges == 'RANGE_AGREES_WITH_WHOLE':
        if middle != whole_first[middle_offset:middle_offset + middle_count]:
            ranges = 'RANGE_MIDDLE_DISAGREES_WITH_WHOLE'
    return whole, ranges


def diagnose(*, extended: bool = False) -> int:
    deadline = time.monotonic() + (
        EXTENDED_TOTAL_SECONDS if extended else TOTAL_SECONDS)
    candidate_limit = EXTENDED_MAX_CANDIDATES if extended else MAX_CANDIDATES
    per_scope = EXTENDED_MAX_PER_SCOPE if extended else MAX_PER_SCOPE
    if extended and backup_busy():
        print('Drive diagnostic DEFER: a backup is active or its state is unknown; '
              'no source reads attempted', flush=True)
        return 0
    checked = 0
    clean = 0
    differences = 0
    saw_iwork = False
    for scope in SCOPES:
        if checked >= candidate_limit or deadline - time.monotonic() < 15:
            break
        print('Drive: listing one limited scope; names and contents withheld',
              flush=True)
        try:
            blob = run_rclone(
                ['lsjson', scope, '--files-only', '--recursive',
                 '--max-depth', '2', '--checkers', '1', '--tpslimit', '2'],
                min(LIST_SECONDS, deadline - time.monotonic()))
            entries = select_files(json.loads(blob))
        except (ReadFailure, ValueError, json.JSONDecodeError):
            print('Drive: scope unavailable or inconclusive', flush=True)
            continue
        selections = (varied_files(entries, per_scope) if extended
                      else entries[:per_scope])
        for entry in selections:
            if checked >= candidate_limit or deadline - time.monotonic() < 15:
                break
            if extended and backup_busy():
                print('Drive diagnostic DEFER: another backup became active; '
                      'stopping read-only sampling', flush=True)
                break
            checked += 1
            iwork = is_iwork(entry['Path'])
            saw_iwork |= iwork
            category = 'iWork-like' if iwork else 'ordinary'
            remote = scope.rstrip('/') + '/' + entry['Path']
            whole, ranges = examine(remote, entry['Size'], deadline,
                                     extended=extended)
            ok = (whole == 'WHOLE_STABLE_MATCHES_SIZE'
                  and ranges == 'RANGE_AGREES_WITH_WHOLE')
            clean += int(ok)
            differences += int(not ok)
            print(f'Drive sample {checked} ({category}): '
                  f'whole={whole}, ranges={ranges}', flush=True)

    print(f'Drive diagnosis: {checked} samples; {clean} fully consistent; '
          f'{differences} differing/inconclusive; iWork sampled={saw_iwork}',
          flush=True)
    if checked < 2:
        print('DIAGNOSIS INCONCLUSIVE: fewer than two samples; '
              'Drive archive remains BLOCKED', flush=True)
    elif differences:
        print('DIAGNOSIS FOUND DIFFERENCES: Drive archive remains BLOCKED',
              flush=True)
    else:
        print('DIAGNOSIS SAMPLES CONSISTENT: further end-to-end validation '
              'required; Drive archive remains BLOCKED', flush=True)
    # This is a diagnostic command, not a backup gate. Nonzero only when
    # invocation itself crashes; anomalies are reported explicitly above.
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extended', action='store_true',
                        help='more varied small files and 16-KiB/mid-file ranges; '
                        'defer while other backups are active')
    args = parser.parse_args()
    return diagnose(extended=args.extended)


if __name__ == '__main__':
    raise SystemExit(main())

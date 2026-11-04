#!/usr/bin/env python3
"""Read-only iCloud Drive diagnosis: whole-file versus ranged downloads.

Only samples small files in selected folders. Never writes to the archive,
modifies iCloud, prints private file names, logs file bytes or hashes, or
changes systemd units. This is a diagnosis, NOT approval to enable backups.
"""
from __future__ import annotations

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
            listed_size: int) -> tuple[str, str]:
    """Two independent whole downloads are the reference, not ground truth."""
    if whole_first != whole_again:
        whole_status = 'WHOLE_DOWNLOADS_DISAGREE'
    elif len(whole_first) != listed_size:
        whole_status = 'WHOLE_SIZE_DIFFERS_FROM_LISTING'
    else:
        whole_status = 'WHOLE_STABLE_MATCHES_SIZE'

    n = min(SAMPLE_BYTES, listed_size - 1)
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


def examine(remote: str, size: int, deadline: float) -> tuple[str, str]:
    """Bounded, in-memory reads; raw content never reaches output."""
    def read(offset: int | None = None, count: int | None = None) -> bytes:
        args = ['cat', remote]
        if offset is not None:
            args.extend(['--offset', str(offset)])
        if count is not None:
            args.extend(['--count', str(count)])
        return run_rclone(args, deadline - time.monotonic())

    n = min(SAMPLE_BYTES, size - 1)
    try:
        whole_first = read()
        whole_again = read()
        first = read(0, n)
        second = read(1, n)
        again = read(0, n)
    except ReadFailure as exc:
        return ('INCONCLUSIVE_READ_ERROR', str(exc))
    return inspect(whole_first, whole_again, first, second, again, size)


def diagnose() -> int:
    deadline = time.monotonic() + TOTAL_SECONDS
    checked = 0
    clean = 0
    differences = 0
    saw_iwork = False
    for scope in SCOPES:
        if checked >= MAX_CANDIDATES or deadline - time.monotonic() < 15:
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
        for entry in entries[:MAX_PER_SCOPE]:
            if checked >= MAX_CANDIDATES or deadline - time.monotonic() < 15:
                break
            checked += 1
            iwork = is_iwork(entry['Path'])
            saw_iwork |= iwork
            category = 'iWork-like' if iwork else 'ordinary'
            remote = scope.rstrip('/') + '/' + entry['Path']
            whole, ranges = examine(remote, entry['Size'], deadline)
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


if __name__ == '__main__':
    raise SystemExit(diagnose())

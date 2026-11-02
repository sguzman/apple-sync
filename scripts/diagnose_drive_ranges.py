#!/usr/bin/env python3
"""Read-only diagnosis of iCloud Drive byte ranges across multiple small files.

Does not write any downloaded data or expose private filenames or file bytes.
This tool is deliberately separate from the archive downloader.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import PurePosixPath

SCOPES = ('icloud:Desktop', 'icloud:Downloads', 'icloud:read')
MAX_CANDIDATES = 3
MAX_PER_SCOPE = 2
MAX_FILE_BYTES = 1024 * 1024
SAMPLE_BYTES = 4096
DEADLINE_SECONDS = 180


def valid_path(path: object) -> bool:
    return (isinstance(path, str) and bool(path) and
            not path.startswith('/') and
            not any(p in ('', '.', '..') for p in path.split('/')) and
            not any(c in path for c in ('\x00', '\r', '\n')) and
            PurePosixPath(path).as_posix() == path)


def select_files(entries: object) -> list[dict]:
    if not isinstance(entries, list):
        raise ValueError('Unexpected inventory JSON')
    candidates = [e for e in entries if isinstance(e, dict) and
                  valid_path(e.get('Path')) and type(e.get('Size')) is int and
                  2 <= e['Size'] <= MAX_FILE_BYTES and not e.get('IsDir', False)]
    return sorted(candidates, key=lambda e: (e['Size'] < SAMPLE_BYTES + 1,
                                              e['Size'], e['Path']))


def rclone(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(['rclone', *args, '--retries', '1',
                           '--low-level-retries', '1', '--log-level', 'ERROR'],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=max(1, timeout), check=False)


def classify(first: bytes, shifted: bytes, again: bytes) -> str:
    if len(first) != len(shifted) or len(first) != len(again):
        return 'WRONG_LENGTH'
    if first != again:
        return 'INCONSISTENT_SAME_OFFSET'
    if len(set(first)) < 2:
        return 'INCONCLUSIVE_UNIFORM_DATA'
    if first[1:] == shifted[:-1]:
        return 'PASS'
    if first == shifted:
        return 'OFFSET_IGNORED_OR_STALE_RESPONSE'
    return 'OFFSET_CONTENT_MISMATCH'


def examine(remote: str, size: int, started: float) -> str:
    length = min(SAMPLE_BYTES, size - 1)
    readings = []
    for offset in (0, 1, 0):
        left = DEADLINE_SECONDS - (time.monotonic() - started)
        if left < 5:
            return 'INCONCLUSIVE_TIME_BUDGET'
        try:
            result = rclone(['cat', remote, '--offset', str(offset),
                             '--count', str(length), '--bwlimit', '512K'],
                            min(30, left))
        except subprocess.TimeoutExpired:
            return 'READ_TIMEOUT'
        if result.returncode:
            return 'READ_ERROR'
        if len(result.stdout) != length:
            return 'WRONG_LENGTH'
        readings.append(result.stdout)
    return classify(*readings)


def diagnose() -> int:
    started = time.monotonic()
    checked = 0
    failures = 0
    passed = 0
    for scope in SCOPES:
        if checked >= MAX_CANDIDATES:
            break
        remaining = DEADLINE_SECONDS - (time.monotonic() - started)
        if remaining < 10:
            break
        print('Drive: scanning a limited folder without printing private names',
              flush=True)
        try:
            result = rclone(['lsjson', scope, '--files-only', '--recursive',
                             '--max-depth', '2', '--checkers', '1',
                             '--tpslimit', '2'], min(25, remaining))
            if result.returncode:
                print('Drive: directory listing failed; checking another', flush=True)
                continue
            files = select_files(json.loads(result.stdout))
        except (subprocess.TimeoutExpired, ValueError, json.JSONDecodeError):
            print('Drive: directory listing timed out or malformed; checking another',
                  flush=True)
            continue
        for entry in files[:MAX_PER_SCOPE]:
            if checked >= MAX_CANDIDATES:
                break
            checked += 1
            remote = scope.rstrip('/') + '/' + entry['Path']
            result_name = examine(remote, entry['Size'], started)
            print(f'Drive sample {checked}: {result_name}', flush=True)
            if result_name == 'PASS':
                passed += 1
            else:
                failures += 1
    print(f'Drive diagnosis: {checked} samples, {passed} pass, '
          f'{failures} other results', flush=True)
    if failures:
        print('Do not enable Drive archiving; failed or inconclusive range reads.',
              flush=True)
        return 2
    if checked < 2:
        print('Inconclusive: need at least two different successful samples.',
              flush=True)
        return 2
    print('Sampled ranges consistent; full downloader still requires live validation.',
          flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(diagnose())

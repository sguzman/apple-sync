#!/usr/bin/env python3
"""Bounded read-only range probes for the Apple rclone remotes.

Downloads two overlapping byte ranges to memory only. Never writes an archive,
changes a remote, or prints any user file name or contents.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import PurePosixPath

# Probe small directories instead of recursively enumerating an entire library.
SCOPES = {
    'drive': ('icloud:Desktop', 'icloud:Downloads', 'icloud:read'),
    'photos': ('icloudphotos:PrimarySync/Favorites',
               'icloudphotos:PrimarySync/Animated',
               'icloudphotos:PrimarySync/Car'),
}
MAX_FILE_BYTES = 16 * 1024 * 1024
PROBE_BYTES = 16 * 1024
LIST_TIMEOUT = 25
READ_TIMEOUT = 40


def valid_path(path: object) -> bool:
    return (isinstance(path, str) and bool(path) and
            not path.startswith('/') and
            not any(p in ('', '.', '..') for p in path.split('/')) and
            not any(c in path for c in ('\x00', '\r', '\n')) and
            PurePosixPath(path).as_posix() == path)


def candidates(entries: object) -> list[dict]:
    if not isinstance(entries, list):
        raise ValueError('Unexpected rclone inventory format')
    found = [e for e in entries if isinstance(e, dict) and
             valid_path(e.get('Path')) and
             type(e.get('Size')) is int and
             2 <= e['Size'] <= MAX_FILE_BYTES and not e.get('IsDir', False)]
    # Prefer a meaningful nontrivial overlap when a directory also has tiny files.
    return sorted(found, key=lambda e: (e['Size'] < 4096, e['Size'], e['Path']))


def run_rclone(args: list[str], seconds: int) -> subprocess.CompletedProcess:
    return subprocess.run(['rclone', *args, '--retries', '1',
                           '--low-level-retries', '1', '--log-level', 'ERROR'],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=seconds, check=False)


def probe(kind: str) -> int:
    for scope in SCOPES[kind]:
        # Do not expose private names in public log output.
        print(f'{kind}: checking a small source directory (timeout {LIST_TIMEOUT}s)',
              flush=True)
        try:
            result = run_rclone(['lsjson', scope, '--recursive', '--max-depth', '2',
                                 '--files-only', '--checkers', '1', '--tpslimit', '2'],
                                LIST_TIMEOUT)
            if result.returncode:
                print(f'{kind}: directory listing failed (exit {result.returncode}); trying next',
                      flush=True)
                continue
            eligible = candidates(json.loads(result.stdout))
            if not eligible:
                print(f'{kind}: no suitable small file here; trying next', flush=True)
                continue
            entry = eligible[0]
            n = min(PROBE_BYTES, entry['Size'] - 1)
            remote = scope.rstrip('/') + '/' + entry['Path']
            print(f'{kind}: testing two overlapping {n}-byte reads; '
                  'data remains in memory', flush=True)
            a = run_rclone(['cat', remote, '--offset', '0', '--count', str(n),
                            '--bwlimit', '512K'], READ_TIMEOUT)
            b = run_rclone(['cat', remote, '--offset', '1', '--count', str(n),
                            '--bwlimit', '512K'], READ_TIMEOUT)
            if a.returncode or b.returncode:
                print(f'{kind}: FAIL: ranged read failed '
                      f'(exit {a.returncode}/{b.returncode})', flush=True)
                return 2
            if len(a.stdout) != n or len(b.stdout) != n:
                print(f'{kind}: FAIL: wrong byte count '
                      f'({len(a.stdout)}/{len(b.stdout)} expected {n})', flush=True)
                return 2
            if a.stdout[1:] != b.stdout[:-1]:
                print(f'{kind}: FAIL: overlapping ranges disagree; '
                      'do not enable sync', flush=True)
                return 2
            if len(set(a.stdout)) < 2:
                print(f'{kind}: INCONCLUSIVE: uniform data cannot establish '
                      'offset correctness', flush=True)
                return 2
            print(f'{kind}: PASS: byte counts and overlapping offsets match '
                  '(read-only probe; full archiving not yet verified)', flush=True)
            return 0
        except (subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
            print(f'{kind}: probe scope failed ({type(exc).__name__}); trying next',
                  flush=True)
            continue
    print(f'{kind}: INCONCLUSIVE: no suitable source could be tested; '
          'do not enable sync', file=sys.stderr, flush=True)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=(*SCOPES, 'both'))
    kind = parser.parse_args().kind
    if kind == 'both':
        drive_status = probe('drive')
        photos_status = probe('photos')
        return max(drive_status, photos_status)
    return probe(kind)


if __name__ == '__main__':
    raise SystemExit(main())

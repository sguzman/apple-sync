#!/usr/bin/env python3
"""Build a separate, experimental batch-capable rclone v1.75.2 binary.

Only downloads public rclone source and Go modules, then builds locally.
No rclone config reads, Apple requests, archive writes, privilege escalation,
PATH changes, or replacement of the system rclone binary.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

RCLONE_TAG = 'v1.75.2'
RCLONE_COMMIT = 'be8d6044eacb843b74d335db0c5fbe09222031fe'
RCLONE_REPO = 'https://github.com/rclone/rclone.git'
ADDON_FILES = ('apple_sync_batch.go', 'apple_sync_batch_test.go')
BUILD_TAG = 'apple_sync_experimental'
REPO_ROOT = Path(__file__).resolve().parent.parent


class BuildError(Exception):
    """An actionable, private-data-free build error."""


def run(args: list[str], *, cwd: Path | None = None,
        timeout: int = 900, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                            check=False, timeout=timeout, env=env)
    if result.returncode:
        if result.stdout:
            print(result.stdout[-16000:], file=sys.stderr)
        if result.stderr:
            print(result.stderr[-16000:], file=sys.stderr)
        raise BuildError('COMMAND_FAILED: ' + args[0] +
                         ' (exit=' + str(result.returncode) + ')')
    return result.stdout.strip()


def checked_directory(path: Path, *, create: bool = False) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise BuildError('UNSAFE_BUILD_DIRECTORY')
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise BuildError('MISSING_BUILD_DIRECTORY')


def verify_checkout(source: Path) -> None:
    if source.is_symlink() or not (source / '.git').is_dir():
        raise BuildError('UNTRUSTED_RCLONE_CHECKOUT')
    remote = run(['git', '-C', str(source), 'remote', 'get-url', 'origin'])
    if remote not in (RCLONE_REPO, RCLONE_REPO.removesuffix('.git')):
        raise BuildError('UNEXPECTED_RCLONE_REMOTE')
    head = run(['git', '-C', str(source), 'rev-parse', 'HEAD'])
    if head != RCLONE_COMMIT:
        raise BuildError('RCLONE_COMMIT_MISMATCH')
    if run(['git', '-C', str(source), 'status', '--porcelain',
            '--untracked-files=no']):
        raise BuildError('RCLONE_SOURCE_HAS_TRACKED_CHANGES')


def build(*, source: Path, binary: Path, clone: bool = True) -> None:
    checked_directory(source.parent, create=True)
    checked_directory(binary.parent, create=True)
    if source.exists():
        verify_checkout(source)
    else:
        if not clone:
            raise BuildError('RCLONE_SOURCE_NOT_CLONED')
        print('CLONE: pinned public rclone source', flush=True)
        run(['git', 'clone', '--depth', '1', '--single-branch',
             '--branch', RCLONE_TAG, RCLONE_REPO, str(source)], timeout=600)
        verify_checkout(source)

    target = source / 'backend' / 'iclouddrive'
    checked_directory(target)
    for filename in ADDON_FILES:
        src = REPO_ROOT / 'rclone-addon' / 'iclouddrive' / filename
        dst = target / filename
        if src.is_symlink() or not src.is_file() or dst.is_symlink():
            raise BuildError('UNSAFE_ADDON_FILE')
        payload = src.read_bytes()
        if dst.exists() and (not dst.is_file() or dst.read_bytes() != payload):
            raise BuildError('EXPERIMENTAL_ADDON_ALREADY_MODIFIED')
        if not dst.exists():
            dst.write_bytes(payload)
    # gofmt only touches the two new, untracked addon files in this cache.
    for filename in ADDON_FILES:
        run(['gofmt', '-w', str(target / filename)], cwd=source)
    if binary.is_symlink() or binary.exists():
        raise BuildError('EXPERIMENTAL_BINARY_ALREADY_EXISTS')

    environment = os.environ.copy()
    environment['GOTOOLCHAIN'] = 'local'
    print('TEST: experimental Go backend only (no Apple requests)', flush=True)
    print(run(['go', 'test', '-tags', BUILD_TAG, '-count=1',
               '-run', '^TestAppleSyncBatch', './backend/iclouddrive'],
              cwd=source, env=environment), flush=True)
    print('BUILD: isolated experimental rclone binary', flush=True)
    with tempfile.TemporaryDirectory(prefix='.rclone-batch-build-',
                                     dir=binary.parent) as tmp:
        temp_binary = Path(tmp) / 'rclone-batch'
        run(['go', 'build', '-tags', BUILD_TAG,
             '-o', str(temp_binary), '.'], cwd=source, env=environment)
        metadata = temp_binary.stat()
        if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & stat.S_IXUSR:
            raise BuildError('INVALID_EXPERIMENTAL_BINARY')
        print(run([str(temp_binary), 'version'], cwd=source, timeout=15),
              flush=True)
        os.replace(temp_binary, binary)
    print('SUCCESS: standalone experimental binary built and tested', flush=True)
    print('Binary: ' + str(binary), flush=True)
    print('System rclone and Apple Sync services unchanged', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true',
                        help='fetch pinned source and build (public network only)')
    args = parser.parse_args()
    source = Path.home() / '.cache' / 'apple-sync' / 'rclone' / RCLONE_TAG
    binary = (Path.home() / '.local' / 'share' / 'apple-sync' /
              'experimental' / ('rclone-batch-' + RCLONE_TAG))
    if not args.execute:
        print('PREVIEW: pinned rclone commit ' + RCLONE_COMMIT)
        print('Source checkout: ' + str(source))
        print('Separate binary: ' + str(binary))
        print('Public GitHub and Go module downloads ONLY with --execute')
        print('No Apple requests, archive writes or system rclone replacement')
        return 0
    try:
        build(source=source, binary=binary)
    except (BuildError, OSError, subprocess.TimeoutExpired) as exc:
        print('EXPERIMENTAL BUILD FAILED: ' +
              (str(exc) if isinstance(exc, BuildError) else 'LOCAL_BUILD_ERROR'),
              file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

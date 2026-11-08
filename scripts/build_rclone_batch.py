#!/usr/bin/env python3
"""Build a separate, experimental batch-capable rclone v1.75.2 binary.

Only downloads public rclone source and Go modules, then builds locally.
No rclone config reads, Apple requests, archive writes, privilege escalation,
PATH changes, or replacement of the system rclone binary.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import signal
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
GO_TEST_TIMEOUT_SECONDS = 1800
GO_BUILD_TIMEOUT_SECONDS = 3600


class BuildError(Exception):
    """An actionable, private-data-free build error."""


def _stop_subprocess_group(process: subprocess.Popen) -> None:
    """Stop both go and its compiler children on timeout or interruption."""
    # A child compiler may outlive its go parent and keep stdout open.
    # Signal the entire group even if the group leader has already exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()


def run(args: list[str], *, cwd: Path | None = None,
        timeout: int = 90, env: dict[str, str] | None = None,
        stage: str | None = None) -> str:
    """Run a bounded command; report failures and reap child processes."""
    name = stage if stage is not None else Path(args[0]).name.upper()
    try:
        process = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True,
                                   start_new_session=True, env=env)
    except OSError as exc:
        raise BuildError(f'{name}_START_FAILED') from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_subprocess_group(process)
        raise BuildError(f'{name}_TIMED_OUT_AFTER_{timeout}_SECONDS') from None
    except BaseException:
        _stop_subprocess_group(process)
        raise
    if process.returncode:
        if stdout:
            print(stdout[-16000:], file=sys.stderr)
        if stderr:
            print(stderr[-16000:], file=sys.stderr)
        raise BuildError(f'{name}_FAILED_EXIT_{process.returncode}')
    return stdout.strip()


def checked_directory(path: Path, *, create: bool = False) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise BuildError('UNSAFE_BUILD_DIRECTORY')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise BuildError('MISSING_BUILD_DIRECTORY')


@contextmanager
def exclusive_build_lock(directory: Path):
    """Reject a second invocation before touching source or build cache."""
    checked_directory(directory, create=True)
    if directory.stat().st_uid != os.getuid():
        raise BuildError('UNTRUSTED_BUILD_LOCK_DIRECTORY')
    lock_file = directory / '.experimental-build.lock'
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        handle = os.open(lock_file, flags, 0o600)
    except OSError as exc:
        raise BuildError('BUILD_LOCK_UNAVAILABLE') from exc
    try:
        metadata = os.fstat(handle)
        if (not stat.S_ISREG(metadata.st_mode) or
                metadata.st_uid != os.getuid() or
                metadata.st_nlink != 1 or
                stat.S_IMODE(metadata.st_mode) & 0o077):
            raise BuildError('UNSAFE_BUILD_LOCK_FILE')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BuildError('BUILD_ALREADY_RUNNING_NO_CHANGES') from None
        yield
    finally:
        os.close(handle)


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


def verify_binary_identity(binary: Path) -> str:
    """Require pinned rclone version, Go module identity and build tag.

    Rclone's displayed 'go/tags' is assembled from buildinfo.Tags and does
    NOT list arbitrary Go -tags values. The Go executable's own build
    metadata (go version -m) records them exactly.
    """
    output = run([str(binary), 'version'], timeout=15,
                 stage='VERIFY_RCLONE_VERSION')
    if not output.splitlines() or output.splitlines()[0] != f'rclone {RCLONE_TAG}':
        raise BuildError('RCLONE_VERSION_MISMATCH')
    details = run(['go', 'version', '-m', str(binary)], timeout=15,
                  stage='VERIFY_GO_BINARY_METADATA')
    lines = [line.strip().split('\t') for line in details.splitlines()]
    if not any(len(fields) >= 2 and fields[0] == 'path' and
               fields[1] == 'github.com/rclone/rclone' for fields in lines):
        raise BuildError('GO_BINARY_MODULE_MISMATCH')
    tag_fields = [fields[1] for fields in lines if
                  len(fields) >= 2 and fields[0] == 'build' and
                  fields[1].startswith('-tags=')]
    if len(tag_fields) != 1 or BUILD_TAG not in tag_fields[0][len('-tags='):].split(','):
        raise BuildError('GO_BINARY_EXPERIMENTAL_TAG_MISSING')
    return output


def existing_binary_verified(binary: Path) -> bool:
    """Do not overwrite or rerun an already verified experimental binary."""
    if binary.is_symlink():
        raise BuildError('UNSAFE_EXPERIMENTAL_BINARY')
    if not binary.exists():
        return False
    metadata = binary.stat()
    if (not stat.S_ISREG(metadata.st_mode) or
            metadata.st_uid != os.getuid() or
            not metadata.st_mode & stat.S_IXUSR):
        raise BuildError('UNTRUSTED_EXPERIMENTAL_BINARY')
    try:
        verify_binary_identity(binary)
    except BuildError:
        raise BuildError('EXPERIMENTAL_BINARY_ALREADY_EXISTS_UNVERIFIED') from None
    return True


def stage_addons(source: Path) -> None:
    """Format against the repository originals; safely reuse prior staging."""
    target = source / 'backend' / 'iclouddrive'
    checked_directory(target)
    for filename in ADDON_FILES:
        src = REPO_ROOT / 'rclone-addon' / 'iclouddrive' / filename
        dst = target / filename
        if src.is_symlink() or not src.is_file() or dst.is_symlink():
            raise BuildError('UNSAFE_ADDON_FILE')
        # gofmt stdout is canonical; running gofmt on the original WITHOUT
        # -w never modifies the user's Git working tree.
        formatted = run(['gofmt', str(src)], stage='GOFMT', timeout=30) + '\n'
        expected = formatted.encode('utf-8')
        raw = src.read_bytes()
        if dst.exists():
            if not dst.is_file() or dst.read_bytes() not in (raw, expected):
                raise BuildError('EXPERIMENTAL_ADDON_ALREADY_MODIFIED')
        # Fully formatted already? No-op; raw copy from interrupted build?
        # Normalize it without changing any rclone tracked source files.
        if not dst.exists() or dst.read_bytes() != expected:
            dst.write_bytes(expected)


def build(*, source: Path, binary: Path, clone: bool = True) -> None:
    checked_directory(source.parent, create=True)
    checked_directory(binary.parent, create=True)
    if existing_binary_verified(binary):
        print('ALREADY_BUILT: verified experimental binary; no rebuild', flush=True)
        print('System rclone and Apple Sync services unchanged', flush=True)
        return
    if source.exists():
        verify_checkout(source)
    else:
        if not clone:
            raise BuildError('RCLONE_SOURCE_NOT_CLONED')
        print('CLONE: pinned public rclone source', flush=True)
        run(['git', 'clone', '--depth', '1', '--single-branch',
             '--branch', RCLONE_TAG, RCLONE_REPO, str(source)],
            timeout=600, stage='RCLONE_CLONE')
        verify_checkout(source)
    stage_addons(source)

    environment = os.environ.copy()
    environment['GOTOOLCHAIN'] = 'local'
    print('TEST: experimental Go backend only (no Apple requests)', flush=True)
    print(run(['go', 'test', '-tags', BUILD_TAG, '-count=1',
               '-run', '^TestAppleSyncBatch', './backend/iclouddrive'],
              cwd=source, env=environment, stage='GO_ADDON_TEST',
              timeout=GO_TEST_TIMEOUT_SECONDS), flush=True)
    print('BUILD: isolated experimental rclone binary (no Apple requests)',
          flush=True)
    with tempfile.TemporaryDirectory(prefix='.rclone-batch-build-',
                                     dir=binary.parent) as tmp:
        temp_binary = Path(tmp) / 'rclone-batch'
        run(['go', 'build', '-tags', BUILD_TAG,
             '-o', str(temp_binary), '.'], cwd=source, env=environment,
            stage='GO_BINARY_BUILD', timeout=GO_BUILD_TIMEOUT_SECONDS)
        metadata = temp_binary.stat()
        if (not stat.S_ISREG(metadata.st_mode) or
                not metadata.st_mode & stat.S_IXUSR):
            raise BuildError('INVALID_EXPERIMENTAL_BINARY')
        output = verify_binary_identity(temp_binary)
        print(output, flush=True)
        print('VERIFIED: pinned rclone version, Go module and experimental build tag',
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
        with exclusive_build_lock(source.parent):
            build(source=source, binary=binary)
    except BuildError as exc:
        print('EXPERIMENTAL BUILD: ' + str(exc), file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError):
        print('EXPERIMENTAL BUILD: UNEXPECTED_LOCAL_IO_ERROR', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

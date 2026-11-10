#!/usr/bin/env python3
"""Manual iCloud replay ROOT BOOTSTRAP: diagnostic-only, never executes Apple.

This file is NOT installed by the existing installer. A future *separately
authorized* root-owned manual unit may invoke:
  /usr/bin/python3 -I -S -B /usr/local/libexec/apple-sync-drive-metered/drive_batch_replay_root_bootstrap.py doctor

The only supported action is "doctor": check the isolated interpreter and
the root-owned code tree, then print a fixed, non-sensitive result. No
--execute, no service start, no ledger access, no Apple credentials, and
no replay imports/worker activation exist here. Do not reuse "doctor"
as an authorization token for network access.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import sys

LIBEXEC = Path('/usr/local/libexec/apple-sync-drive-metered')
BOOTSTRAP_NAME = 'drive_batch_replay_root_bootstrap.py'
TRUST_NAME = 'drive_batch_replay_root_trust.py'
BOOTSTRAP = LIBEXEC / BOOTSTRAP_NAME
PYTHON = Path('/usr/bin/python3')
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


class ReplayBootstrapError(Exception):
    """Fixed refusal only; never expose paths, stdout, or import errors."""


def validate_invocation(*, argv: object, uid: object, interpreter: object,
                        script: object, flags: object, prefix: object,
                        base_prefix: object) -> None:
    """Pure policy check; no filesystem IO or import.

    Explicit single 'doctor' action. Even an installed root bootstrap
    CANNOT run a worker; all other actions are permanently refused.
    """
    if argv != ('doctor',):
        raise ReplayBootstrapError('REPLAY_BOOTSTRAP_ACTION_DISABLED')
    if type(uid) is not int or uid != 0:
        raise ReplayBootstrapError('REPLAY_BOOTSTRAP_ROOT_REQUIRED')
    if (interpreter != str(PYTHON) or script != str(BOOTSTRAP)
            or prefix != base_prefix
            or getattr(flags, 'isolated', None) != 1
            or getattr(flags, 'no_site', None) != 1
            or getattr(flags, 'no_user_site', None) != 1
            or getattr(flags, 'dont_write_bytecode', None) != 1):
        raise ReplayBootstrapError('REPLAY_BOOTSTRAP_INTERPRETER_UNTRUSTED')


def _check_dir(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise ReplayBootstrapError('REPLAY_BOOTSTRAP_DIRECTORY_UNTRUSTED')


def _check_file(fd: int, *, owner: int = 0) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_nlink != 1 or info.st_size == 0):
        raise ReplayBootstrapError('REPLAY_BOOTSTRAP_CODE_UNTRUSTED')


def _verify_seed_at_fd(directory_fd: int, *, owner: int = 0) -> None:
    """Testable seed checks before importing the root verifier itself."""
    if type(owner) is not int or owner < 0:
        raise ReplayBootstrapError('REPLAY_BOOTSTRAP_OWNER_INVALID')
    # The owner override is test-only for an unprivileged temporary tree.
    info = os.fstat(directory_fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise ReplayBootstrapError('REPLAY_BOOTSTRAP_DIRECTORY_UNTRUSTED')
    for name in (BOOTSTRAP_NAME, TRUST_NAME):
        try:
            fd = os.open(name, FILE_FLAGS, dir_fd=directory_fd)
        except OSError:
            raise ReplayBootstrapError('REPLAY_BOOTSTRAP_CODE_MISSING') from None
        try:
            _check_file(fd, owner=owner)
        finally:
            os.close(fd)


def verify_installed_seed() -> None:
    """Safely walk fixed root-owned ancestors before any local import.

    All components are opened O_NOFOLLOW and held via directory fds.
    No untrusted PYTHONPATH or user checkout source is imported.
    """
    if os.geteuid() != 0:
        raise ReplayBootstrapError('REPLAY_BOOTSTRAP_ROOT_REQUIRED')
    try:
        directory_fd = os.open('/', DIR_FLAGS)
    except OSError:
        raise ReplayBootstrapError('REPLAY_BOOTSTRAP_DIRECTORY_MISSING') from None
    try:
        _check_dir(directory_fd)
        for part in LIBEXEC.parts[1:]:
            try:
                child = os.open(part, DIR_FLAGS, dir_fd=directory_fd)
            except OSError:
                raise ReplayBootstrapError(
                    'REPLAY_BOOTSTRAP_DIRECTORY_MISSING') from None
            try:
                _check_dir(child)
            except BaseException:
                os.close(child)
                raise
            os.close(directory_fd)
            directory_fd = child
        _verify_seed_at_fd(directory_fd)
    finally:
        os.close(directory_fd)


def _verify_installed_modules() -> None:
    """Only import the trusted verifier *after* the seed was checked.

    The path is on a root-owned non-writable tree, so no unprivileged
    actor can replace it after the O_NOFOLLOW fd checks.
    """
    spec = importlib.util.spec_from_file_location(
        'drive_batch_replay_root_trust', str(LIBEXEC / TRUST_NAME))
    if spec is None or spec.loader is None:
        raise ReplayBootstrapError('REPLAY_BOOTSTRAP_TRUST_IMPORT_FAILED')
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        module.verify_root_installation()
    except Exception:
        raise ReplayBootstrapError('REPLAY_BOOTSTRAP_TRUST_UNVERIFIED') from None


def main(argv: tuple[str, ...] | None = None) -> int:
    """The only executable code path is read-only 'doctor'; NEVER replay."""
    if argv is None:
        argv = tuple(sys.argv[1:])
    try:
        validate_invocation(
            argv=argv, uid=os.geteuid(), interpreter=sys.executable,
            script=__file__, flags=sys.flags,
            prefix=sys.prefix, base_prefix=sys.base_prefix)
        verify_installed_seed()
        _verify_installed_modules()
    except ReplayBootstrapError:
        print('REPLAY_ROOT_BOOTSTRAP_REFUSED', file=sys.stderr, flush=True)
        return 2
    except Exception:
        print('REPLAY_ROOT_BOOTSTRAP_REFUSED', file=sys.stderr, flush=True)
        return 2
    print('REPLAY_ROOT_BOOTSTRAP_DORMANT: ROOT_CODE_VERIFIED; '
          'NO_APPLE_ACCESS; NO_SERVICE_START', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

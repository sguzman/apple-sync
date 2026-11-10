#!/usr/bin/env python3
"""Read-only trust verifier for a FUTURE root-owned manual replay controller.

NOT an installer, command executor, or live entrypoint. The verified code
tree must be completely separate from the user's writable Git checkout.
A future *root-owned* bootstrap must run with /usr/bin/python3 -I, verify
this exact installed tree BEFORE adding it to sys.path or importing replay
modules, and refuse execution on any failed check.

All directory components are opened fd-relative O_NOFOLLOW; modules are
regular, single-link, root-owned, non-group/world-writable files. The tests
exercise the same verifier on a private temporary tree with a test UID.
No network, root mutation, shell, service, ledger or checkpoint operation.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

LIBEXEC = Path('/usr/local/libexec/apple-sync-drive-metered')

# Exact root-side imports of replay lifecycle and its dependencies.
# The worker itself is intentionally NOT copied into this privileged tree:
# it runs under the regular user's identity in a separate metered cgroup.
ROOT_REPLAY_MODULES = (
    'drive_meter_budget.py',
    'metered_drive_inventory_trial.py',
    'metered_drive_batch_replay.py',
    'drive_batch_replay_accounting.py',
    'drive_batch_replay_systemd_plan.py',
    'drive_batch_replay_systemd_adapter.py',
    'drive_batch_replay_channel.py',
    'drive_batch_replay_root_wiring.py',
    'drive_batch_replay_lifecycle.py',
    'drive_batch_replay_root_trust.py',
)
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


class ReplayRootTrustError(Exception):
    """Fixed categories only: never print privileged paths or file data."""


def _trusted_dir(fd: int, *, owner: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise ReplayRootTrustError('REPLAY_ROOT_DIRECTORY_UNTRUSTED')


def _trusted_file(fd: int, *, owner: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_nlink != 1 or info.st_size == 0):
        raise ReplayRootTrustError('REPLAY_ROOT_MODULE_UNTRUSTED')


def _verify_modules_in_fd(directory_fd: int, *,
                          owner: int, modules: tuple[str, ...]) -> None:
    """Testable fd-relative verifier; never import untrusted source code."""
    _trusted_dir(directory_fd, owner=owner)
    if (type(modules) is not tuple or len(modules) == 0
            or len(set(modules)) != len(modules)):
        raise ReplayRootTrustError('REPLAY_ROOT_MANIFEST_INVALID')
    for name in modules:
        if (type(name) is not str or not name.isascii()
                or not name.endswith('.py') or name in ('.py', '..py')
                or not name.replace('_', '').replace('.', '').isalnum()
                or '/' in name or '\\' in name):
            raise ReplayRootTrustError('REPLAY_ROOT_MANIFEST_INVALID')
    for name in modules:
        try:
            fd = os.open(name, FILE_FLAGS, dir_fd=directory_fd)
        except OSError:
            raise ReplayRootTrustError('REPLAY_ROOT_MODULE_MISSING') from None
        try:
            _trusted_file(fd, owner=owner)
        finally:
            os.close(fd)


def verify_root_installation() -> None:
    """Future root bootstrap calls before importing any replay module.

    This function itself must be included in a root-owned bootstrap chain
    with -I, not first imported from the writable checkout as root.
    """
    if os.geteuid() != 0:
        raise ReplayRootTrustError('REPLAY_ROOT_REQUIRED')
    if not LIBEXEC.is_absolute() or LIBEXEC.parts[:2] != ('/', 'usr'):
        raise ReplayRootTrustError('REPLAY_ROOT_INSTALL_PATH_INVALID')
    try:
        parent_fd = os.open('/', DIR_FLAGS)
    except OSError:
        raise ReplayRootTrustError('REPLAY_ROOT_DIRECTORY_UNAVAILABLE') from None
    try:
        _trusted_dir(parent_fd, owner=0)
        for part in LIBEXEC.parts[1:]:
            try:
                fd = os.open(part, DIR_FLAGS, dir_fd=parent_fd)
            except OSError:
                raise ReplayRootTrustError(
                    'REPLAY_ROOT_DIRECTORY_UNAVAILABLE') from None
            try:
                _trusted_dir(fd, owner=0)
            except BaseException:
                os.close(fd)
                raise
            os.close(parent_fd)
            parent_fd = fd
        _verify_modules_in_fd(parent_fd, owner=0,
                              modules=ROOT_REPLAY_MODULES)
    finally:
        os.close(parent_fd)

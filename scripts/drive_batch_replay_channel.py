#!/usr/bin/env python3
"""Dormant secure receipt-channel lifecycle for a future root replay wrapper.

No service, Apple, ledger, credential, shell or subprocess operations. The
public allocator is root-only and opens fixed /run/user/<uid> components using
O_DIRECTORY|O_NOFOLLOW and fstat. Tests use _create_at_fd with a temporary,
user-owned runtime directory. The lease intentionally DOES NOT auto-delete
on close: cleanup requires explicit confirmation that the worker was stopped.

An unconfirmed worker or unexpected child leaves the channel intact; never
delete arbitrary data to make cleanup succeed. /run/user is volatile.
"""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import secrets
import stat

import drive_batch_replay_systemd_plan as plan
from metered_drive_inventory_trial import USER

RESULT_NAME = 'replay.json'
RUNTIME_ROOT = Path('/run/user')
O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class ReplayChannelError(Exception):
    """Fixed privacy-safe error, no caller paths or metadata."""


def _require_directory(fd: int, *, uid: int, private: bool) -> None:
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid
            or (mode != 0o700 if private else bool(mode & 0o022))):
        raise ReplayChannelError('REPLAY_RUNTIME_DIRECTORY_UNTRUSTED')


def _open_runtime(uid: int) -> int:
    """Hold trusted directory fds while walking the fixed runtime path."""
    if type(uid) is not int or uid <= 0:
        raise ReplayChannelError('REPLAY_RUNTIME_ID_INVALID')
    try:
        root = os.open('/run', O_DIR)
        try:
            _require_directory(root, uid=0, private=False)
            user_root = os.open('user', O_DIR, dir_fd=root)
            try:
                _require_directory(user_root, uid=0, private=False)
                runtime = os.open(str(uid), O_DIR, dir_fd=user_root)
                try:
                    _require_directory(runtime, uid=uid, private=True)
                except BaseException:
                    os.close(runtime)
                    raise
                return runtime
            finally:
                os.close(user_root)
        finally:
            os.close(root)
    except ReplayChannelError:
        raise
    except OSError:
        raise ReplayChannelError('REPLAY_RUNTIME_DIRECTORY_UNAVAILABLE') from None


class ReplayChannelLease:
    """Owns descriptor-bound channel; explicit cleanup needs stopped proof."""

    def __init__(self, *, runtime_fd: int, channel_fd: int,
                 path: Path, name: str, uid: int):
        self.runtime_fd = runtime_fd
        self.channel_fd = channel_fd
        self.path = path
        self.name = name
        self.uid = uid
        entry = os.fstat(channel_fd)
        self.device = entry.st_dev
        self.inode = entry.st_ino
        self.closed = False

    def _same_entry(self) -> bool:
        try:
            info = os.stat(self.name, dir_fd=self.runtime_fd,
                           follow_symlinks=False)
        except OSError:
            return False
        return (stat.S_ISDIR(info.st_mode) and info.st_uid == self.uid
                and stat.S_IMODE(info.st_mode) == 0o700
                and (info.st_dev, info.st_ino) ==
                (self.device, self.inode))

    def cleanup_after_stop(self, *, stopped_verified: bool) -> None:
        """Remove only this directory and its one permitted private receipt.

        An interrupted worker has no proven stop: preserve channel and
        pending ledger so an operator can reconcile; never auto-erase.
        """
        if self.closed:
            raise ReplayChannelError('REPLAY_CHANNEL_CLOSED')
        if stopped_verified is not True:
            raise ReplayChannelError('REPLAY_STOP_UNCONFIRMED')
        if not self._same_entry():
            raise ReplayChannelError('REPLAY_CHANNEL_CHANGED')
        try:
            children = os.listdir(self.channel_fd)
        except OSError:
            raise ReplayChannelError('REPLAY_CHANNEL_INSPECTION_FAILED') from None
        if len(children) > 1 or any(child != RESULT_NAME
                                   for child in children):
            raise ReplayChannelError('REPLAY_CHANNEL_UNEXPECTED_CONTENTS')
        if children:
            try:
                info = os.stat(RESULT_NAME, dir_fd=self.channel_fd,
                               follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode)
                        or info.st_uid != self.uid
                        or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1):
                    raise ReplayChannelError('REPLAY_RECEIPT_UNTRUSTED')
                os.unlink(RESULT_NAME, dir_fd=self.channel_fd)
            except ReplayChannelError:
                raise
            except OSError:
                raise ReplayChannelError('REPLAY_RECEIPT_CLEANUP_FAILED') from None
        if not self._same_entry():
            raise ReplayChannelError('REPLAY_CHANNEL_CHANGED')
        try:
            os.rmdir(self.name, dir_fd=self.runtime_fd)
        except OSError:
            raise ReplayChannelError('REPLAY_CHANNEL_CLEANUP_FAILED') from None
        self.close()

    def close(self) -> None:
        """Release descriptors only; never mistake closing for safe cleanup."""
        if not self.closed:
            os.close(self.channel_fd)
            os.close(self.runtime_fd)
            self.closed = True


def _create_at_fd(runtime_fd: int, *, uid: int, gid: int,
                  runtime_path: Path) -> ReplayChannelLease:
    """Testable fd-relative allocator; caller must supply trusted runtime fd."""
    if (type(uid) is not int or uid <= 0 or type(gid) is not int
            or gid < 0 or not isinstance(runtime_path, Path)
            or not runtime_path.is_absolute()):
        raise ReplayChannelError('REPLAY_RUNTIME_ID_INVALID')
    try:
        retained = os.dup(runtime_fd)
        os.set_inheritable(retained, False)
        _require_directory(retained, uid=uid, private=True)
    except ReplayChannelError:
        if 'retained' in locals():
            os.close(retained)
        raise
    except OSError:
        if 'retained' in locals():
            os.close(retained)
        raise ReplayChannelError('REPLAY_RUNTIME_DIRECTORY_UNAVAILABLE') from None

    name = plan.CHANNEL_PREFIX + secrets.token_urlsafe(24)
    if not plan.CHANNEL_NAME.fullmatch(name):
        os.close(retained)
        raise ReplayChannelError('REPLAY_CHANNEL_NAME_INVALID')
    created = False
    opened = None
    completed = False
    try:
        os.mkdir(name, mode=0o700, dir_fd=retained)
        created = True
        opened = os.open(name, O_DIR, dir_fd=retained)
        os.fchown(opened, uid, gid)
        os.fchmod(opened, 0o700)
        _require_directory(opened, uid=uid, private=True)
        lease = ReplayChannelLease(
            runtime_fd=retained, channel_fd=opened,
            path=runtime_path / name, name=name, uid=uid)
        if not lease._same_entry():
            raise ReplayChannelError('REPLAY_CHANNEL_CHANGED')
        completed = True
        return lease
    except ReplayChannelError:
        raise
    except OSError:
        raise ReplayChannelError('REPLAY_CHANNEL_CREATION_FAILED') from None
    finally:
        # Only a fully verified, returned lease takes fd ownership.
        if not completed:
            if opened is not None:
                os.close(opened)
            if created:
                try:
                    os.rmdir(name, dir_fd=retained)
                except OSError:
                    pass
            os.close(retained)


def allocate_root_channel() -> ReplayChannelLease:
    """ONLY for future trusted root entrypoint; no direct invocation here."""
    if os.geteuid() != 0:
        raise ReplayChannelError('REPLAY_ROOT_SERVICE_REQUIRED')
    try:
        account = pwd.getpwnam(USER)
        if account.pw_uid <= 0 or account.pw_gid < 0:
            raise ReplayChannelError('REPLAY_RUNTIME_ID_INVALID')
    except KeyError:
        raise ReplayChannelError('REPLAY_RUNTIME_ACCOUNT_UNAVAILABLE') from None
    runtime = _open_runtime(account.pw_uid)
    try:
        return _create_at_fd(
            runtime, uid=account.pw_uid, gid=account.pw_gid,
            runtime_path=RUNTIME_ROOT / str(account.pw_uid))
    finally:
        os.close(runtime)

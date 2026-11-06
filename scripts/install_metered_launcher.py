#!/usr/bin/env python3
"""One-time explicitly authorized installer for manual metered inventory.

Runs under pkexec. Copies privileged controller code into a root-owned
directory, then installs a static system service and start-only Polkit rule.
No timers are installed, enabled, or changed. Does not launch a scan.
"""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile

USER = 'sguzman'
SOURCE = Path('/home/sguzman/Code/Sync/apple-sync')
LIBEXEC = Path('/usr/local/libexec/apple-sync-drive-metered')
UNIT = Path('/etc/systemd/system/apple-sync-drive-metered-manual.service')
RULE = Path('/etc/polkit-1/rules.d/49-apple-sync-drive-metered.rules')
COPIES = (
    ('scripts/metered_drive_inventory_trial.py',
     LIBEXEC / 'metered_drive_inventory_trial.py'),
    ('scripts/drive_meter_budget.py', LIBEXEC / 'drive_meter_budget.py'),
    ('packaging/apple-sync-drive-metered-manual.service', UNIT),
    ('packaging/49-apple-sync-drive-metered.rules', RULE),
)


class InstallationError(Exception):
    """Sanitized administrative install refusal."""


def check_trusted_directory(directory: Path, *, create: bool = False) -> None:
    """Refuse symlinked, non-root-owned or group/world-writable ancestors."""
    for path in reversed((directory, *directory.parents)):
        if not path.exists() and create:
            path.mkdir(mode=0o755)
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or
                info.st_mode & 0o022):
            raise InstallationError('UNSAFE_INSTALL_DIRECTORY')


def atomic_install(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise InstallationError('MISSING_OR_UNSAFE_INSTALL_SOURCE')
    check_trusted_directory(destination.parent, create=True)
    if destination.exists() or destination.is_symlink():
        old = destination.lstat()
        if not (stat.S_ISREG(old.st_mode) and old.st_uid == 0 and
                not old.st_mode & 0o022):
            raise InstallationError('UNSAFE_EXISTING_INSTALL_FILE')
    fd, tmp = tempfile.mkstemp(prefix='.apple-sync-install-',
                                dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as output, source.open('rb') as inp:
            os.fchmod(output.fileno(), 0o644)
            shutil.copyfileobj(inp, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, destination)
        directory_fd = os.open(destination.parent,
                               os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def install() -> None:
    if os.geteuid() != 0:
        raise InstallationError('ADMIN_AUTHORIZATION_REQUIRED')
    user = pwd.getpwnam(USER)
    if os.environ.get('PKEXEC_UID') != str(user.pw_uid):
        raise InstallationError('PKEXEC_MUST_BE_INVOKED_BY_ACCOUNT_OWNER')
    if SOURCE.is_symlink() or not SOURCE.is_dir():
        raise InstallationError('SOURCE_REPOSITORY_UNAVAILABLE')
    if not shutil.which('systemctl') or not shutil.which('runuser'):
        raise InstallationError('SYSTEMD_OR_RUNUSER_MISSING')
    state = subprocess.run(
        ['systemctl', 'show', UNIT.name, '--property=ActiveState', '--value'],
        check=False, capture_output=True, text=True, timeout=10)
    if state.returncode == 0 and state.stdout.strip() in ('active', 'activating'):
        raise InstallationError('METERED_SERVICE_ALREADY_RUNNING')
    for rel, dest in COPIES:
        atomic_install(SOURCE / rel, dest)
    subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=20)
    enabled = subprocess.run(
        ['systemctl', 'is-enabled', UNIT.name],
        capture_output=True, text=True, check=False, timeout=10)
    if enabled.stdout.strip() in (
            'enabled', 'enabled-runtime', 'linked', 'linked-runtime'):
        raise InstallationError('MANUAL_SERVICE_UNEXPECTEDLY_ENABLED')
    print('MANUAL_METERED_LAUNCHER_INSTALLED: static service, not enabled')
    print('Polkit permission: active local user may start this one service')
    print('No inventory scan, backup job, or timer was started or changed')


def main() -> int:
    try:
        install()
        return 0
    except (InstallationError, OSError, subprocess.SubprocessError, KeyError):
        print('METERED_INSTALL_FAILED: authorization or configuration problem',
              file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

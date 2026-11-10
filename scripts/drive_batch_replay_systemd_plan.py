#!/usr/bin/env python3
"""Pure systemd argv plans for an UNINSTALLED iCloud replay worker.

No subprocess, service start, Apple call, ledger operation or live CLI.
The worker is still DORMANT and ignores --execute. The argv only names
root-owned installed code, never the writable Git checkout; those files
are not installed by this change. A future root-owned wrapper must verify
receipt directory ownership, mode, absence of
symlinks and systemd enforcement BEFORE using any returned argv.
IPAccounting provides a sampled soft limit, NOT a hard network quota.
"""
from __future__ import annotations

from pathlib import Path
import re

import drive_batch_replay_accounting as accounting
import drive_batch_replay_root_trust as root_trust
import metered_drive_inventory_trial as baseline

USER = baseline.USER
WORKER_ROOT = root_trust.LIBEXEC
WORKER = WORKER_ROOT / 'drive_batch_replay_worker.py'
PYTHON = '/usr/bin/python3'
RUNTIME_SECONDS = accounting.MAX_SECONDS + 10
CHANNEL_PREFIX = '.apple-sync-replay-'
CHANNEL_NAME = re.compile(r'\.apple-sync-replay-[A-Za-z0-9_-]{8,64}\Z')
PREFLIGHT_PATTERN = re.compile(
    r'apple-sync-ipaccounting-replay-check-[1-9][0-9]*\.service\Z'
)
PROPERTY_FIELDS = (
    'ActiveState', 'SubState', 'ExecMainStatus',
    'Result', 'IPAccounting', 'IPIngressBytes', 'IPEgressBytes',
)


class ReplaySystemdPlanError(Exception):
    """Fixed failure reason, never a private path or identifier."""


def unit_name(pid: int) -> str:
    """A unique per-process replay unit, never a user-supplied service."""
    if type(pid) is not int or not 1 <= pid < 2**31:
        raise ReplaySystemdPlanError('REPLAY_UNIT_ID_INVALID')
    unit = f'apple-sync-drive-metered-replay-{pid}.service'
    if not accounting.UNIT_PATTERN.fullmatch(unit):
        raise ReplaySystemdPlanError('REPLAY_UNIT_UNTRUSTED')
    return unit


def _valid_unit(unit: object) -> str:
    if type(unit) is not str or not accounting.UNIT_PATTERN.fullmatch(unit):
        raise ReplaySystemdPlanError('REPLAY_UNIT_UNTRUSTED')
    return unit


def _valid_uid(uid: object) -> int:
    if type(uid) is not int or not 1 <= uid < 2**31:
        raise ReplaySystemdPlanError('REPLAY_RUNTIME_UID_INVALID')
    return uid


def _channel(receipt_dir: object, uid: int) -> str:
    """Check exact runtime shape; actual filesystem trust is NOT proven."""
    _valid_uid(uid)
    if (not isinstance(receipt_dir, Path)
            or not receipt_dir.is_absolute()
            or receipt_dir.parent != Path('/run/user') / str(uid)
            or not CHANNEL_NAME.fullmatch(receipt_dir.name)):
        raise ReplaySystemdPlanError('REPLAY_CHANNEL_PATH_INVALID')
    return str(receipt_dir)


def worker_start_argv(unit: str, *, uid: int,
                      receipt_dir: Path) -> tuple[str, ...]:
    """Construct argv only; future caller must use shell=False.

    Root must independently verify the private runtime channel before
    calling the eventual launcher. This function performs no filesystem IO.
    """
    _valid_unit(unit)
    _valid_uid(uid)
    receipt = _channel(receipt_dir, uid)
    runtime = f'/run/user/{uid}'
    return (
        'systemd-run', '--system', '--quiet', f'--unit={unit}',
        '--property=Type=exec',
        '--property=IPAccounting=yes',
        '--property=RemainAfterExit=yes',
        f'--property=RuntimeMaxSec={RUNTIME_SECONDS}s',
        '--property=TimeoutStopSec=5s',
        '--property=KillMode=control-group',
        '--property=Restart=no',
        '--property=Nice=15',
        '--property=IOSchedulingClass=idle',
        '--property=NoNewPrivileges=yes',
        f'--property=User={USER}',
        f'--property=WorkingDirectory={WORKER_ROOT}',
        f'--setenv=HOME=/home/{USER}',
        f'--setenv=XDG_RUNTIME_DIR={runtime}',
        '--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path='
        + runtime + '/bus',
        '--setenv=APPLE_SYNC_REPLAY_METERED=1',
        f'--setenv=APPLE_SYNC_REPLAY_UNIT={unit}',
        f'--setenv=APPLE_SYNC_REPLAY_RECEIPT_DIR={receipt}',
        '--property=StandardOutput=journal',
        '--property=StandardError=journal',
        PYTHON, '-B', str(WORKER), '--execute',
    )


def properties_argv(unit: str) -> tuple[str, ...]:
    """Request only fields used to certify stopped IP-accounted worker."""
    _valid_unit(unit)
    args = ['systemctl', '--system', 'show', unit]
    for name in PROPERTY_FIELDS:
        args.extend(('-p', name))
    return tuple(args)


def stop_argv(unit: str) -> tuple[str, ...]:
    """Stop the whole transient cgroup, never arbitrary service names."""
    _valid_unit(unit)
    return ('systemctl', '--system', 'stop', unit)


def preflight_unit_name(pid: int) -> str:
    """Separate fixed namespace for a zero-traffic accounting preflight."""
    if type(pid) is not int or not 1 <= pid < 2**31:
        raise ReplaySystemdPlanError('REPLAY_PREFLIGHT_UNIT_INVALID')
    unit = f'apple-sync-ipaccounting-replay-check-{pid}.service'
    if not PREFLIGHT_PATTERN.fullmatch(unit):
        raise ReplaySystemdPlanError('REPLAY_PREFLIGHT_UNIT_INVALID')
    return unit


def preflight_start_argv(pid: int) -> tuple[str, ...]:
    """Only a harmless sleep, for future zero-traffic IP preflight."""
    unit = preflight_unit_name(pid)
    return (
        'systemd-run', '--system', '--quiet', f'--unit={unit}',
        '--property=Type=exec',
        '--property=IPAccounting=yes',
        '--property=RemainAfterExit=yes',
        '--property=RuntimeMaxSec=10s',
        '--property=TimeoutStopSec=5s',
        '--property=KillMode=control-group',
        '--property=Restart=no',
        '/usr/bin/sleep', '4',
    )


def preflight_properties_argv(pid: int) -> tuple[str, ...]:
    """Read required IP counters before accepting an accounting preflight."""
    unit = preflight_unit_name(pid)
    args = ['systemctl', '--system', 'show', unit]
    for name in PROPERTY_FIELDS:
        args.extend(('-p', name))
    return tuple(args)


def preflight_stop_argv(pid: int) -> tuple[str, ...]:
    """The future root wrapper must stop+verify this unit in a finally."""
    return ('systemctl', '--system', 'stop', preflight_unit_name(pid))

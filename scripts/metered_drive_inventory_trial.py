#!/usr/bin/env python3
"""One-shot, privileged, fail-closed network accounting for Apple Drive inventory.

Launch a TRANSIENT system service as the regular user with IPAccounting=yes.
Use another independent controller process to poll ingress+egress, terminate
the entire service cgroup at the soft budget threshold, then show the last
measurement. No timer or permanent system unit is installed.

IMPORTANT: systemd counters are sampled. There is no exact hardware/network
byte quota, so bursts may overshoot the threshold. Only use for a short trial.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import time

USER = 'sguzman'
REPO = Path('/home/sguzman/Code/Sync/apple-sync')
WORKER = REPO / 'scripts' / 'drive_inventory.py'
ARCHIVE = Path('/drive')
MAX_WALL_SECONDS = 90
MAX_BYTES = 10_000_000
# Stop before 10 MB to leave room for polling delay. Not a guaranteed cap.
STOP_BYTES = 8_000_000
POLL_SECONDS = 0.25
PROPS = ('ActiveState', 'SubState', 'IPAccounting',
         'IPIngressBytes', 'IPEgressBytes')
BUSY_UNITS = (
    'google-drive-rclone.service',
    'apple-sync-photos.service',
    'apple-sync-photos-progress.service',
    'apple-sync-photos-inventory.service',
)


class TrialError(Exception):
    """Sanitized failure for a cautious root-run trial."""


def run(command: list[str], *, timeout: float = 12,
        check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=timeout, check=False)
    if check and result.returncode:
        raise TrialError('SYSTEMD_COMMAND_FAILED')
    return result


def props(unit: str) -> dict[str, str]:
    args = ['systemctl', '--system', 'show', unit]
    for prop in PROPS:
        args += ['-p', prop]
    result = run(args, timeout=6)
    fields = {}
    for line in result.stdout.splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            fields[key] = value
    return fields


def accounted_bytes(fields: dict[str, str]) -> tuple[int, int]:
    if fields.get('IPAccounting') != 'yes':
        raise TrialError('IP_ACCOUNTING_DISABLED')
    values = []
    for key in ('IPIngressBytes', 'IPEgressBytes'):
        raw = fields.get(key, '')
        if not re.fullmatch(r'[0-9]{1,20}', raw):
            raise TrialError('IP_COUNTER_UNAVAILABLE')
        number = int(raw)
        if number > 2**64 - 1:
            raise TrialError('IP_COUNTER_INVALID')
        values.append(number)
    return values[0], values[1]


def start(unit: str, args: list[str], *, with_user: bool,
          runtime_seconds: int) -> None:
    command = [
        'systemd-run', '--system', '--quiet',
        '--unit=' + unit,
        '--property=Type=exec',
        '--property=IPAccounting=yes',
        '--property=RemainAfterExit=yes',
        '--property=RuntimeMaxSec=' + str(runtime_seconds) + 's',
        '--property=TimeoutStopSec=5s',
        '--property=KillMode=control-group',
        '--property=Nice=15',
        '--property=IOSchedulingClass=idle',
        '--property=NoNewPrivileges=yes',
    ]
    if with_user:
        user = pwd.getpwnam(USER)
        command += [
            '--property=User=' + USER,
            '--property=WorkingDirectory=' + str(REPO),
            '--setenv=HOME=' + user.pw_dir,
            '--setenv=XDG_RUNTIME_DIR=/run/user/' + str(user.pw_uid),
            '--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'
            + str(user.pw_uid) + '/bus',
        ]
    run(command + args, timeout=15)


def stop(unit: str) -> None:
    # Best effort even if the service already exited; never keep scanning
    # merely because the controller hit an exception.
    run(['systemctl', '--system', 'stop', unit], timeout=15, check=False)
    run(['systemctl', '--system', 'reset-failed', unit],
        timeout=5, check=False)


def guard_setup() -> None:
    if os.geteuid() != 0:
        raise TrialError('ROOT_REQUIRED')
    if not WORKER.is_file():
        raise TrialError('WORKER_NOT_FOUND')
    if not ARCHIVE.is_dir():
        raise TrialError('ARCHIVE_MISSING')
    if run(['mountpoint', '-q', str(ARCHIVE)], check=False).returncode:
        raise TrialError('ARCHIVE_NOT_MOUNTED')
    user = pwd.getpwnam(USER)
    if user.pw_uid == 0:
        raise TrialError('UNSAFE_SERVICE_USER')
    # Explicitly reject a still-active user timer before any new network.
    # This one-time measurement is NOT permission to resume automation.
    user_env = {'XDG_RUNTIME_DIR': f'/run/user/{user.pw_uid}'}
    command = ['runuser', '-u', USER, '--', 'env',
               'XDG_RUNTIME_DIR=' + user_env['XDG_RUNTIME_DIR'],
               'systemctl', '--user', 'is-active',
               'apple-sync-drive-inventory.timer']
    check = run(command, timeout=8, check=False)
    state = check.stdout.strip()
    if check.returncode == 0 and state == 'active':
        raise TrialError('USER_INVENTORY_TIMER_STILL_ACTIVE')
    # A disabled/unlinked timer can appear as inactive (3) or
    # unknown/not-found (4) depending on whether it remains loaded.
    if not (check.returncode in (3, 4)
            and state in ('inactive', 'unknown', 'failed')):
        raise TrialError('CANNOT_VALIDATE_USER_TIMER_STATE')


def preflight(unit: str) -> None:
    print('Accounting preflight: no Apple traffic', flush=True)
    try:
        start(unit, ['/usr/bin/sleep', '4'],
              with_user=False, runtime_seconds=10)
        time.sleep(0.35)
        data = props(unit)
        ingress, egress = accounted_bytes(data)
        if data.get('ActiveState') not in ('active',):
            raise TrialError('PREFLIGHT_NOT_ACTIVE')
        print('Accounting preflight OK: '
              f'ingress={ingress} bytes, egress={egress} bytes', flush=True)
    finally:
        stop(unit)


def worker_entry() -> int:
    """Executed inside the transient system service as the regular user.

    Distinguish a genuinely active backup from an inaccessible user manager
    before allowing the original inventory worker to run unchanged.
    """
    for unit in BUSY_UNITS:
        result = run(
            ['systemctl', '--user', 'show', unit,
             '--property=ActiveState', '--value', '--no-pager'],
            timeout=8, check=False)
        state = result.stdout.strip()
        if result.returncode != 0 or state not in ('inactive', 'failed', 'active',
                                                    'activating', 'deactivating'):
            print(f'TRIAL_BLOCKED: USER_MANAGER_STATUS_UNAVAILABLE {unit}',
                  flush=True)
            return 3
        print(f'TRIAL_BACKUP_STATE: {unit}={state}', flush=True)
        if state not in ('inactive', 'failed'):
            print(f'TRIAL_BLOCKED: OTHER_BACKUP_ACTIVE {unit}', flush=True)
            return 3
    print('TRIAL_USER_MANAGER_OK: proceeding with original backup locks',
          flush=True)
    os.execv('/usr/bin/python3',
             ['/usr/bin/python3', str(WORKER), '--execute', '--resume-only'])
    return 0


def report_trial_service_log(unit: str) -> None:
    """Relay only sanitized worker status; never print private filenames."""
    result = run(
        ['journalctl', '--system', '-u', unit, '-o', 'cat',
         '--no-pager', '-n', '30'],
        timeout=8, check=False)
    if result.returncode:
        print('TRIAL_WORKER_LOG: unavailable; inspect with journalctl',
              flush=True)
        return
    for line in result.stdout.splitlines():
        if line.startswith(('TRIAL_', 'Drive inventory ')):
            print(line, flush=True)


def trial(unit: str) -> None:
    # Launch a same-context user-manager check, then exec the original
    # resumable inventory. This prevents silent zero-byte deferrals.
    args = ['/usr/bin/python3', str(REPO / 'scripts'
            / 'metered_drive_inventory_trial.py'), '--worker-entry']
    largest_ingress = largest_egress = 0
    reason = 'TIME_LIMIT'
    began = time.monotonic()
    try:
        start(unit, args, with_user=True,
              runtime_seconds=MAX_WALL_SECONDS + 10)
        while True:
            data = props(unit)
            ingress, egress = accounted_bytes(data)
            largest_ingress = max(largest_ingress, ingress)
            largest_egress = max(largest_egress, egress)
            total = ingress + egress
            if total >= STOP_BYTES:
                reason = 'SOFT_BUDGET_REACHED'
                break
            if data.get('SubState') in ('exited', 'dead', 'failed'):
                reason = 'WORKER_FINISHED'
                break
            if data.get('ActiveState') != 'active':
                reason = 'WORKER_NOT_ACTIVE'
                break
            if time.monotonic() - began >= MAX_WALL_SECONDS:
                reason = 'TIME_LIMIT'
                break
            time.sleep(POLL_SECONDS)
        # Stop the unit, and take a last sample if possible; counters may
        # vanish after the system service is unloaded.
        print(f'Trial end reason: {reason}', flush=True)
    finally:
        try:
            data = props(unit)
            ingress, egress = accounted_bytes(data)
            largest_ingress = max(largest_ingress, ingress)
            largest_egress = max(largest_egress, egress)
        except (TrialError, subprocess.TimeoutExpired, OSError):
            pass
        report_trial_service_log(unit)
        stop(unit)
        print(f'MEASURED IP INGRESS: {largest_ingress} bytes', flush=True)
        print(f'MEASURED IP EGRESS: {largest_egress} bytes', flush=True)
        print(f'MEASURED IP TOTAL: '
              f'{largest_ingress + largest_egress} bytes', flush=True)
        print('Budget: 10,000,000 bytes; polling stop at 8,000,000 bytes. '
              'Counter is sampled, not a strict quota; brief overshoot '
              'is possible.', flush=True)
        print('Automatic iCloud Drive inventory timer was NOT enabled.',
              flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true',
                        help='perform one short system-accounted inventory trial')
    parser.add_argument('--worker-entry', action='store_true',
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_entry:
        return worker_entry()
    if not args.execute:
        print('PREVIEW: one transient user-owned system service, '
              'IPAccounting=yes, 90s runtime, 8MB sampled cutoff; '
              '10MB advisory budget. No timer enabled. '
              'Pass --execute to actually start.', flush=True)
        return 0
    nonce = str(os.getpid())
    try:
        guard_setup()
        preflight('apple-sync-ipaccounting-check-' + nonce + '.service')
        print('Starting one metered iCloud Drive inventory trial; '
              'remaining scan checkpoint preserved', flush=True)
        trial('apple-sync-drive-metered-' + nonce + '.service')
    except (TrialError, OSError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, TrialError):
            print('METERED TRIAL BLOCKED: ' + str(exc), file=sys.stderr,
                  flush=True)
        else:
            print('METERED TRIAL FAILED: controller or systemd error; '
                  'no further runs scheduled', file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

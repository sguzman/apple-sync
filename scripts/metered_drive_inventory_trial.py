#!/usr/bin/env python3
"""Explicit, persistent-budget network accounting for Apple Drive inventory.

Launch one or more TRANSIENT system services as the regular user with
IPAccounting=yes. A privileged controller polls ingress+egress and stops the
whole service cgroup at a soft per-pass threshold. A private, root-owned
ledger accounts for those measurements across invocations; interrupted runs
block further work until reconciled. No timer or permanent unit is installed.

IMPORTANT: systemd counters are sampled, NOT a hard network quota. The
100 MB cumulative target can be exceeded by short bursts.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from drive_meter_budget import (BudgetError, BudgetLedger, CAP_BYTES,
                                RESERVE_BYTES)

USER = 'sguzman'
REPO = Path('/home/sguzman/Code/Sync/apple-sync')
WORKER = REPO / 'scripts' / 'drive_inventory.py'
ARCHIVE = Path('/drive')
MAX_WALL_SECONDS = 90
MAX_BYTES = 10_000_000
# Stop before 10 MB to leave room for polling delay. Not a guaranteed cap.
STOP_BYTES = 8_000_000
POLL_SECONDS = 0.25
IDLE_WAIT_SECONDS = 12 * 60
IDLE_STABLE_SECONDS = 5
IDLE_POLL_SECONDS = 5
PROPS = ('ActiveState', 'SubState', 'ExecMainStatus', 'Result',
         'IPAccounting', 'IPIngressBytes', 'IPEgressBytes')
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
    # A failed stop must leave the durable ledger pending. Otherwise the
    # controller might record a completed run while systemd keeps scanning.
    response = run(['systemctl', '--system', 'stop', unit],
                   timeout=15, check=False)
    if response.returncode:
        raise TrialError('SYSTEMD_STOP_UNCONFIRMED')
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


def user_unit_state(unit: str) -> str:
    """Read the user's unit state without altering any services."""
    user = pwd.getpwnam(USER)
    command = [
        'runuser', '-u', USER, '--', 'env',
        f'XDG_RUNTIME_DIR=/run/user/{user.pw_uid}',
        f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{user.pw_uid}/bus',
        'systemctl', '--user', 'show', unit,
        '--property=ActiveState', '--value', '--no-pager',
    ]
    result = run(command, timeout=8, check=False)
    state = result.stdout.strip()
    if result.returncode != 0 or state not in (
            'inactive', 'failed', 'active', 'activating', 'deactivating'):
        raise TrialError('USER_MANAGER_STATUS_UNAVAILABLE')
    return state


def wait_for_idle_window(deadline: float) -> bool:
    """Wait for a stable idle interval, before reserving a metered pass."""
    idle_since = None
    announced = None
    while True:
        blocked = None
        for unit in BUSY_UNITS:
            if user_unit_state(unit) not in ('inactive', 'failed'):
                blocked = unit
                break
        now = time.monotonic()
        if blocked is None:
            if idle_since is None:
                idle_since = now
            if now - idle_since >= IDLE_STABLE_SECONDS:
                print('TRIAL_IDLE_WINDOW_READY: backups idle for 5 seconds',
                      flush=True)
                return True
        else:
            idle_since = None
            if blocked != announced:
                print(f'TRIAL_WAITING_FOR_BACKUP: {blocked}', flush=True)
                announced = blocked
        if now >= deadline:
            print('TRIAL_IDLE_WAIT_EXPIRED: no stable idle window; '
                  'no new inventory pass launched', flush=True)
            return False
        time.sleep(min(IDLE_POLL_SECONDS, deadline - now))


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


def worker_entry(*, persistent_rc: bool = False) -> int:
    """Executed inside the transient system service as the regular user.

    Distinguish a genuinely active backup from an inaccessible user manager
    before allowing the original inventory worker to run unchanged.
    """
    for unit in BUSY_UNITS:
        try:
            result = run(
                ['systemctl', '--user', 'show', unit,
                 '--property=ActiveState', '--value', '--no-pager'],
                timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired):
            print(f'TRIAL_BLOCKED: USER_MANAGER_STATUS_UNAVAILABLE {unit}',
                  flush=True)
            return 3
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
    args = ['/usr/bin/python3', str(WORKER), '--execute', '--resume-only']
    if persistent_rc:
        args.append('--persistent-rc')
    os.execv('/usr/bin/python3', args)
    return 0


def report_trial_service_log(unit: str) -> None:
    """Relay only sanitized worker status; never print private filenames."""
    # Short-lived failed workers can otherwise be queried before journald
    # has written their final status to the journal.
    try:
        run(['journalctl', '--sync'], timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass
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


def trial(unit: str, *, stop_at: int = STOP_BYTES,
          persistent_rc: bool = False,
          outcome: dict | None = None) -> tuple[int, int]:
    # Launch a same-context user-manager check, then exec the original
    # resumable inventory. This prevents silent zero-byte deferrals.
    args = ['/usr/bin/python3', str(REPO / 'scripts'
            / 'metered_drive_inventory_trial.py'), '--worker-entry']
    if persistent_rc:
        args.append('--persistent-rc')
    largest_ingress = largest_egress = 0
    reason = 'TIME_LIMIT'
    last_properties = {}
    began = time.monotonic()
    try:
        start(unit, args, with_user=True,
              runtime_seconds=MAX_WALL_SECONDS + 10)
        while True:
            data = props(unit)
            last_properties = data
            ingress, egress = accounted_bytes(data)
            largest_ingress = max(largest_ingress, ingress)
            largest_egress = max(largest_egress, egress)
            total = ingress + egress
            if total >= stop_at:
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
        if reason in ('WORKER_FINISHED', 'WORKER_NOT_ACTIVE'):
            status = last_properties.get('ExecMainStatus', '')
            if re.fullmatch(r'[0-9]{1,4}', status):
                print(f'TRIAL_WORKER_EXIT_STATUS: {status}', flush=True)
                if outcome is not None:
                    outcome['worker_exit_status'] = int(status)
            result = last_properties.get('Result', '')
            if result in ('exit-code', 'timeout', 'signal', 'core-dump',
                          'success', 'start-limit-hit'):
                print(f'TRIAL_WORKER_UNIT_RESULT: {result}', flush=True)
    finally:
        # Stop the entire service cgroup FIRST. Do not keep Apple network
        # traffic alive while collecting a journal or querying counters.
        try:
            stop(unit)
        finally:
            # Some systemd versions unload transient units on stop, so the
            # last *observed* samples remain the trustworthy lower bound.
            try:
                data = props(unit)
                ingress, egress = accounted_bytes(data)
                largest_ingress = max(largest_ingress, ingress)
                largest_egress = max(largest_egress, egress)
            except (TrialError, subprocess.TimeoutExpired, OSError):
                pass
            report_trial_service_log(unit)
        print(f'MEASURED IP INGRESS: {largest_ingress} bytes', flush=True)
        print(f'MEASURED IP EGRESS: {largest_egress} bytes', flush=True)
        print(f'MEASURED IP TOTAL: '
              f'{largest_ingress + largest_egress} bytes', flush=True)
        print(f'Per-pass advisory target: {MAX_BYTES} bytes; '
              f'sampled cutoff: {stop_at} bytes. '
              'Counter is sampled, not a strict quota; brief overshoot '
              'is possible.', flush=True)
        print('Automatic iCloud Drive inventory timer was NOT enabled.',
              flush=True)
    return largest_ingress, largest_egress


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true',
                        help='perform one short system-accounted inventory trial')
    parser.add_argument('--worker-entry', action='store_true',
                        help=argparse.SUPPRESS)
    parser.add_argument('--passes', type=int, default=1,
                        help='number of 90-second metered passes (1-24)')
    parser.add_argument('--status', action='store_true',
                        help='show cumulative budget without contacting Apple')
    parser.add_argument('--persistent-rc', action='store_true',
                        help='opt-in experimental persistent rclone listing '
                             'backend; use a short measured comparison first')
    parser.add_argument('--wait-idle', action='store_true',
                        help='before each pass wait up to 12 minutes total for '
                             'backups to stay idle for five seconds')
    args = parser.parse_args()
    if args.worker_entry:
        return worker_entry(persistent_rc=args.persistent_rc)
    if not (1 <= args.passes <= 24):
        parser.error('--passes must be between 1 and 24')
    if args.status and args.execute:
        parser.error('--status and --execute are mutually exclusive')
    if args.wait_idle and not args.execute:
        parser.error('--wait-idle requires --execute')
    if not args.execute and not args.status:
        print('PREVIEW: 90s metered passes; per-pass 10MB advisory budget, '
              '8MB sampled cutoff, cumulative 100MB measured cap '
              'with 2MB reserve. No timer enabled. '
              'Run with sudo --status to inspect the ledger, or '
              'sudo --execute --passes N for N explicitly metered passes.',
              flush=True)
        return 0

    nonce = str(os.getpid())
    try:
        if os.geteuid() != 0:
            raise TrialError('ROOT_REQUIRED')
        if not args.status:
            guard_setup()
        with BudgetLedger() as ledger:
            print(ledger.summary(), flush=True)
            if args.status:
                if ledger.data['pending'] is not None:
                    print('BLOCKED: previous metered run was interrupted; '
                          'do not reset or assume zero traffic', flush=True)
                return 0

            ledger.check_ready()
            idle_deadline = time.monotonic() + IDLE_WAIT_SECONDS
            preflight('apple-sync-ipaccounting-check-' + nonce + '.service')
            for index in range(args.passes):
                if ledger.remaining <= RESERVE_BYTES:
                    print('STOP: cumulative measured budget exhausted',
                          flush=True)
                    break
                ledger.check_ready()
                if args.wait_idle and not wait_for_idle_window(idle_deadline):
                    break
                cutoff = min(STOP_BYTES, ledger.remaining - RESERVE_BYTES)
                unit = f'apple-sync-drive-metered-{nonce}-{index}.service'
                print(f'METERED PASS {index + 1}/{args.passes}: '
                      f'reader={"persistent-rc" if args.persistent_rc else "legacy-lsjson"}, '
                      f'sampled cutoff={cutoff} bytes, '
                      f'remaining cumulative budget={ledger.remaining} bytes',
                      flush=True)
                # Crash-safe reservation: a dead controller cannot silently
                # treat an unmeasured run as zero traffic on the next launch.
                ledger.begin(unit)
                outcome = {}
                if args.persistent_rc:
                    ingress, egress = trial(unit, stop_at=cutoff,
                                            persistent_rc=True,
                                            outcome=outcome)
                else:
                    ingress, egress = trial(unit, stop_at=cutoff,
                                            outcome=outcome)
                ledger.complete(ingress, egress)
                print(ledger.summary(), flush=True)
                if outcome.get('worker_exit_status', 0) != 0:
                    print('METERED INVENTORY: worker failed, measured traffic '
                          'was committed; no more passes started',
                          file=sys.stderr, flush=True)
                    return 2
                if ingress + egress == 0:
                    print('STOP: no network traffic; worker probably '
                          'deferred or inventory is complete', flush=True)
                    break
    except (TrialError, BudgetError, OSError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, (TrialError, BudgetError)):
            print('METERED INVENTORY BLOCKED: ' + str(exc),
                  file=sys.stderr, flush=True)
        else:
            print('METERED INVENTORY FAILED: controller or systemd error; '
                  'no further runs scheduled', file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

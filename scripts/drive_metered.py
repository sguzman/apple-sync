#!/usr/bin/env python3
"""No-sudo control of an opt-in, root-owned metered Drive inventory unit.

'install' requests one-time administrator authorization through Polkit.
Subsequent 'run' requests are start-only for the fixed system service,
and 'status' reads a private-safe aggregate projection without root.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import polkit_authorization as polkit

UNIT = 'apple-sync-drive-metered-manual.service'
SINGLE_UNIT = 'apple-sync-drive-metered-single.service'
STATUS = Path('/var/lib/apple-sync-drive-metered-status.json')
INSTALLER = Path('/home/sguzman/Code/Sync/apple-sync/scripts/install_metered_launcher.py')
MAX_MONITOR_SECONDS = 35 * 60 + 30
POLL_SECONDS = 5
REQUIRED = ('version', 'cap_bytes', 'ingress_bytes', 'egress_bytes',
            'total_bytes', 'remaining_bytes', 'passes', 'pending', 'updated_at')


class LauncherError(Exception):
    """Known sanitized error displayed to the local user."""


def public_budget(path: Path = STATUS) -> dict | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and
            not info.st_mode & 0o022 and info.st_mode & stat.S_IROTH):
        raise LauncherError('UNTRUSTED_PUBLIC_BUDGET_STATUS')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError) as exc:
        raise LauncherError('INVALID_PUBLIC_BUDGET_STATUS') from exc
    if not isinstance(data, dict) or set(data) != set(REQUIRED):
        raise LauncherError('INVALID_PUBLIC_BUDGET_STATUS')
    if not all(type(data[k]) is int and data[k] >= 0 for k in REQUIRED
               if k != 'pending') or type(data['pending']) is not bool:
        raise LauncherError('INVALID_PUBLIC_BUDGET_STATUS')
    if (data['version'] != 1 or
        data['ingress_bytes'] + data['egress_bytes'] != data['total_bytes'] or
        max(0, data['cap_bytes'] - data['total_bytes']) != data['remaining_bytes']):
        raise LauncherError('INVALID_PUBLIC_BUDGET_STATUS')
    return data


def service_state(unit: str = UNIT) -> dict:
    result = subprocess.run(
        ['systemctl', '--system', 'show', unit, '-p', 'LoadState',
         '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result'],
        capture_output=True, text=True, check=False, timeout=10)
    if result.returncode:
        raise LauncherError('SYSTEMD_STATE_UNAVAILABLE')
    return dict(line.split('=', 1) for line in result.stdout.splitlines()
                if '=' in line)


def show_state(*, checkpoint: bool = True) -> dict:
    state = service_state()
    print('METERED SERVICE: ' + state.get('ActiveState', 'unknown') +
          ' (result=' + state.get('Result', 'unknown') + ')', flush=True)
    single = service_state(SINGLE_UNIT)
    print('ONE-PASS SERVICE: ' + single.get('ActiveState', 'unknown') +
          ' (loaded=' + single.get('LoadState', 'unknown') + ', result=' +
          single.get('Result', 'unknown') + ')', flush=True)
    budget = public_budget()
    if budget is None:
        print('PUBLIC BUDGET: not yet published by installed controller',
              flush=True)
    else:
        print('CUMULATIVE MEASURED: '
              f'total={budget["total_bytes"]} bytes, '
              f'remaining={budget["remaining_bytes"]} bytes, '
              f'passes={budget["passes"]}, '
              f'pending={"yes" if budget["pending"] else "no"}', flush=True)
    if checkpoint:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name('drive_inventory.py'))],
            check=False, timeout=15)
        if result.returncode:
            print('INVENTORY CHECKPOINT: unavailable', flush=True)
    return state


def request_start(passes: int = 8) -> str:
    if passes not in (1, 8):
        raise LauncherError('UNSUPPORTED_PASS_COUNT')
    target = SINGLE_UNIT if passes == 1 else UNIT
    state = service_state(target)
    if state.get('LoadState') != 'loaded':
        raise LauncherError('MANUAL_SERVICE_NOT_INSTALLED')
    if state.get('ActiveState') in ('active', 'activating', 'reloading'):
        raise LauncherError('MANUAL_SERVICE_ALREADY_RUNNING')
    # Avoid launching the other controller even though the shared private
    # ledger already serializes and fails closed on overlapping jobs.
    other = UNIT if passes == 1 else SINGLE_UNIT
    other_state = service_state(other)
    if other_state.get('ActiveState') in ('active', 'activating', 'reloading'):
        raise LauncherError('ANOTHER_METERED_SERVICE_RUNNING')
    result = subprocess.run(
        ['systemctl', '--system', '--no-block', 'start', target],
        capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise LauncherError('START_NOT_AUTHORIZED_OR_SYSTEMD_REFUSED')
    print(f'METERED SCAN REQUEST ACCEPTED: up to {passes} pass(es); '
          'no timer enabled', flush=True)
    return target


def monitor(unit: str = UNIT) -> None:
    print('Monitoring public counters. Ctrl+C detaches; scan continues.',
          flush=True)
    start = time.monotonic()
    previous = None
    time.sleep(3)
    while time.monotonic() - start < MAX_MONITOR_SECONDS:
        budget = public_budget()
        signature = None if budget is None else (
            budget['passes'], budget['total_bytes'], budget['pending'])
        if signature is not None and signature != previous:
            print('PROGRESS: '
                  f'passes={budget["passes"]}, '
                  f'measured={budget["total_bytes"]} bytes, '
                  f'remaining={budget["remaining_bytes"]} bytes, '
                  f'pending={"yes" if budget["pending"] else "no"}',
                  flush=True)
            previous = signature
        state = service_state(unit)
        if state.get('ActiveState') not in ('active', 'activating', 'reloading'):
            print('METERED SERVICE FINISHED: '
                  + state.get('ActiveState', 'unknown')
                  + ' (result=' + state.get('Result', 'unknown') + ')', flush=True)
            show_state()
            if state.get('Result') != 'success':
                raise LauncherError('METERED_SERVICE_FAILED')
            return
        time.sleep(POLL_SECONDS)
    print('MONITOR ENDED AT SAFETY WINDOW; check status for latest progress',
          flush=True)
    show_state()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'run', 'status', 'doctor'))
    parser.add_argument('--detach', action='store_true',
                        help='start and exit without monitoring')
    parser.add_argument('--passes', type=int, choices=(1, 8), default=8,
                        help='for run: fixed one-pass service or existing '
                             'eight-pass service (default)')
    args = parser.parse_args()
    if os.geteuid() == 0:
        print('RUN_THIS_LAUNCHER_AS_NORMAL_USER', file=sys.stderr)
        return 2
    try:
        if args.action == 'doctor':
            ready = polkit.doctor()
            state = service_state()
            print('MANUAL SERVICE: ' +
                  state.get('LoadState', 'unknown'), flush=True)
            return 0 if ready else 2
        if args.action == 'install':
            if not polkit.doctor():
                raise LauncherError('POLKIT_PREFLIGHT_FAILED')
            print('One-time administrator authorization is required to '
                  'install a root-owned service and restricted Polkit rule.',
                  flush=True)
            result = polkit.authenticate_installer(INSTALLER)
            return result
        if args.action == 'status':
            show_state()
            return 0
        unit = request_start(args.passes)
        if not args.detach:
            monitor(unit)
        return 0
    except KeyboardInterrupt:
        print('Viewer detached; system service continues under its budget.',
              flush=True)
        return 0
    except (LauncherError, polkit.AuthorizationError, OSError,
            subprocess.SubprocessError) as exc:
        print('METERED LAUNCHER: ' +
              (str(exc) if isinstance(exc, (LauncherError,
                                            polkit.AuthorizationError))
               else 'SYSTEM_ERROR'),
              file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

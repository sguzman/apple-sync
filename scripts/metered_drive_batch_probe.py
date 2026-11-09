#!/usr/bin/env python3
"""Single manual IP-accounted batch comparison, never an inventory scan.

Run ONLY through a root-owned, static, explicitly-started system unit. It
shares the existing exclusive cumulative 100 MB ledger and the same backup
idleness / disabled-timer prerequisites. The worker cannot advance any
inventory checkpoint. Sampled IPAccounting is advisory, not a hard quota.
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

from drive_meter_budget import BudgetLedger, BudgetError, RESERVE_BYTES
import metered_drive_inventory_trial as baseline

REPO = baseline.REPO
USER = baseline.USER
MAX_SECONDS = 85
SOFT_STOP_BYTES = 1_500_000
POLL_SECONDS = 0.25


class ProbeMeterError(Exception):
    """No raw private account identifiers or rclone errors."""


def start_worker(unit: str) -> None:
    user = pwd.getpwnam(USER)
    worker = REPO / 'scripts' / 'drive_batch_probe_worker.py'
    if not worker.is_file():
        raise ProbeMeterError('PROBE_WORKER_MISSING')
    args = [
        'systemd-run', '--system', '--quiet', '--unit=' + unit,
        '--property=Type=exec',
        '--property=IPAccounting=yes',
        '--property=RemainAfterExit=yes',
        '--property=RuntimeMaxSec=' + str(MAX_SECONDS + 10) + 's',
        '--property=TimeoutStopSec=5s',
        '--property=KillMode=control-group',
        '--property=Nice=15',
        '--property=IOSchedulingClass=idle',
        '--property=NoNewPrivileges=yes',
        '--property=User=' + USER,
        '--property=WorkingDirectory=' + str(REPO),
        '--setenv=HOME=' + user.pw_dir,
        '--setenv=XDG_RUNTIME_DIR=/run/user/' + str(user.pw_uid),
        '--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'
        + str(user.pw_uid) + '/bus',
        '--setenv=APPLE_SYNC_BATCH_METERED=1',
        '/usr/bin/python3', str(worker), '--execute',
    ]
    baseline.run(args, timeout=15)


def sanitized_worker_log(unit: str) -> None:
    """Relay aggregate status only, never Apple names, paths, IDs, or errors."""
    try:
        baseline.run(['journalctl', '--sync'], timeout=8, check=False)
        log = baseline.run(['journalctl', '--system', '-u', unit,
                            '--no-pager', '-o', 'cat', '-n', '20'],
                           timeout=8, check=False)
        if log.returncode == 0:
            for line in log.stdout.splitlines():
                if re.fullmatch(
                        r'PROBE_EQUIVALENCE: equivalent=(yes|no), '
                        r'folders=[0-2], entries=[0-9]{1,7}, '
                        r'files=[0-9]{1,7}, dirs=[0-9]{1,7}, '
                        r'checkpoint_writes=0; bandwidth evaluated by '
                        r'root ledger separately', line):
                    print(line, flush=True)
                elif re.fullmatch(r'PROBE_FAILED: [A-Z_]+', line):
                    print(line, flush=True)
    except (OSError, subprocess.SubprocessError):
        print('PROBE_WORKER_LOG_UNAVAILABLE', flush=True)


def metered_sample(unit: str, *, stop_at: int) -> tuple[int, int, int | None]:
    """Stop the whole user worker cgroup before recording the ledger."""
    largest_ingress = largest_egress = 0
    exit_status: int | None = None
    started = False
    last = {}
    start = time.monotonic()
    try:
        start_worker(unit)
        started = True
        while True:
            data = baseline.props(unit)
            last = data
            ingress, egress = baseline.accounted_bytes(data)
            largest_ingress = max(largest_ingress, ingress)
            largest_egress = max(largest_egress, egress)
            if ingress + egress >= stop_at:
                print('PROBE_STOP: SAMPLED_TRAFFIC_CUTOFF', flush=True)
                break
            if data.get('SubState') in ('exited', 'dead', 'failed'):
                print('PROBE_STOP: WORKER_EXITED', flush=True)
                break
            if data.get('ActiveState') != 'active':
                print('PROBE_STOP: UNEXPECTED_SERVICE_STATE', flush=True)
                break
            if time.monotonic() - start >= MAX_SECONDS:
                print('PROBE_STOP: TIME_LIMIT', flush=True)
                break
            time.sleep(POLL_SECONDS)
        raw_status = last.get('ExecMainStatus', '')
        if re.fullmatch(r'[0-9]{1,4}', raw_status):
            exit_status = int(raw_status)
    finally:
        # Failure to stop fails closed: the ledger remains pending.
        if started:
            baseline.stop(unit)
            try:
                data = baseline.props(unit)
                ingress, egress = baseline.accounted_bytes(data)
                largest_ingress = max(largest_ingress, ingress)
                largest_egress = max(largest_egress, egress)
            except (baseline.TrialError, OSError, subprocess.SubprocessError):
                pass
            sanitized_worker_log(unit)
        else:
            # A partially accepted systemd-run can still have started work.
            # The unconditional stop is necessary before ledger reconciliation.
            baseline.stop(unit)
    print(f'PROBE_MEASURED_IP: ingress={largest_ingress} bytes, '
          f'egress={largest_egress} bytes, '
          f'total={largest_ingress + largest_egress} bytes', flush=True)
    print('PROBE_IP_ACCOUNTING: sampled soft cutoff, NOT a hard quota',
          flush=True)
    return largest_ingress, largest_egress, exit_status


def execute() -> int:
    baseline.guard_setup()
    with BudgetLedger() as ledger:
        ledger.check_ready()
        if not baseline.wait_for_idle_window(
                time.monotonic() + baseline.IDLE_WAIT_SECONDS):
            print('PROBE_DEFERRED: backups did not become idle; no Apple requests',
                  flush=True)
            return 0
        baseline.preflight('apple-sync-ipaccounting-batch-check-' +
                           str(os.getpid()) + '.service')
        cutoff = min(SOFT_STOP_BYTES, ledger.remaining - RESERVE_BYTES)
        if cutoff < 1:
            raise ProbeMeterError('PROBE_BANDWIDTH_BUDGET_EXHAUSTED')
        unit = 'apple-sync-drive-metered-batch-probe-' + str(os.getpid()) + '.service'
        print(f'PROBE_START: read-only <=2 folders; sampled stop={cutoff} bytes; '
              f'cumulative budget remaining={ledger.remaining} bytes',
              flush=True)
        # Durable reservation must precede any Apple network. Share the same
        # exclusive ledger as all ordinary inventory services.
        ledger.begin(unit)
        ingress, egress, exit_status = metered_sample(unit, stop_at=cutoff)
        # Only after the entire worker cgroup has stopped.
        ledger.complete(ingress, egress)
        print(ledger.summary(), flush=True)
        if exit_status != 0:
            print('PROBE_RESULT: WORKER_FAILED_OR_INTERRUPTED; '
                  'measured traffic committed; no checkpoint writes',
                  flush=True)
            return 2
        print('PROBE_RESULT: worker finished; comparison is diagnostic only',
              flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        print('PROBE_CONTROLLER_PREVIEW: no network; one two-folder comparison; '
              'IP accounting and archive lock required')
        return 0
    try:
        if os.geteuid() != 0:
            raise ProbeMeterError('ROOT_SYSTEM_SERVICE_REQUIRED')
        return execute()
    except (ProbeMeterError, baseline.TrialError, BudgetError, OSError,
            subprocess.SubprocessError) as exc:
        safe = str(exc) if isinstance(exc, (ProbeMeterError,
                                            baseline.TrialError,
                                            BudgetError)) else 'SYSTEMD_OR_IO_ERROR'
        print('PROBE_METER_BLOCKED: ' + safe, file=sys.stderr, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

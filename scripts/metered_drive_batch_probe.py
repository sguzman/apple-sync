#!/usr/bin/env python3
"""Single manual IP-accounted batch comparison, never an inventory scan.

Run ONLY through a root-owned, static, explicitly-started system unit. It
shares the existing exclusive cumulative 100 MB ledger and the same backup
idleness / disabled-timer prerequisites. The worker cannot advance any
inventory checkpoint. Sampled IPAccounting is advisory, not a hard quota.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import tempfile
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


RECEIPT_NAME = 'result.json'
MAX_RECEIPT_BYTES = 1024


@contextmanager
def receipt_directory():
    """A fresh 0700 per-probe channel in the user's volatile runtime tree.

    Never place comparison receipts in the archive or a shared world-readable
    directory. A root controller can inspect and delete the file after the
    transient worker cgroup has stopped and network bytes are accounted.
    """
    user = pwd.getpwnam(USER)
    runtime = Path('/run/user') / str(user.pw_uid)
    if runtime.is_symlink():
        raise ProbeMeterError('PROBE_RUNTIME_DIRECTORY_UNTRUSTED')
    info = runtime.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != user.pw_uid
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ProbeMeterError('PROBE_RUNTIME_DIRECTORY_UNTRUSTED')
    with tempfile.TemporaryDirectory(
            prefix='.apple-sync-batch-probe-', dir=runtime) as directory:
        os.chown(directory, user.pw_uid, user.pw_gid)
        os.chmod(directory, 0o700)
        yield Path(directory)


def read_receipt(directory: Path) -> dict:
    """Accept only a complete, user-owned aggregate success attestation."""
    user = pwd.getpwnam(USER)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != user.pw_uid
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ProbeMeterError('PROBE_RESULT_CHANNEL_UNTRUSTED')
    path = directory / RECEIPT_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ProbeMeterError('PROBE_EQUIVALENCE_RECEIPT_MISSING') from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != user.pw_uid
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1 or info.st_size > MAX_RECEIPT_BYTES):
        raise ProbeMeterError('PROBE_EQUIVALENCE_RECEIPT_UNTRUSTED')
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError, UnicodeError):
        raise ProbeMeterError('PROBE_EQUIVALENCE_RECEIPT_INVALID') from None
    fields = {'version', 'equivalent', 'core_fields_verified',
              'timestamps_verified', 'folders_matched', 'entries_matched',
              'files_matched', 'directories_matched', 'checkpoint_writes',
              'checkpoints_advanced'}
    if (not isinstance(data, dict) or set(data) != fields
            or type(data['version']) is not int or data['version'] != 2
            or data['equivalent'] is not True
            or data['core_fields_verified'] is not True
            or data['timestamps_verified'] is not True
            or any(type(data[key]) is not int for key in fields -
                   {'version', 'equivalent', 'core_fields_verified',
                    'timestamps_verified'})
            or not 1 <= data['folders_matched'] <= 2
            or not 0 <= data['entries_matched'] <= 1_000_000
            or not 0 <= data['files_matched'] <= data['entries_matched']
            or not 0 <= data['directories_matched'] <= data['entries_matched']
            or data['files_matched'] + data['directories_matched']
            != data['entries_matched']
            or data['checkpoint_writes'] != 0
            or data['checkpoints_advanced'] != 0):
        raise ProbeMeterError('PROBE_EQUIVALENCE_RECEIPT_INVALID')
    return data


# No user-supplied strings are ever copied into controller diagnostics.
# This closed set mirrors the worker's privacy-safe failure categories.
WORKER_FAILURE_CODES = frozenset((
    'PROBE_CORE_FIELD_MISMATCH', 'PROBE_METADATA_MISMATCH',
    'PROBE_NATIVE_READ_FAILED', 'PROBE_LEGACY_READ_FAILED',
    'PROBE_COMPARE_FAILED', 'PROBE_BACKUP_BECAME_ACTIVE',
    'PROBE_BACKUP_ACTIVE_OR_UNCERTAIN', 'INVENTORY_CHECKPOINT_CHANGED',
    'INVENTORY_CHECKPOINT_MISSING', 'OTHER_APPLE_ARCHIVE_ACTIVE',
    'EXPERIMENTAL_BINARY_MISSING_OR_UNSAFE',
    'EXPERIMENTAL_BINARY_UNTRUSTED',
    'EXPERIMENTAL_BINARY_IDENTITY_MISMATCH',
    'UNSAFE_ARCHIVE_STATE', 'ARCHIVE_LOCK_MISSING_OR_UNSAFE',
    'ARCHIVE_LOCK_WRONG_OWNER', 'INVALID_PROBE_COMPARISON_RESULT',
    'PROBE_RESULT_TOO_LARGE', 'PROBE_RESULT_CHANNEL_UNTRUSTED',
    'LOCAL_CHECK_FAILED', 'UNEXPECTED_WORKER_EXCEPTION',
))


def read_failure_receipt(directory: Path) -> str:
    """Return a validated fixed failure code, never private worker output.

    This receipt is *only* inspected after the stopped worker's traffic has
    been durably accounted for, and only when the worker did not exit zero.
    A failure receipt is never accepted as proof of metadata equivalence.
    """
    user = pwd.getpwnam(USER)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != user.pw_uid
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ProbeMeterError('PROBE_FAILURE_RECEIPT_UNTRUSTED')
    path = directory / RECEIPT_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ProbeMeterError('PROBE_FAILURE_RECEIPT_MISSING') from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != user.pw_uid
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1 or not 0 < info.st_size <= MAX_RECEIPT_BYTES):
        raise ProbeMeterError('PROBE_FAILURE_RECEIPT_UNTRUSTED')
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError, UnicodeError):
        raise ProbeMeterError('PROBE_FAILURE_RECEIPT_INVALID') from None
    if (not isinstance(data, dict)
            or set(data) != {'version', 'outcome', 'code', 'checkpoint_writes'}
            or type(data['version']) is not int or data['version'] != 2
            or data['outcome'] != 'failed'
            or type(data['checkpoint_writes']) is not int
            or data['checkpoint_writes'] != 0
            or not isinstance(data['code'], str)
            or data['code'] not in WORKER_FAILURE_CODES):
        raise ProbeMeterError('PROBE_FAILURE_RECEIPT_INVALID')
    return data['code']


def start_worker(unit: str, *, receipt_dir: Path) -> None:
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
        '--setenv=APPLE_SYNC_BATCH_RECEIPT_DIR=' + str(receipt_dir),
        '--property=StandardOutput=journal',
        '--property=StandardError=journal',
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
                        r'checkpoint_writes=0; core_fields_verified=yes; '
                        r'timestamps_verified=yes; bandwidth evaluated by '
                        r'root ledger separately', line):
                    print(line, flush=True)
                elif re.fullmatch(r'PROBE_FAILED: [A-Z_]+', line):
                    print(line, flush=True)
    except (OSError, subprocess.SubprocessError):
        print('PROBE_WORKER_LOG_UNAVAILABLE', flush=True)


def metered_sample(unit: str, *, stop_at: int,
                   receipt_dir: Path) -> tuple[int, int, int | None]:
    """Stop the whole user worker cgroup before recording the ledger."""
    largest_ingress = largest_egress = 0
    exit_status: int | None = None
    started = False
    last = {}
    start = time.monotonic()
    try:
        start_worker(unit, receipt_dir=receipt_dir)
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
        # A still-running process may report ExecMainStatus=0 even though
        # the controller stopped it at the traffic/time cutoff. Only an
        # actually exited worker can report success.
        if last.get('SubState') in ('exited', 'dead', 'failed'):
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
        # This directory is outside the archive, and automatically removed.
        # A service exit code alone cannot establish metadata equivalence.
        with receipt_directory() as result_dir:
            ledger.begin(unit)
            ingress, egress, exit_status = metered_sample(
                unit, stop_at=cutoff, receipt_dir=result_dir)
            # Only after the entire worker cgroup has stopped.
            ledger.complete(ingress, egress)
            print(ledger.summary(), flush=True)
            if exit_status != 0:
                # A transient systemd unit may drop stdout/stderr from the
                # journal. Read the private worker's fixed error category
                # only after the service has stopped and IP bytes are saved.
                if exit_status is None:
                    print('PROBE_FAILED: WORKER_INTERRUPTED', flush=True)
                else:
                    try:
                        reason = read_failure_receipt(result_dir)
                    except ProbeMeterError:
                        print('PROBE_FAILED: WORKER_REASON_UNAVAILABLE',
                              flush=True)
                    else:
                        print('PROBE_FAILED: ' + reason, flush=True)
                print('PROBE_RESULT: WORKER_FAILED_OR_INTERRUPTED; '
                      'measured traffic committed; no checkpoint writes',
                      flush=True)
                return 2
            try:
                receipt = read_receipt(result_dir)
            except ProbeMeterError as exc:
                print('PROBE_RESULT: ' + str(exc) +
                      '; measured traffic committed; equivalence NOT verified',
                      flush=True)
                return 2
            print('PROBE_EQUIVALENCE: equivalent=yes, '
                  f'folders={receipt["folders_matched"]}, '
                  f'entries={receipt["entries_matched"]}, '
                  f'files={receipt["files_matched"]}, '
                  f'dirs={receipt["directories_matched"]}, '
                  'checkpoint_writes=0; core_fields_verified=yes; '
                  'timestamps_verified=yes; verified by private worker receipt',
                  flush=True)
            print('PROBE_RESULT: worker completed and private '
                  'equivalence receipt validated; scanner unchanged',
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

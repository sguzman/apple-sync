"""End-to-end mock replay root wiring: no systemd, root or Apple calls."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_root_wiring as wiring
import drive_batch_replay_systemd_adapter as adapter
import metered_drive_batch_replay as receipts
from drive_batch_replay_systemd_plan import PROPERTY_FIELDS

CHANNEL = Path('/run/user/1000/.apple-sync-replay-aBcDE123')


def observation():
    return {
        'version': 1, 'kind': 'observation',
        'outcome': 'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED',
        'directory_children_checked': 2,
        'directory_children_stable': 1,
        'directory_children_batch_only_difference': 1,
        'directory_children_single_read_difference': 0,
        'checkpoint_writes': 0, 'checkpoints_advanced': 0,
        'production_commit_authorized': False,
        'replay_requests_completed': 3, 'replay_folders_selected': 2,
    }


def systemd_fields(*, active='active', substate='running',
                   ingress=0, egress=0, status='0', accounting='yes'):
    fields = {
        'ActiveState': active, 'SubState': substate,
        'ExecMainStatus': status, 'Result': 'success',
        'IPAccounting': accounting, 'IPIngressBytes': str(ingress),
        'IPEgressBytes': str(egress),
    }
    return ''.join(f'{key}={fields[key]}\n' for key in PROPERTY_FIELDS)


class FakeLedger:
    def __init__(self, events):
        self.events = events
        self.remaining = 47_592_221
        self.pending = False
        self.completed = False
        self.fail_complete = False

    def __enter__(self):
        self.events.append('ledger_enter')
        return self

    def __exit__(self, *_unused):
        self.events.append('ledger_exit')

    def check_ready(self):
        self.events.append('check_budget')

    def begin(self, _unit):
        self.events.append('begin')
        self.pending = True

    def complete(self, ingress, egress):
        self.events.append('complete')
        if self.fail_complete:
            raise OSError('private ledger')
        self.measured = ingress, egress
        self.pending = False
        self.completed = True


class FakeExecutor:
    def __init__(self, events):
        self.events = events
        self.commands = []
        self.worker_status = '0'
        self.fail_worker_start = False
        self.fail_worker_stop = False
        self.fail_preflight_start = False
        self.fail_preflight_stop = False
        self.bad_final_counter = False
        self.preflight_traffic = False

    def __call__(self, argv, timeout):
        self.commands.append((argv, timeout))
        is_preflight = 'ipaccounting-replay-check-' in ' '.join(argv)
        if argv[0] == 'systemd-run':
            if (is_preflight and self.fail_preflight_start
                    or not is_preflight and self.fail_worker_start):
                return SimpleNamespace(returncode=2, stdout='', stderr='private')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if argv[:3] == ('systemctl', '--system', 'stop'):
            if (is_preflight and self.fail_preflight_stop
                    or not is_preflight and self.fail_worker_stop):
                return SimpleNamespace(returncode=2, stdout='', stderr='private')
            self.events.append('stop_preflight' if is_preflight else 'stop_worker')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if argv[:3] == ('systemctl', '--system', 'show'):
            stopped = ('stop_preflight' if is_preflight else 'stop_worker') in self.events
            if is_preflight:
                output = systemd_fields(
                    active='inactive' if stopped else 'active',
                    substate='dead' if stopped else 'running',
                    ingress=int(self.preflight_traffic))
            else:
                output = systemd_fields(
                    active='inactive' if stopped else 'active',
                    substate='dead' if stopped else 'exited',
                    ingress=120 if stopped else 99,
                    egress=23 if stopped else 20,
                    status=self.worker_status,
                    accounting='no' if stopped and self.bad_final_counter else 'yes')
            return SimpleNamespace(returncode=0, stdout=output, stderr='')
        raise AssertionError('unexpected test-only systemd argv')


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.ledger = FakeLedger(self.events)
        self.executor = FakeExecutor(self.events)
        self.idle = True
        self.guard_error = None
        self.payload = observation()

    def run_wiring(self, *, pid=1234, uid=1000, ledger_factory=None,
                   guard=None, idle=None, executor=None):
        def setup():
            self.events.append('guard')
            if self.guard_error:
                raise self.guard_error

        def idle_status():
            self.events.append('idle')
            return self.idle

        def read_private(_directory):
            self.events.append('receipt')
            self.assertTrue(self.ledger.completed)
            self.assertFalse(self.ledger.pending)
            self.assertIn('stop_worker', self.events)
            return self.payload

        with (patch.object(adapter, 'require_private_channel'),
              patch.object(receipts, 'read_private_receipt',
                           side_effect=read_private)):
            return wiring.run_wired(
                uid=uid, pid=pid, receipt_dir=CHANNEL,
                ledger_factory=ledger_factory or (lambda: self.ledger),
                execute_argv=executor or self.executor,
                guard_setup=setup if guard is None else guard,
                idle_window=idle_status if idle is None else idle,
                monotonic=lambda: 0,
                sleep=lambda _seconds: None)

    def test_success_stop_then_ledger_commit_then_strict_receipt(self):
        result = self.run_wiring()
        self.assertEqual(result['status'], 'REPLAY_OBSERVATION')
        self.assertEqual(result['measured_ip_bytes'], 143)
        self.assertFalse(result['production_commit_authorized'])
        self.assertEqual(result['checkpoint_writes'], 0)
        sequence = ('ledger_enter', 'check_budget', 'guard', 'idle',
                    'stop_preflight', 'begin', 'stop_worker',
                    'complete', 'receipt', 'ledger_exit')
        marks = [self.events.index(item) for item in sequence]
        self.assertEqual(marks, sorted(marks))

    def test_cumulative_ledger_lock_held_through_receipt(self):
        self.run_wiring()
        self.assertLess(self.events.index('complete'),
                        self.events.index('receipt'))
        self.assertLess(self.events.index('receipt'),
                        self.events.index('ledger_exit'))

    def test_fixed_worker_command_with_accounting_and_private_channel(self):
        self.run_wiring()
        launches = [argv for argv, _ in self.executor.commands
                    if argv[0] == 'systemd-run'
                    and '/usr/bin/python3' in argv]
        self.assertEqual(len(launches), 1)
        self.assertIn('--property=IPAccounting=yes', launches[0])
        self.assertIn(
            '--setenv=APPLE_SYNC_REPLAY_RECEIPT_DIR=' + str(CHANNEL),
            launches[0])
        self.assertEqual(launches[0][-1], '--execute')

    def test_executor_receives_only_fixed_argv_tuples_no_timers(self):
        self.run_wiring()
        self.assertTrue(all(type(argv) is tuple for argv, _ in
                            self.executor.commands))
        self.assertFalse(any('--on-calendar' in argv
                             for argv, _ in self.executor.commands))

    def test_nonzero_worker_failure_receipt_after_accounting(self):
        self.executor.worker_status = '2'
        self.payload = {
            'version': 1, 'kind': 'failure', 'code': 'REPLAY_BACKUP_INTERRUPTED',
            'checkpoint_writes': 0, 'production_commit_authorized': False,
        }
        result = self.run_wiring()
        self.assertEqual(result['status'], 'REPLAY_WORKER_FAILED')
        self.assertEqual(result['code'], 'REPLAY_BACKUP_INTERRUPTED')
        self.assertTrue(self.ledger.completed)

    def test_equivalence_receipt_cannot_be_misread_as_observation(self):
        self.payload['equivalent'] = True
        result = self.run_wiring()
        self.assertEqual(result['status'], 'REPLAY_OBSERVATION_UNVERIFIED')
        self.assertTrue(self.ledger.completed)

    def test_contradictory_counts_fail_after_measuring_bytes(self):
        self.payload['directory_children_stable'] = 2
        result = self.run_wiring()
        self.assertEqual(result['status'], 'REPLAY_OBSERVATION_UNVERIFIED')
        self.assertTrue(self.ledger.completed)

    def test_idle_backup_defers_without_launch_or_reservation(self):
        self.idle = False
        result = self.run_wiring()
        self.assertEqual(result['status'], 'REPLAY_DEFERRED_NO_TRAFFIC')
        self.assertFalse(self.executor.commands)
        self.assertNotIn('begin', self.events)

    def test_unknown_backup_status_refuses_without_launch(self):
        with self.assertRaisesRegex(wiring.ReplayWiringError,
                                    'REPLAY_IDLE_STATUS_UNKNOWN'):
            self.run_wiring(idle=lambda: None)
        self.assertFalse(self.executor.commands)

    def test_guard_failure_refuses_before_systemd_or_reservation(self):
        self.guard_error = ValueError('private account data')
        with self.assertRaisesRegex(wiring.ReplayWiringError,
                                    'REPLAY_PREFLIGHT_UNVERIFIED') as err:
            self.run_wiring()
        self.assertFalse(self.executor.commands)
        self.assertNotIn('begin', self.events)
        self.assertNotIn('private', str(err.exception))

    def test_failed_preflight_launch_still_attempts_stop(self):
        self.executor.fail_preflight_start = True
        with self.assertRaisesRegex(wiring.ReplayWiringError,
                                    'REPLAY_PREFLIGHT_UNVERIFIED'):
            self.run_wiring()
        self.assertIn('stop_preflight', self.events)
        self.assertNotIn('begin', self.events)

    def test_preflight_traffic_rejected_before_reserving(self):
        self.executor.preflight_traffic = True
        with self.assertRaisesRegex(wiring.ReplayWiringError,
                                    'REPLAY_PREFLIGHT_UNVERIFIED'):
            self.run_wiring()
        self.assertIn('stop_preflight', self.events)
        self.assertNotIn('begin', self.events)

    def test_preflight_stop_failure_never_reserves(self):
        self.executor.fail_preflight_stop = True
        with self.assertRaisesRegex(wiring.ReplayWiringError,
                                    'REPLAY_PREFLIGHT_UNVERIFIED'):
            self.run_wiring()
        self.assertNotIn('begin', self.events)
        self.assertFalse(self.ledger.pending)

    def test_worker_start_failure_stops_and_reconciles(self):
        self.executor.fail_worker_start = True
        result = self.run_wiring()
        self.assertEqual(result['status'], 'REPLAY_WORKER_INTERRUPTED')
        self.assertIn('stop_worker', self.events)
        self.assertTrue(self.ledger.completed)
        self.assertNotIn('receipt', self.events)

    def test_worker_stop_failure_keeps_shared_budget_pending(self):
        self.executor.fail_worker_stop = True
        with self.assertRaisesRegex(wiring.ReplayWiringError,
                                    'REPLAY_STOP_OR_ACCOUNTING_UNCONFIRMED'):
            self.run_wiring()
        self.assertTrue(self.ledger.pending)
        self.assertNotIn('receipt', self.events)

    def test_final_ip_accounting_disabled_keeps_budget_pending(self):
        self.executor.bad_final_counter = True
        with self.assertRaisesRegex(wiring.ReplayWiringError,
                                    'REPLAY_STOP_OR_ACCOUNTING_UNCONFIRMED'):
            self.run_wiring()
        self.assertTrue(self.ledger.pending)
        self.assertNotIn('receipt', self.events)

    def test_ledger_commit_failure_suppresses_receipt(self):
        self.ledger.fail_complete = True
        with self.assertRaisesRegex(wiring.ReplayWiringError,
                                    'REPLAY_LEDGER_RECONCILIATION_FAILED'):
            self.run_wiring()
        self.assertTrue(self.ledger.pending)
        self.assertNotIn('receipt', self.events)

    def test_invalid_worker_pid_refuses_before_opening_ledger(self):
        with self.assertRaisesRegex(wiring.ReplayWiringError,
                                    'REPLAY_ADAPTER_UNTRUSTED'):
            self.run_wiring(pid=0)
        self.assertNotIn('ledger_enter', self.events)

    def test_missing_required_callback_refuses_before_ledger(self):
        with self.assertRaisesRegex(wiring.ReplayWiringError,
                                    'REPLAY_TRUSTED_DEPENDENCY_MISSING'):
            self.run_wiring(guard=False)
        self.assertNotIn('ledger_enter', self.events)

    def test_ledger_factory_failure_is_sanitized(self):
        def fail():
            raise RuntimeError('/private/root/ledger')
        with self.assertRaisesRegex(wiring.ReplayWiringError,
                                    'REPLAY_WIRING_UNVERIFIED') as err:
            self.run_wiring(ledger_factory=fail)
        self.assertNotIn('private', str(err.exception))

    def test_no_live_controller_or_default_executor(self):
        source = Path(wiring.__file__).read_text(encoding='utf-8')
        self.assertNotIn('import subprocess', source)
        self.assertNotIn('subprocess.run(', source)
        self.assertNotIn("if __name__ == '__main__'", source)


if __name__ == '__main__':
    unittest.main()

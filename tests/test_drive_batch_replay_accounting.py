"""No-network tests for the dormant replay metering state machine."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_accounting as accounting


def props(ingress=100, egress=25, *, active='active',
          substate='exited', status='0', metered='yes'):
    return {
        'IPAccounting': metered, 'IPIngressBytes': str(ingress),
        'IPEgressBytes': str(egress), 'ActiveState': active,
        'SubState': substate, 'ExecMainStatus': status,
    }


class Ledger:
    def __init__(self):
        self.remaining = 47_592_221
        self.events = []
        self.pending = False
        self.finished = False
        self.fail_complete = False

    def check_ready(self):
        self.events.append('ready')

    def begin(self, unit):
        self.events.append('begin')
        assert not self.pending
        self.pending = True

    def complete(self, ingress, egress):
        self.events.append('complete')
        if self.fail_complete:
            raise ValueError('untrusted filesystem')
        assert self.pending
        self.measured = ingress, egress
        self.pending = False
        self.finished = True


def receipt(**changes):
    result = {
        'version': 1, 'kind': 'observation',
        'outcome': 'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED',
        'directory_children_checked': 2,
        'directory_children_stable': 1,
        'directory_children_batch_only_difference': 1,
        'directory_children_single_read_difference': 0,
        'checkpoint_writes': 0, 'checkpoints_advanced': 0,
        'production_commit_authorized': False,
        'replay_requests_completed': 3,
        'replay_folders_selected': 2,
    }
    result.update(changes)
    return result


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger()
        self.actions = []
        self.samples = iter([props(),
                             props(110, 30, active='inactive',
                                   substate='dead')])
        self.idle = True
        self.launch_error = None
        self.stop_error = None
        self.counter_error = None

    def run_once(self, *, observe=None, fail=None, unit=None,
                 preflight=None, guard=None, counter=None, stop=None,
                 start=None, idle=None, monotonic=None, sleep=None):
        events = self.actions
        def wrap(label, callback):
            def call(*args, **kwargs):
                events.append(label)
                return callback(*args, **kwargs)
            return call

        def worker():
            if self.launch_error:
                raise self.launch_error

        def stop_worker():
            if self.stop_error:
                raise self.stop_error

        def snapshot():
            if self.counter_error:
                raise self.counter_error
            return next(self.samples)

        return accounting.run_accounted(
            ledger=self.ledger,
            unit=unit or 'apple-sync-drive-metered-replay-123.service',
            guard_setup=wrap('guard', guard or (lambda: None)),
            idle_window=wrap('idle', idle or (lambda: self.idle)),
            preflight=wrap('preflight', preflight or (lambda: None)),
            start_worker=wrap('start', start or worker),
            properties=wrap('props', counter or snapshot),
            stop_worker=wrap('stop', stop or stop_worker),
            monotonic=monotonic or (lambda: 0),
            sleep=sleep or (lambda _t: None),
            read_observation=wrap('observation', observe or (lambda: receipt())),
            read_failure=wrap('failure', fail or
                              (lambda: 'REPLAY_BACKUP_INTERRUPTED')),
        )

    def test_success_reserves_stops_accounts_before_receipt(self):
        result = self.run_once()
        self.assertEqual(result['status'], 'REPLAY_OBSERVATION')
        self.assertEqual(result['outcome'],
                         'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED')
        self.assertEqual(result['measured_ip_bytes'], 140)
        self.assertEqual(self.ledger.measured, (110, 30))
        self.assertTrue(self.ledger.finished)
        self.assertEqual(self.actions, [
            'guard', 'idle', 'preflight', 'start',
            'props', 'stop', 'props', 'observation',
        ])
        self.assertEqual(self.ledger.events, ['ready', 'begin', 'complete'])
        self.assertFalse(result['production_commit_authorized'])

    def test_receipt_reader_proves_ledger_was_already_completed(self):
        def proof():
            self.assertTrue(self.ledger.finished)
            self.assertFalse(self.ledger.pending)
            self.assertIn('stop', self.actions)
            return receipt()
        self.assertEqual(self.run_once(observe=proof)['status'],
                         'REPLAY_OBSERVATION')

    def test_nonzero_worker_failure_still_reconciles_before_failure_receipt(self):
        self.samples = iter([props(status='2'),
                             props(225, 66, active='inactive',
                                   substate='dead', status='2')])
        def reason():
            self.assertTrue(self.ledger.finished)
            return 'REPLAY_BACKUP_INTERRUPTED'
        result = self.run_once(fail=reason)
        self.assertEqual(result['status'], 'REPLAY_WORKER_FAILED')
        self.assertEqual(result['code'], 'REPLAY_BACKUP_INTERRUPTED')
        self.assertEqual(result['measured_ip_bytes'], 291)
        self.assertNotIn('observation', self.actions)

    def test_unknown_failure_code_is_not_disclosed(self):
        self.samples = iter([props(status='2'),
                             props(active='inactive', substate='dead')])
        result = self.run_once(fail=lambda: '/private/account/name')
        self.assertEqual(result['status'], 'REPLAY_WORKER_FAILED_UNVERIFIED')
        self.assertNotIn('private', str(result))

    def test_missing_failure_receipt_remains_accounted(self):
        self.samples = iter([props(status='2'),
                             props(active='inactive', substate='dead')])
        def error():
            raise OSError('private receipt path')
        result = self.run_once(fail=error)
        self.assertEqual(result['status'], 'REPLAY_WORKER_FAILED_UNVERIFIED')
        self.assertTrue(self.ledger.finished)

    def test_invalid_observation_does_not_claim_success(self):
        for bad in (
                {'kind': 'equivalence'},
                {'equivalent': True},
                {'timestamps_verified': True},
                {'directory_children_checked': 3},
                {'outcome': 'REPLAY_ALL_DIRECTORY_TIMESTAMPS_IDENTICAL'},
                {'production_commit_authorized': True},
                {'checkpoint_writes': 1},
                {'outcome': 'secret/private/file'},
                {'outcome': []}):
            self.setUp()
            result = self.run_once(observe=lambda bad=bad: receipt(**bad))
            self.assertEqual(result['status'], 'REPLAY_OBSERVATION_UNVERIFIED')
            self.assertTrue(self.ledger.finished)
            self.assertNotIn('secret', str(result))

    def test_unreadable_observation_does_not_undo_accounting(self):
        def broken():
            raise ValueError('/private/cloud/path')
        result = self.run_once(observe=broken)
        self.assertEqual(result['status'], 'REPLAY_OBSERVATION_UNVERIFIED')
        self.assertTrue(self.ledger.finished)
        self.assertNotIn('private', str(result))

    def test_unknown_idle_status_refuses_without_begin_or_launch(self):
        with self.assertRaisesRegex(accounting.ReplayAccountingError,
                                    'REPLAY_IDLE_STATUS_UNKNOWN'):
            self.run_once(idle=lambda: None)
        self.assertNotIn('begin', self.ledger.events)
        self.assertNotIn('start', self.actions)

    def test_active_backup_defers_without_traffic(self):
        self.idle = False
        result = self.run_once()
        self.assertEqual(result['status'], 'REPLAY_DEFERRED_NO_TRAFFIC')
        self.assertNotIn('start', self.actions)
        self.assertNotIn('begin', self.ledger.events)

    def test_guard_failure_refuses_before_reservation(self):
        def denied():
            raise ValueError('private local state')
        with self.assertRaisesRegex(accounting.ReplayAccountingError,
                                    'REPLAY_PREFLIGHT_UNVERIFIED'):
            self.run_once(guard=denied)
        self.assertEqual(self.actions, ['guard'])
        self.assertNotIn('begin', self.ledger.events)

    def test_preflight_failure_refuses_before_reservation(self):
        def denied():
            raise OSError('private local state')
        with self.assertRaisesRegex(accounting.ReplayAccountingError,
                                    'REPLAY_PREFLIGHT_UNVERIFIED'):
            self.run_once(preflight=denied)
        self.assertEqual(self.actions, ['guard', 'idle', 'preflight'])
        self.assertNotIn('begin', self.ledger.events)

    def test_bad_or_exhausted_budget_refuses_without_launch(self):
        self.ledger.remaining = accounting.RESERVE_BYTES
        with self.assertRaisesRegex(accounting.ReplayAccountingError,
                                    'REPLAY_BUDGET_EXHAUSTED'):
            self.run_once()
        self.assertNotIn('begin', self.ledger.events)
        self.assertNotIn('start', self.actions)

    def test_untrusted_unit_refuses_before_guard_or_ledger(self):
        for unit in ('apple-sync-drive-metered-batch-probe-1.service',
                     'private.service', 'apple-sync-drive-metered-replay-a.service'):
            self.setUp()
            with self.assertRaisesRegex(accounting.ReplayAccountingError,
                                        'REPLAY_UNIT_UNTRUSTED'):
                self.run_once(unit=unit)
            self.assertEqual(self.actions, [])
            self.assertEqual(self.ledger.events, [])

    def test_failed_start_still_stops_before_settlement(self):
        self.launch_error = OSError('private path')
        self.samples = iter([props(0, 0, active='inactive',
                                   substate='dead')])
        result = self.run_once()
        self.assertEqual(result['status'], 'REPLAY_WORKER_INTERRUPTED')
        self.assertEqual(result['measured_ip_bytes'], 0)
        self.assertTrue(self.ledger.finished)
        self.assertEqual(self.actions[-2:], ['stop', 'props'])
        self.assertNotIn('observation', self.actions)

    def test_stop_failure_keeps_durable_ledger_pending(self):
        self.stop_error = OSError('must not skip stop')
        with self.assertRaisesRegex(accounting.ReplayAccountingError,
                                    'REPLAY_STOP_OR_ACCOUNTING_UNCONFIRMED'):
            self.run_once()
        self.assertTrue(self.ledger.pending)
        self.assertFalse(self.ledger.finished)
        self.assertNotIn('observation', self.actions)

    def test_final_unit_state_must_be_confirmed_inactive_dead(self):
        self.samples = iter([props(), props(110, 30, active='active',
                                            substate='running')])
        with self.assertRaisesRegex(accounting.ReplayAccountingError,
                                    'REPLAY_STOP_OR_ACCOUNTING_UNCONFIRMED'):
            self.run_once()
        self.assertTrue(self.ledger.pending)

    def test_missing_ip_accounting_keeps_ledger_pending(self):
        self.samples = iter([props(), props(active='inactive',
                                            substate='dead', metered='no')])
        with self.assertRaisesRegex(accounting.ReplayAccountingError,
                                    'REPLAY_STOP_OR_ACCOUNTING_UNCONFIRMED'):
            self.run_once()
        self.assertTrue(self.ledger.pending)
        self.assertNotIn('observation', self.actions)

    def test_counter_error_still_forces_stop_and_blocks_success(self):
        calls = 0
        def read():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError('private cloud credentials')
            return props(active='inactive', substate='dead')
        result = self.run_once(counter=read)
        self.assertEqual(result['status'], 'REPLAY_WORKER_INTERRUPTED')
        self.assertIn('stop', self.actions)
        self.assertTrue(self.ledger.finished)
        self.assertNotIn('private', str(result))

    def test_soft_byte_cutoff_stops_and_never_accepts_success_receipt(self):
        self.samples = iter([
            props(900_000, 800_000, active='active', substate='running'),
            props(900_100, 800_050, active='inactive', substate='dead')])
        result = self.run_once()
        self.assertEqual(result['status'], 'REPLAY_WORKER_INTERRUPTED')
        self.assertEqual(result['measured_ip_bytes'], 1_700_150)
        self.assertNotIn('observation', self.actions)

    def test_time_limit_stops_and_never_accepts_receipt(self):
        self.samples = iter([
            props(5, 5, active='active', substate='running'),
            props(5, 6, active='inactive', substate='dead')])
        calls = iter([0, accounting.MAX_SECONDS + 1])
        result = self.run_once(monotonic=lambda: next(calls))
        self.assertEqual(result['status'], 'REPLAY_WORKER_INTERRUPTED')
        self.assertIn('stop', self.actions)
        self.assertTrue(self.ledger.finished)

    def test_ledger_commit_failure_keeps_pending_and_suppresses_receipt(self):
        self.ledger.fail_complete = True
        with self.assertRaisesRegex(accounting.ReplayAccountingError,
                                    'REPLAY_LEDGER_RECONCILIATION_FAILED'):
            self.run_once()
        self.assertTrue(self.ledger.pending)
        self.assertNotIn('observation', self.actions)
        self.assertNotIn('failure', self.actions)

    def test_live_worker_is_not_launchable_from_this_module(self):
        with open(accounting.__file__, encoding='utf-8') as inp:
            code = inp.read()
        self.assertNotIn('systemd-run', code)
        self.assertNotIn('argparse', code)
        self.assertNotIn("if __name__ == '__main__'", code)


if __name__ == '__main__':
    unittest.main()

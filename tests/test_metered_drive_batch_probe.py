"""No systemd work: root-metered probe controller failure-path tests."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import metered_drive_batch_probe as controller


def counters(ingress, egress, *, substate='running', exit_status='0'):
    return {
        'IPAccounting': 'yes',
        'IPIngressBytes': str(ingress),
        'IPEgressBytes': str(egress),
        'ActiveState': 'active',
        'SubState': substate,
        'ExecMainStatus': exit_status,
    }


class MeteredProbeTests(unittest.TestCase):
    def test_transient_worker_uses_IP_accounting_and_has_no_timer(self):
        class User:
            pw_dir = '/home/sguzman'
            pw_uid = 1000
        command = []
        with (patch.object(controller.pwd, 'getpwnam', return_value=User()),
              patch.object(controller.baseline, 'run',
                           side_effect=lambda argv, **kwargs: command.extend(argv)),
              patch.object(Path, 'is_file', return_value=True)):
            controller.start_worker('apple-sync-drive-metered-batch-probe-test.service',
                                    receipt_dir=Path('/run/user/1000/probe-test'))
        self.assertIn('--property=IPAccounting=yes', command)
        self.assertIn('--property=KillMode=control-group', command)
        self.assertIn('--property=User=sguzman', command)
        self.assertIn('--setenv=APPLE_SYNC_BATCH_METERED=1', command)
        self.assertIn('--setenv=APPLE_SYNC_BATCH_RECEIPT_DIR=/run/user/1000/probe-test',
                      command)
        self.assertIn('--property=StandardOutput=journal', command)
        self.assertIn('--property=NoNewPrivileges=yes', command)
        self.assertNotIn('--on-calendar', command)
        self.assertEqual(command[-1], '--execute')
        self.assertIn('drive_batch_probe_worker.py', command[-2])

    def test_completed_worker_stopped_before_returning_bytes(self):
        samples = iter([counters(500, 200, substate='exited'),
                        counters(550, 210, substate='dead')])
        calls = []
        with (patch.object(controller, 'start_worker', side_effect=lambda *_a, **_kw: calls.append('start')),
              patch.object(controller.baseline, 'props', side_effect=lambda *_a: next(samples)),
              patch.object(controller.baseline, 'stop', side_effect=lambda *_a: calls.append('stop')),
              patch.object(controller, 'sanitized_worker_log',
                           side_effect=lambda *_a: calls.append('log')),
              contextlib.redirect_stdout(io.StringIO())):
            traffic = controller.metered_sample('unit', receipt_dir=Path('/tmp/fake-probe'), stop_at=1000)
        self.assertEqual(traffic, (550, 210, 0))
        self.assertEqual(calls, ['start', 'stop', 'log'])

    def test_soft_cutoff_stops_worker_even_if_still_running(self):
        samples = iter([counters(700, 900), counters(710, 905)])
        with (patch.object(controller, 'start_worker'),
              patch.object(controller.baseline, 'props', side_effect=lambda *_a: next(samples)),
              patch.object(controller.baseline, 'stop') as stop,
              patch.object(controller, 'sanitized_worker_log'),
              contextlib.redirect_stdout(io.StringIO())):
            result = controller.metered_sample('unit', receipt_dir=Path('/tmp/fake-probe'), stop_at=1500)
        stop.assert_called_once_with('unit')
        self.assertEqual(result[:2], (710, 905))
        self.assertIsNone(result[2], 'a force-stopped worker is never a success')

    def test_stop_failure_leaves_ledger_pending(self):
        samples = [counters(100, 200, substate='exited')]
        with (patch.object(controller, 'start_worker'),
              patch.object(controller.baseline, 'props', return_value=samples[0]),
              patch.object(controller.baseline, 'stop',
                           side_effect=controller.baseline.TrialError('SYSTEMD_STOP_UNCONFIRMED')),
              self.assertRaisesRegex(controller.baseline.TrialError,
                                    'SYSTEMD_STOP_UNCONFIRMED')):
            controller.metered_sample('unit', receipt_dir=Path('/tmp/fake-probe'), stop_at=1500)

    def test_run_uses_one_shared_ledger_reservation_and_completion(self):
        events = []
        class Ledger:
            remaining = 10_000_000
            def __enter__(self):
                events.append('ledger_lock')
                return self
            def __exit__(self, *_a):
                events.append('ledger_unlock')
            def check_ready(self):
                events.append('ready')
            def begin(self, unit):
                events.append('begin')
            def complete(self, ingress, egress):
                events.append('complete')
                self.last = ingress, egress
            def summary(self):
                return 'safe'
        ledger = Ledger()
        with (patch.object(controller.baseline, 'guard_setup') as guard,
              patch.object(controller.baseline, 'wait_for_idle_window',
                           return_value=True),
              patch.object(controller.baseline, 'preflight'),
              patch.object(controller, 'BudgetLedger', return_value=ledger),
              patch.object(controller, 'receipt_directory',
                           return_value=contextlib.nullcontext(Path('/tmp/fake-probe'))),
              patch.object(controller, 'read_receipt', return_value={
                  'folders_matched': 2, 'entries_matched': 1,
                  'files_matched': 1, 'directories_matched': 0,
                  'core_fields_verified': True, 'timestamps_verified': True}),
              patch.object(controller, 'metered_sample',
                           side_effect=lambda *a, **kw:
                           (events.append('worker'), (100, 200, 0))[1]),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(controller.execute(), 0)
        guard.assert_called_once()
        self.assertEqual(ledger.last, (100, 200))
        self.assertLess(events.index('begin'), events.index('worker'))
        self.assertLess(events.index('worker'), events.index('complete'))
        self.assertLess(events.index('complete'), events.index('ledger_unlock'))

    def test_no_idle_window_does_not_begin_ledger_pass(self):
        class Ledger:
            remaining = 10_000_000
            def __enter__(self):
                return self
            def __exit__(self, *_a):
                pass
            def check_ready(self):
                pass
            def begin(self, *_a):
                raise AssertionError('a remote request would be possible')
        with (patch.object(controller.baseline, 'guard_setup'),
              patch.object(controller.baseline, 'wait_for_idle_window',
                           return_value=False),
              patch.object(controller.baseline, 'preflight') as preflight,
              patch.object(controller, 'BudgetLedger', return_value=Ledger()),
              patch.object(controller, 'start_worker') as worker,
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(controller.execute(), 0)
        preflight.assert_not_called()
        worker.assert_not_called()

    def test_worker_failure_still_accounts_for_observed_traffic(self):
        class Ledger:
            remaining = 10_000_000
            def __enter__(self):
                return self
            def __exit__(self, *_a):
                pass
            def check_ready(self):
                pass
            def begin(self, *_a):
                pass
            def complete(self, ingress, egress):
                self.measured = (ingress, egress)
            def summary(self):
                return 'safe'
        ledger = Ledger()
        with (patch.object(controller.baseline, 'guard_setup'),
              patch.object(controller.baseline, 'wait_for_idle_window', return_value=True),
              patch.object(controller.baseline, 'preflight'),
              patch.object(controller, 'BudgetLedger', return_value=ledger),
              patch.object(controller, 'receipt_directory',
                           return_value=contextlib.nullcontext(Path('/tmp/fake-probe'))),
              patch.object(controller, 'metered_sample', return_value=(8, 9, 2)),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(controller.execute(), 2)
        self.assertEqual(ledger.measured, (8, 9))

    def test_missing_ip_accounting_fails_closed(self):
        with (patch.object(controller, 'start_worker'),
              patch.object(controller.baseline, 'props',
                           return_value={'IPAccounting': 'no'}),
              patch.object(controller.baseline, 'stop') as stopped,
              patch.object(controller, 'sanitized_worker_log'),
              self.assertRaises(controller.baseline.TrialError)):
            controller.metered_sample('unit', receipt_dir=Path('/tmp/fake-probe'), stop_at=1500)
        stopped.assert_called_once()

    def test_private_result_receipt_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            directory.chmod(0o700)
            receipt = {
                'version': 2, 'equivalent': True,
                'core_fields_verified': True, 'timestamps_verified': True,
                'folders_matched': 2,
                'entries_matched': 3, 'files_matched': 2,
                'directories_matched': 1, 'checkpoint_writes': 0,
                'checkpoints_advanced': 0,
            }
            path = directory / controller.RECEIPT_NAME
            path.write_text(json.dumps(receipt))
            path.chmod(0o600)
            user = Mock(pw_uid=os.getuid(), pw_gid=os.getgid())
            with patch.object(controller.pwd, 'getpwnam', return_value=user):
                self.assertEqual(controller.read_receipt(directory), receipt)

    def test_missing_or_invalid_receipt_never_establishes_equivalence(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            directory.chmod(0o700)
            user = Mock(pw_uid=os.getuid(), pw_gid=os.getgid())
            with patch.object(controller.pwd, 'getpwnam', return_value=user):
                with self.assertRaisesRegex(controller.ProbeMeterError, 'RECEIPT_MISSING'):
                    controller.read_receipt(directory)
                path = directory / controller.RECEIPT_NAME
                path.write_text(json.dumps({
                    'version': 2, 'equivalent': True,
                'core_fields_verified': True, 'timestamps_verified': True,
                'folders_matched': 2,
                    'entries_matched': 1, 'files_matched': 1,
                    'directories_matched': 0, 'checkpoint_writes': 1,
                    'checkpoints_advanced': 0,
                }))
                path.chmod(0o600)
                with self.assertRaisesRegex(controller.ProbeMeterError, 'RECEIPT_INVALID'):
                    controller.read_receipt(directory)

    def test_identity_only_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            directory.chmod(0o700)
            receipt = {
                'version': 2, 'equivalent': True,
                'core_fields_verified': False, 'timestamps_verified': False,
                'folders_matched': 2, 'entries_matched': 3, 'files_matched': 2,
                'directories_matched': 1, 'checkpoint_writes': 0,
                'checkpoints_advanced': 0,
            }
            path = directory / controller.RECEIPT_NAME
            path.write_text(json.dumps(receipt))
            path.chmod(0o600)
            user = Mock(pw_uid=os.getuid(), pw_gid=os.getgid())
            with patch.object(controller.pwd, 'getpwnam', return_value=user):
                with self.assertRaisesRegex(controller.ProbeMeterError,
                                            'RECEIPT_INVALID'):
                    controller.read_receipt(directory)
            receipt['core_fields_verified'] = True
            receipt['timestamps_verified'] = True
            receipt['version'] = 1
            path.write_text(json.dumps(receipt))
            with patch.object(controller.pwd, 'getpwnam', return_value=user):
                with self.assertRaisesRegex(controller.ProbeMeterError,
                                            'RECEIPT_INVALID'):
                    controller.read_receipt(directory)

    def test_zero_exit_without_receipt_returns_failure_after_accounting(self):
        class Ledger:
            remaining = 10_000_000
            def __enter__(self):
                return self
            def __exit__(self, *_a):
                pass
            def check_ready(self):
                pass
            def begin(self, *_a):
                pass
            def complete(self, ingress, egress):
                self.measured = ingress, egress
            def summary(self):
                return 'safe'
        ledger = Ledger()
        with (patch.object(controller.baseline, 'guard_setup'),
              patch.object(controller.baseline, 'wait_for_idle_window', return_value=True),
              patch.object(controller.baseline, 'preflight'),
              patch.object(controller, 'BudgetLedger', return_value=ledger),
              patch.object(controller, 'receipt_directory',
                           return_value=contextlib.nullcontext(Path('/tmp/fake-probe'))),
              patch.object(controller, 'metered_sample', return_value=(123, 45, 0)),
              patch.object(controller, 'read_receipt',
                           side_effect=controller.ProbeMeterError(
                               'PROBE_EQUIVALENCE_RECEIPT_MISSING')),
              contextlib.redirect_stdout(io.StringIO()) as out):
            self.assertEqual(controller.execute(), 2)
        self.assertEqual(ledger.measured, (123, 45))
        self.assertIn('equivalence NOT verified', out.getvalue())

    def test_private_failure_receipt_recovers_specific_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            directory.chmod(0o700)
            receipt = {
                'version': 2, 'outcome': 'failed',
                'code': 'PROBE_CORE_FIELD_MISMATCH',
                'checkpoint_writes': 0,
            }
            path = directory / controller.RECEIPT_NAME
            path.write_text(json.dumps(receipt))
            path.chmod(0o600)
            user = Mock(pw_uid=os.getuid(), pw_gid=os.getgid())
            with patch.object(controller.pwd, 'getpwnam', return_value=user):
                self.assertEqual(controller.read_failure_receipt(directory),
                                 'PROBE_CORE_FIELD_MISMATCH')
                with self.assertRaises(controller.ProbeMeterError):
                    controller.read_receipt(directory)

    def test_failure_receipt_rejects_injected_data_or_unsafe_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            directory.chmod(0o700)
            path = directory / controller.RECEIPT_NAME
            user = Mock(pw_uid=os.getuid(), pw_gid=os.getgid())
            with patch.object(controller.pwd, 'getpwnam', return_value=user):
                with self.assertRaisesRegex(controller.ProbeMeterError,
                                            'RECEIPT_MISSING'):
                    controller.read_failure_receipt(directory)
                for code in ('/private/secret', 'PRIVATE_FOLDER_NAME',
                             'PROBE_COMPARE_FAILED; secret'):
                    path.write_text(json.dumps({
                        'version': 2, 'outcome': 'failed', 'code': code,
                        'checkpoint_writes': 0,
                    }))
                    path.chmod(0o600)
                    with self.subTest(code=code), self.assertRaisesRegex(
                            controller.ProbeMeterError, 'RECEIPT_INVALID'):
                        controller.read_failure_receipt(directory)
                path.write_text(json.dumps({
                    'version': 2, 'outcome': 'failed',
                    'code': 'PROBE_CORE_FIELD_MISMATCH',
                    'checkpoint_writes': 0,
                }))
                path.chmod(0o644)
                with self.assertRaisesRegex(controller.ProbeMeterError,
                                            'RECEIPT_UNTRUSTED'):
                    controller.read_failure_receipt(directory)

    def test_worker_failure_reason_printed_only_after_traffic_committed(self):
        events = []
        class Ledger:
            remaining = 10_000_000
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
            def check_ready(self):
                pass
            def begin(self, *_args):
                events.append('begin')
            def complete(self, ingress, egress):
                events.append('complete')
                self.measured = (ingress, egress)
            def summary(self):
                return 'safe'
        ledger = Ledger()
        def recover(_directory):
            self.assertEqual(ledger.measured, (123, 45))
            events.append('failure_receipt')
            return 'PROBE_CORE_FIELD_MISMATCH'
        with (patch.object(controller.baseline, 'guard_setup'),
              patch.object(controller.baseline, 'wait_for_idle_window',
                           return_value=True),
              patch.object(controller.baseline, 'preflight'),
              patch.object(controller, 'BudgetLedger', return_value=ledger),
              patch.object(controller, 'receipt_directory',
                           return_value=contextlib.nullcontext(Path('/tmp/fake'))),
              patch.object(controller, 'metered_sample',
                           return_value=(123, 45, 2)),
              patch.object(controller, 'read_failure_receipt',
                           side_effect=recover),
              patch.object(controller, 'read_receipt') as read_success,
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(controller.execute(), 2)
        self.assertEqual(events, ['begin', 'complete', 'failure_receipt'])
        read_success.assert_not_called()
        self.assertIn('PROBE_FAILED: PROBE_CORE_FIELD_MISMATCH',
                      output.getvalue())
        self.assertNotIn('PROBE_EQUIVALENCE: equivalent=yes',
                         output.getvalue())

    def test_missing_failure_receipt_reports_unavailable_not_success(self):
        class Ledger:
            remaining = 10_000_000
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                pass
            def check_ready(self):
                pass
            def begin(self, *_args):
                pass
            def complete(self, *_args):
                pass
            def summary(self):
                return 'safe'
        with (patch.object(controller.baseline, 'guard_setup'),
              patch.object(controller.baseline, 'wait_for_idle_window',
                           return_value=True),
              patch.object(controller.baseline, 'preflight'),
              patch.object(controller, 'BudgetLedger', return_value=Ledger()),
              patch.object(controller, 'receipt_directory',
                           return_value=contextlib.nullcontext(Path('/tmp/fake'))),
              patch.object(controller, 'metered_sample', return_value=(0, 0, 2)),
              patch.object(controller, 'read_failure_receipt',
                           side_effect=controller.ProbeMeterError(
                               'PROBE_FAILURE_RECEIPT_MISSING')),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(controller.execute(), 2)
        self.assertIn('WORKER_REASON_UNAVAILABLE', output.getvalue())
        self.assertNotIn('equivalent=yes', output.getvalue())

    def test_logs_never_relay_arbitrary_remote_paths(self):
        raw = ('Sensitive folder FOLDER::private::secret\n'
               'PROBE_FAILED: PROBE_COMPARE_FAILED\n'
               'PROBE_EQUIVALENCE: equivalent=yes, folders=2, entries=3, '
               'files=2, dirs=1, checkpoint_writes=0; core_fields_verified=yes; '
               'timestamps_verified=yes; bandwidth evaluated by root ledger separately')
        response = Mock(returncode=0, stdout=raw)
        with (patch.object(controller.baseline, 'run', return_value=response),
              contextlib.redirect_stdout(io.StringIO()) as out):
            controller.sanitized_worker_log('unit')
        self.assertNotIn('Sensitive', out.getvalue())
        self.assertNotIn('FOLDER::', out.getvalue())
        self.assertIn('PROBE_COMPARE_FAILED', out.getvalue())
        self.assertIn('equivalent=yes', out.getvalue())


if __name__ == '__main__':
    unittest.main()

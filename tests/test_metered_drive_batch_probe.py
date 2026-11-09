"""No systemd work: root-metered probe controller failure-path tests."""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
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
            controller.start_worker('apple-sync-drive-metered-batch-probe-test.service')
        self.assertIn('--property=IPAccounting=yes', command)
        self.assertIn('--property=KillMode=control-group', command)
        self.assertIn('--property=User=sguzman', command)
        self.assertIn('--setenv=APPLE_SYNC_BATCH_METERED=1', command)
        self.assertIn('--property=NoNewPrivileges=yes', command)
        self.assertNotIn('--on-calendar', command)
        self.assertEqual(command[-1], '--execute')
        self.assertIn('drive_batch_probe_worker.py', command[-2])

    def test_completed_worker_stopped_before_returning_bytes(self):
        samples = iter([counters(500, 200, substate='exited'),
                        counters(550, 210, substate='dead')])
        calls = []
        with (patch.object(controller, 'start_worker', side_effect=lambda *_a: calls.append('start')),
              patch.object(controller.baseline, 'props', side_effect=lambda *_a: next(samples)),
              patch.object(controller.baseline, 'stop', side_effect=lambda *_a: calls.append('stop')),
              patch.object(controller, 'sanitized_worker_log',
                           side_effect=lambda *_a: calls.append('log')),
              contextlib.redirect_stdout(io.StringIO())):
            traffic = controller.metered_sample('unit', stop_at=1000)
        self.assertEqual(traffic, (550, 210, 0))
        self.assertEqual(calls, ['start', 'stop', 'log'])

    def test_soft_cutoff_stops_worker_even_if_still_running(self):
        samples = iter([counters(700, 900), counters(710, 905)])
        with (patch.object(controller, 'start_worker'),
              patch.object(controller.baseline, 'props', side_effect=lambda *_a: next(samples)),
              patch.object(controller.baseline, 'stop') as stop,
              patch.object(controller, 'sanitized_worker_log'),
              contextlib.redirect_stdout(io.StringIO())):
            result = controller.metered_sample('unit', stop_at=1500)
        stop.assert_called_once_with('unit')
        self.assertEqual(result[:2], (710, 905))

    def test_stop_failure_leaves_ledger_pending(self):
        samples = [counters(100, 200, substate='exited')]
        with (patch.object(controller, 'start_worker'),
              patch.object(controller.baseline, 'props', return_value=samples[0]),
              patch.object(controller.baseline, 'stop',
                           side_effect=controller.baseline.TrialError('SYSTEMD_STOP_UNCONFIRMED')),
              self.assertRaisesRegex(controller.baseline.TrialError,
                                    'SYSTEMD_STOP_UNCONFIRMED')):
            controller.metered_sample('unit', stop_at=1500)

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
            controller.metered_sample('unit', stop_at=1500)
        stopped.assert_called_once()

    def test_logs_never_relay_arbitrary_remote_paths(self):
        raw = ('Sensitive folder FOLDER::private::secret\\n'
               'PROBE_FAILED: PROBE_COMPARE_FAILED\\n'
               'PROBE_EQUIVALENCE: equivalent=yes, folders=2, entries=3, '
               'files=2, dirs=1, checkpoint_writes=0; bandwidth '
               'evaluated by root ledger separately')
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

import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / 'scripts' / 'metered_drive_inventory_trial.py'
spec = importlib.util.spec_from_file_location('metered_drive_inventory_trial', SOURCE)
metered = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metered)


def values(ingress, egress, *, state='active', substate='running'):
    return {'IPAccounting': 'yes', 'IPIngressBytes': str(ingress),
            'IPEgressBytes': str(egress),
            'ActiveState': state, 'SubState': substate}


class MeteredDriveTrialTests(unittest.TestCase):
    def test_valid_accounting_bytes_are_parsed_and_combined(self):
        self.assertEqual(metered.accounted_bytes(values(123, 456)),
                         (123, 456))
        self.assertEqual(metered.MAX_BYTES, 10_000_000)
        self.assertEqual(metered.STOP_BYTES, 8_000_000)
        self.assertLess(metered.STOP_BYTES, metered.MAX_BYTES)

    def test_no_data_and_malformed_or_disabled_counters_fail_closed(self):
        for fields in (
            {}, {'IPAccounting': 'no'},
            values('[no data]', 123),
            values(12, '[no data]'),
            values(-1, 10),
            values('2.4 MB', '0'),
            values(2**64, 0),
        ):
            with self.subTest(fields=fields):
                with self.assertRaises(metered.TrialError):
                    metered.accounted_bytes(fields)

    def test_transient_service_is_user_owned_with_accounting_and_kill_mode(self):
        class FakeUser:
            pw_uid = 1000
            pw_dir = '/home/sguzman'
        calls = []
        with (patch.object(metered.pwd, 'getpwnam', return_value=FakeUser()),
              patch.object(metered, 'run', side_effect=lambda c, **kw:
                           calls.append(c))):
            metered.start('unit.service', ['/usr/bin/python3', '/fake'],
                          with_user=True, runtime_seconds=100)
        command = calls[0]
        self.assertIn('--system', command)
        self.assertIn('--property=IPAccounting=yes', command)
        self.assertIn('--property=User=sguzman', command)
        self.assertIn('--property=RuntimeMaxSec=100s', command)
        self.assertIn('--property=TimeoutStopSec=5s', command)
        self.assertIn('--property=KillMode=control-group', command)
        self.assertIn('--setenv=XDG_RUNTIME_DIR=/run/user/1000', command)
        self.assertEqual(command[-2:], ['/usr/bin/python3', '/fake'])
        self.assertNotIn('--on-calendar', ' '.join(command))

    def test_preflight_uses_sleep_not_apple_and_always_stops(self):
        started = []
        stopped = []
        with (patch.object(metered, 'start',
                           side_effect=lambda *a, **kw: started.append((a, kw))),
              patch.object(metered, 'props', return_value=values(0, 0)),
              patch.object(metered, 'stop', side_effect=stopped.append),
              patch.object(metered.time, 'sleep')):
            metered.preflight('preflight.service')
        self.assertEqual(started[0][0][1], ['/usr/bin/sleep', '4'])
        self.assertFalse(started[0][1]['with_user'])
        self.assertEqual(stopped, ['preflight.service'])

    def test_failed_accounting_preflight_cannot_start_trial(self):
        with (patch.object(metered, 'guard_setup'),
              patch.object(metered, 'preflight',
                           side_effect=metered.TrialError('IP_COUNTER_UNAVAILABLE')),
              patch.object(metered, 'trial') as trial,
              patch.object(metered.os, 'getpid', return_value=77),
              patch('sys.argv', ['metered', '--execute']),
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(metered.main(), 2)
        trial.assert_not_called()

    def test_budget_crossing_stops_worker_and_reports_counts(self):
        sample = values(8_000_001, 122)
        stdout = io.StringIO()
        with (patch.object(metered, 'start') as start,
              patch.object(metered, 'props', return_value=sample),
              patch.object(metered, 'stop') as stop,
              contextlib.redirect_stdout(stdout)):
            metered.trial('trial.service')
        start.assert_called_once()
        stop.assert_called_once_with('trial.service')
        self.assertIn('SOFT_BUDGET_REACHED', stdout.getvalue())
        self.assertIn('MEASURED IP INGRESS: 8000001', stdout.getvalue())
        self.assertIn('MEASURED IP TOTAL: 8000123', stdout.getvalue())
        self.assertIn('not a strict quota', stdout.getvalue())

    def test_stopped_worker_reports_final_traffic_without_looping(self):
        sample = values(123, 456, state='active', substate='exited')
        stdout = io.StringIO()
        with (patch.object(metered, 'start'),
              patch.object(metered, 'props', return_value=sample),
              patch.object(metered, 'stop') as stop,
              contextlib.redirect_stdout(stdout)):
            metered.trial('trial.service')
        stop.assert_called_once()
        self.assertIn('WORKER_FINISHED', stdout.getvalue())
        self.assertIn('MEASURED IP TOTAL: 579', stdout.getvalue())

    def test_counter_disappearance_stops_worker(self):
        stdout = io.StringIO()
        with (patch.object(metered, 'start'),
              patch.object(metered, 'props',
                           return_value={'IPAccounting': 'no'}),
              patch.object(metered, 'stop') as stop,
              contextlib.redirect_stdout(stdout)):
            with self.assertRaises(metered.TrialError):
                metered.trial('trial.service')
        stop.assert_called_once_with('trial.service')
        self.assertIn('MEASURED IP TOTAL: 0', stdout.getvalue())

    def test_missing_original_worker_refuses_trial_before_service_start(self):
        with (patch.object(metered.os, 'geteuid', return_value=0),
              patch.object(metered, 'WORKER', Path('/nonexistent/worker')),
              patch.object(metered, 'run') as run):
            with self.assertRaisesRegex(metered.TrialError, 'WORKER_NOT_FOUND'):
                metered.guard_setup()
        run.assert_not_called()

    def test_explicit_execution_required(self):
        out = io.StringIO()
        with (patch('sys.argv', ['metered']),
              patch.object(metered, 'guard_setup') as guard,
              contextlib.redirect_stdout(out)):
            self.assertEqual(metered.main(), 0)
        guard.assert_not_called()
        self.assertIn('PREVIEW:', out.getvalue())

    def test_user_timer_active_is_a_hard_blocker(self):
        class FakeUser:
            pw_uid = 1000
        response = subprocess.CompletedProcess([], 0, 'active\n', '')
        with (patch.object(metered.os, 'geteuid', return_value=0),
              patch.object(metered, 'WORKER', Path(__file__)),
              patch.object(metered, 'ARCHIVE', Path(__file__).parent),
              patch.object(metered.pwd, 'getpwnam', return_value=FakeUser()),
              patch.object(metered, 'run',
                           side_effect=[
                               subprocess.CompletedProcess([], 0, '', ''),
                               response])):
            with self.assertRaisesRegex(
                    metered.TrialError, 'USER_INVENTORY_TIMER_STILL_ACTIVE'):
                metered.guard_setup()

    def test_unlinked_inactive_user_timer_is_permitted(self):
        class FakeUser:
            pw_uid = 1000
        status = subprocess.CompletedProcess([], 4, 'unknown\n', '')
        with (patch.object(metered.os, 'geteuid', return_value=0),
              patch.object(metered, 'WORKER', Path(__file__)),
              patch.object(metered, 'ARCHIVE', Path(__file__).parent),
              patch.object(metered.pwd, 'getpwnam', return_value=FakeUser()),
              patch.object(metered, 'run',
                           side_effect=[
                               subprocess.CompletedProcess([], 0, '', ''),
                               status])):
            metered.guard_setup()


if __name__ == '__main__':
    unittest.main()

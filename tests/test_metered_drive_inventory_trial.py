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
        self.assertIn('--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
                      command)
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
              patch.object(metered, 'BudgetLedger') as ledger,
              patch.object(metered, 'preflight',
                           side_effect=metered.TrialError('IP_COUNTER_UNAVAILABLE')),
              patch.object(metered, 'trial') as trial,
              patch.object(metered.os, 'geteuid', return_value=0),
              patch.object(metered.os, 'getpid', return_value=77),
              patch('sys.argv', ['metered', '--execute']),
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            ledger.return_value.__enter__.return_value.summary.return_value = 'safe'
            self.assertEqual(metered.main(), 2)
        trial.assert_not_called()

    def test_budget_crossing_stops_worker_and_reports_counts(self):
        sample = values(8_000_001, 122)
        stdout = io.StringIO()
        with (patch.object(metered, 'start') as start,
              patch.object(metered, 'props', return_value=sample),
              patch.object(metered, 'stop') as stop,
              patch.object(metered, 'report_trial_service_log') as log,
              contextlib.redirect_stdout(stdout)):
            metered.trial('trial.service')
        log.assert_called_once_with('trial.service')
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
              patch.object(metered, 'report_trial_service_log') as log,
              contextlib.redirect_stdout(stdout)):
            metered.trial('trial.service')
        log.assert_called_once()
        stop.assert_called_once()
        self.assertIn('WORKER_FINISHED', stdout.getvalue())
        self.assertIn('MEASURED IP TOTAL: 579', stdout.getvalue())

    def test_counter_disappearance_stops_worker(self):
        stdout = io.StringIO()
        with (patch.object(metered, 'start'),
              patch.object(metered, 'props',
                           return_value={'IPAccounting': 'no'}),
              patch.object(metered, 'stop') as stop,
              patch.object(metered, 'report_trial_service_log'),
              contextlib.redirect_stdout(stdout)):
            with self.assertRaises(metered.TrialError):
                metered.trial('trial.service')
        stop.assert_called_once_with('trial.service')
        self.assertIn('MEASURED IP TOTAL: 0', stdout.getvalue())

    def test_traffic_stop_happens_before_journal_query(self):
        events = []
        def stop(_unit):
            events.append('stop')
        def log(_unit):
            events.append('journal')
        sample = values(8_000_001, 0)
        with (patch.object(metered, 'start'),
              patch.object(metered, 'props', return_value=sample),
              patch.object(metered, 'stop', side_effect=stop),
              patch.object(metered, 'report_trial_service_log',
                           side_effect=log),
              contextlib.redirect_stdout(io.StringIO())):
            metered.trial('trial.service')
        self.assertEqual(events, ['stop', 'journal'])

    def test_failed_stop_blocks_ledger_commit_by_raising(self):
        failure = subprocess.CompletedProcess([], 1, '', 'systemd unavailable')
        with patch.object(metered, 'run', return_value=failure):
            with self.assertRaisesRegex(
                    metered.TrialError, 'SYSTEMD_STOP_UNCONFIRMED'):
                metered.stop('apple-sync-drive-metered-test.service')

    def test_missing_original_worker_refuses_trial_before_service_start(self):
        with (patch.object(metered.os, 'geteuid', return_value=0),
              patch.object(metered, 'WORKER', Path('/nonexistent/worker')),
              patch.object(metered, 'run') as run):
            with self.assertRaisesRegex(metered.TrialError, 'WORKER_NOT_FOUND'):
                metered.guard_setup()
        run.assert_not_called()

    def test_worker_entry_detects_unreachable_user_manager(self):
        failed = subprocess.CompletedProcess([], 1, '', 'Failed to connect to bus')
        out = io.StringIO()
        with (patch.object(metered, 'run', return_value=failed),
              patch.object(metered.os, 'execv') as execv,
              contextlib.redirect_stdout(out)):
            self.assertEqual(metered.worker_entry(), 3)
        self.assertIn('USER_MANAGER_STATUS_UNAVAILABLE', out.getvalue())
        execv.assert_not_called()

    def test_worker_entry_detects_active_backup_and_skips_inventory(self):
        idle = subprocess.CompletedProcess([], 0, 'inactive\n', '')
        busy = subprocess.CompletedProcess([], 0, 'active\n', '')
        out = io.StringIO()
        with (patch.object(metered, 'run', side_effect=[idle, busy]),
              patch.object(metered.os, 'execv') as execv,
              contextlib.redirect_stdout(out)):
            self.assertEqual(metered.worker_entry(), 3)
        self.assertIn('OTHER_BACKUP_ACTIVE apple-sync-photos.service',
                      out.getvalue())
        execv.assert_not_called()

    def test_worker_entry_execs_existing_worker_only_after_four_idle_checks(self):
        idle = subprocess.CompletedProcess([], 0, 'inactive\n', '')
        calls = []
        out = io.StringIO()
        with (patch.object(metered, 'run', side_effect=lambda c, **kw:
                           (calls.append(c), idle)[1]),
              patch.object(metered.os, 'execv') as execv,
              contextlib.redirect_stdout(out)):
            self.assertEqual(metered.worker_entry(), 0)
        self.assertEqual(len(calls), len(metered.BUSY_UNITS))
        execv.assert_called_once()
        worker_command = execv.call_args.args[1]
        self.assertIn('drive_inventory.py', worker_command[1])
        self.assertEqual(worker_command[-2:], ['--execute', '--resume-only'])
        self.assertIn('TRIAL_USER_MANAGER_OK', out.getvalue())

    def test_log_reports_only_sanitized_worker_status(self):
        out = io.StringIO()
        journal = subprocess.CompletedProcess(
            [], 0, 'secret-filename.pdf\nTRIAL_BLOCKED: sample\n'
            'Drive inventory DEFER: backup active\n', '')
        with (patch.object(metered, 'run', return_value=journal),
              contextlib.redirect_stdout(out)):
            metered.report_trial_service_log('trial.service')
        self.assertNotIn('secret-filename', out.getvalue())
        self.assertIn('TRIAL_BLOCKED: sample', out.getvalue())
        self.assertIn('Drive inventory DEFER:', out.getvalue())

    def test_explicit_execution_required(self):
        out = io.StringIO()
        with (patch('sys.argv', ['metered']),
              patch.object(metered, 'guard_setup') as guard,
              contextlib.redirect_stdout(out)):
            self.assertEqual(metered.main(), 0)
        guard.assert_not_called()
        self.assertIn('PREVIEW:', out.getvalue())

    def test_cumulative_multi_pass_invocation_commits_each_pass(self):
        output = io.StringIO()
        with (patch.object(metered, 'guard_setup'),
              patch.object(metered, 'BudgetLedger') as budget_ledger,
              patch.object(metered, 'preflight'),
              patch.object(metered, 'trial',
                           side_effect=[(2000, 1000), (1000, 2000),
                                        (3000, 4000)]) as trial,
              patch.object(metered.os, 'geteuid', return_value=0),
              patch.object(metered.os, 'getpid', return_value=99),
              patch('sys.argv', ['metered', '--execute', '--passes', '3']),
              contextlib.redirect_stdout(output)):
            ledger = budget_ledger.return_value.__enter__.return_value
            ledger.remaining = metered.CAP_BYTES
            ledger.summary.return_value = 'cumulative status'
            self.assertEqual(metered.main(), 0)
            self.assertEqual(trial.call_count, 3)
            self.assertEqual(ledger.begin.call_count, 3)
            self.assertEqual(ledger.complete.call_count, 3)
            ledger.complete.assert_any_call(3000, 4000)
            self.assertIn('METERED PASS 3/3', output.getvalue())

    def test_zero_traffic_ends_multipass_sequence(self):
        out = io.StringIO()
        with (patch.object(metered, 'guard_setup'),
              patch.object(metered, 'BudgetLedger') as budget_ledger,
              patch.object(metered, 'preflight'),
              patch.object(metered, 'trial',
                           return_value=(0, 0)) as trial,
              patch.object(metered.os, 'geteuid', return_value=0),
              patch('sys.argv', ['metered', '--execute', '--passes', '5']),
              contextlib.redirect_stdout(out)):
            ledger = budget_ledger.return_value.__enter__.return_value
            ledger.remaining = metered.CAP_BYTES
            ledger.summary.return_value = 'status'
            self.assertEqual(metered.main(), 0)
        trial.assert_called_once()
        self.assertIn('no network traffic', out.getvalue())

    def test_pending_run_blocks_before_preflight(self):
        with (patch.object(metered, 'guard_setup'),
              patch.object(metered, 'BudgetLedger') as budget_ledger,
              patch.object(metered, 'preflight') as preflight,
              patch.object(metered.os, 'geteuid', return_value=0),
              patch('sys.argv', ['metered', '--execute']),
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            ledger = budget_ledger.return_value.__enter__.return_value
            ledger.summary.return_value = 'pending'
            ledger.check_ready.side_effect = metered.BudgetError(
                'PREVIOUS_RUN_UNRECONCILED_NO_NEW_SCANS')
            self.assertEqual(metered.main(), 2)
        preflight.assert_not_called()

    def test_status_reads_only_ledger_no_scanner_or_preflight(self):
        out = io.StringIO()
        with (patch.object(metered, 'BudgetLedger') as budget_ledger,
              patch.object(metered, 'guard_setup') as guard,
              patch.object(metered, 'preflight') as preflight,
              patch.object(metered, 'trial') as trial,
              patch.object(metered.os, 'geteuid', return_value=0),
              patch('sys.argv', ['metered', '--status']),
              contextlib.redirect_stdout(out)):
            ledger = budget_ledger.return_value.__enter__.return_value
            ledger.summary.return_value = 'CUMULATIVE MEASURED: status'
            ledger.data = {'pending': None}
            self.assertEqual(metered.main(), 0)
        guard.assert_not_called()
        preflight.assert_not_called()
        trial.assert_not_called()
        self.assertIn('CUMULATIVE MEASURED', out.getvalue())

    def test_adaptive_cutoff_never_spends_the_remaining_reserve(self):
        out = io.StringIO()
        with (patch.object(metered, 'guard_setup'),
              patch.object(metered, 'BudgetLedger') as budget_ledger,
              patch.object(metered, 'preflight'),
              patch.object(metered, 'trial',
                           return_value=(100, 200)) as trial,
              patch.object(metered.os, 'geteuid', return_value=0),
              patch('sys.argv', ['metered', '--execute']),
              contextlib.redirect_stdout(out)):
            ledger = budget_ledger.return_value.__enter__.return_value
            ledger.remaining = metered.RESERVE_BYTES + 12345
            ledger.summary.return_value = 'status'
            self.assertEqual(metered.main(), 0)
        self.assertEqual(trial.call_args.kwargs['stop_at'], 12345)

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

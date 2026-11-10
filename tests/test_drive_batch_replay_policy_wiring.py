"""Offline integration tests for the pinned root-wiring command policy.

These tests mock the shared accounting controller and all systemd results.
They never launch subprocesses, contact Apple, mutate the ledger or install
a root unit. A supplied executor is always a recording fake.
"""
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_root_wiring as wiring
import drive_batch_replay_command_policy as commands
import drive_batch_replay_systemd_plan as plan

UID = 1000
PID = 1234
UNIT = 'apple-sync-drive-metered-replay-1234.service'
CHANNEL = Path('/run/user/1000/.apple-sync-replay-aBcDE123')
DEFERRED = {
    'status': 'REPLAY_DEFERRED_NO_TRAFFIC',
    'production_commit_authorized': False,
    'checkpoint_writes': 0,
}


class PolicyWiringTests(unittest.TestCase):
    def setUp(self):
        self.executed = []
        self.events = []
        self.preflight_stopped = False

    def backend(self, argv, timeout):
        self.executed.append((argv, timeout))
        if argv[:3] == ('systemctl', '--system', 'stop'):
            if 'ipaccounting-replay-check-' in argv[3]:
                self.preflight_stopped = True
        stopped = self.preflight_stopped
        raw = {
            'ActiveState': 'inactive' if stopped else 'active',
            'SubState': 'dead' if stopped else 'running',
            'ExecMainStatus': '0',
            'Result': 'success',
            'IPAccounting': 'yes',
            'IPIngressBytes': '0',
            'IPEgressBytes': '0',
        }
        props = ''.join(f'{k}={raw[k]}\n' for k in plan.PROPERTY_FIELDS)
        return SimpleNamespace(returncode=0, stdout=props, stderr='hidden')

    def invoke(self, operation, *, uid=UID, pid=PID, receipt_dir=CHANNEL,
               executor=None, ledger_factory=None, result=None):
        if result is None:
            result = dict(DEFERRED)

        def run_accounted(**kwargs):
            self.events.append('accounting')
            operation(kwargs)
            return result

        def ledger():
            self.events.append('ledger')
            return nullcontext(object())

        with (patch.object(wiring.accounting, 'run_accounted',
                           side_effect=run_accounted),
              patch.object(wiring.systemd, 'require_private_channel')):
            return wiring.run_wired(
                uid=uid, pid=pid, receipt_dir=receipt_dir,
                ledger_factory=ledger_factory or ledger,
                execute_argv=executor or self.backend,
                guard_setup=lambda: None,
                idle_window=lambda: True,
                monotonic=lambda: 0.0,
                sleep=lambda _seconds: None)

    def checked(self, kwargs):
        # The accounting controller receives adapter callbacks; inspect the
        # actual executor bound inside the adapter, not a separate policy.
        return kwargs['properties'].__self__._execute_argv

    def refuse(self, argv, timeout=15):
        with self.assertRaisesRegex(
                wiring.ReplayWiringError, '^REPLAY_WIRING_UNVERIFIED$'):
            self.invoke(lambda kwargs:
                        self.checked(kwargs)(argv, timeout))
        self.assertEqual(self.executed, [])

    def test_all_six_fixed_operations_forward_exactly_once(self):
        allowed = commands.ReplayCommandPolicy(
            uid=UID, pid=PID, receipt_dir=CHANNEL).planned_commands()
        result = self.invoke(lambda kwargs: [
            self.checked(kwargs)(argv, timeout)
            for argv, timeout in allowed])
        self.assertEqual(result, DEFERRED)
        self.assertEqual(self.executed, list(allowed))

    def test_adapter_worker_start_uses_checked_executor(self):
        result = self.invoke(lambda kwargs: kwargs['start_worker']())
        self.assertEqual(result, DEFERRED)
        self.assertEqual(self.executed, [
            (plan.worker_start_argv(UNIT, uid=UID, receipt_dir=CHANNEL), 15)])

    def test_adapter_worker_stop_uses_checked_executor(self):
        self.invoke(lambda kwargs: kwargs['stop_worker']())
        self.assertEqual(self.executed, [(plan.stop_argv(UNIT), 15)])

    def test_adapter_properties_uses_checked_executor(self):
        result = self.invoke(lambda kwargs: kwargs['properties']())
        self.assertEqual(result, DEFERRED)
        self.assertEqual(self.executed, [(plan.properties_argv(UNIT), 6)])

    def test_preflight_checks_start_show_stop_show_commands(self):
        result = self.invoke(lambda kwargs: kwargs['preflight']())
        self.assertEqual(result, DEFERRED)
        allowed = commands.ReplayCommandPolicy(
            uid=UID, pid=PID, receipt_dir=CHANNEL).planned_commands()
        self.assertEqual(self.executed,
                         [allowed[0], allowed[1], allowed[2], allowed[1]])

    def test_unrelated_systemctl_service_is_not_forwarded(self):
        self.refuse(
            ('systemctl', '--system', 'stop', 'google-drive-rclone.service'))

    def test_wrong_transient_unit_is_not_forwarded(self):
        self.refuse(plan.stop_argv(
            'apple-sync-drive-metered-replay-1235.service'))

    def test_wrong_preflight_pid_is_not_forwarded(self):
        self.refuse(plan.preflight_start_argv(PID + 1))

    def test_systemctl_enable_is_not_forwarded(self):
        self.refuse(('systemctl', '--system', 'enable', UNIT))

    def test_systemctl_daemon_reload_is_not_forwarded(self):
        self.refuse(('systemctl', 'daemon-reload'))

    def test_shell_command_is_not_forwarded(self):
        self.refuse(('/bin/sh', '-c', 'echo private'))

    def test_different_channel_is_not_forwarded(self):
        altered = Path('/run/user/1000/.apple-sync-replay-Other12345')
        self.refuse(plan.worker_start_argv(
            UNIT, uid=UID, receipt_dir=altered))

    def test_modified_worker_executable_is_not_forwarded(self):
        command = list(plan.worker_start_argv(
            UNIT, uid=UID, receipt_dir=CHANNEL))
        command[-2] = '/tmp/worker.py'
        self.refuse(tuple(command))

    def test_added_systemd_property_is_not_forwarded(self):
        command = plan.worker_start_argv(
            UNIT, uid=UID, receipt_dir=CHANNEL)
        self.refuse(command[:4] + ('--property=Environment=PYTHONPATH=/tmp',)
                    + command[4:])

    def test_list_argv_is_not_forwarded(self):
        self.refuse(list(plan.stop_argv(UNIT)))

    def test_wrong_timeout_is_not_forwarded(self):
        self.refuse(plan.properties_argv(UNIT), timeout=15)

    def test_boolean_timeout_is_not_forwarded(self):
        self.refuse(plan.stop_argv(UNIT), timeout=True)

    def test_invalid_context_refuses_before_opening_ledger(self):
        with self.assertRaisesRegex(
                wiring.ReplayWiringError, '^REPLAY_ADAPTER_UNTRUSTED$'):
            self.invoke(lambda _: self.fail('not reached'),
                        receipt_dir=Path('/tmp/untrusted'))
        self.assertNotIn('ledger', self.events)
        self.assertEqual(self.executed, [])

    def test_missing_executor_refuses_before_policy_and_ledger(self):
        with patch.object(
                wiring.commands, 'ReplayCommandPolicy') as check:
            with self.assertRaisesRegex(
                    wiring.ReplayWiringError,
                    '^REPLAY_TRUSTED_DEPENDENCY_MISSING$'):
                wiring.run_wired(
                    uid=UID, pid=PID, receipt_dir=CHANNEL,
                    ledger_factory=lambda: nullcontext(object()),
                    execute_argv=None,
                    guard_setup=lambda: None,
                    idle_window=lambda: True,
                    monotonic=lambda: 0.0,
                    sleep=lambda _seconds: None)
            check.assert_not_called()

    def test_unverified_accounting_result_still_refused(self):
        unsafe = {**DEFERRED, 'production_commit_authorized': True}
        with self.assertRaisesRegex(
                wiring.ReplayWiringError,
                '^REPLAY_RESULT_NOT_READ_ONLY$'):
            self.invoke(lambda kwargs: kwargs['properties'](),
                        result=unsafe)
        self.assertEqual(self.executed, [(plan.properties_argv(UNIT), 6)])

    def test_executor_exceptions_are_privacy_safe(self):
        def bad_backend(_argv, _timeout):
            self.executed.append(('attempted', 15))
            raise RuntimeError('/private/user/account/secret')
        with self.assertRaisesRegex(
                wiring.ReplayWiringError,
                '^REPLAY_WIRING_UNVERIFIED$') as caught:
            self.invoke(lambda kwargs: kwargs['properties'](),
                        executor=bad_backend)
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(self.executed, [('attempted', 15)])

    def test_root_wiring_contains_no_default_live_executor(self):
        body = Path(wiring.__file__).read_text(encoding='utf-8')
        self.assertIn('policy.validate(argv, timeout)', body)
        self.assertIn('execute_argv=policy_checked_executor', body)
        self.assertNotIn('import subprocess', body)
        self.assertNotIn('subprocess.run(', body)
        self.assertNotIn("if __name__ == '__main__'", body)


if __name__ == '__main__':
    unittest.main()

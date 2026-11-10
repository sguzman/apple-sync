"""Pure-mock integration QA: fixed root transport -> accounting wiring.

Every command callback is a fake. Nothing calls subprocess, Apple, systemd,
a real ledger, a service installer, or the user's saved inventory.
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
import drive_batch_replay_root_executor as boundary
import drive_batch_replay_command_policy as policy
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


class RootTransportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.events = []
        self.preflight_stopped = False
        self.fail_backend = False
        self.override = None

    def fake(self, argv, timeout, env):
        self.events.append('callback')
        self.calls.append((argv, timeout, env))
        if self.fail_backend:
            raise RuntimeError('/private/never-show-this')
        if self.override is not None:
            return self.override
        is_show = argv[:3] == (
            '/usr/bin/systemctl', '--system', 'show')
        is_preflight = 'ipaccounting-replay-check-' in ' '.join(argv)
        if (argv[:3] == ('/usr/bin/systemctl', '--system', 'stop')
                and is_preflight):
            self.preflight_stopped = True
        props = {
            'ActiveState': 'inactive'
            if is_preflight and self.preflight_stopped else 'active',
            'SubState': 'dead'
            if is_preflight and self.preflight_stopped else 'running',
            'ExecMainStatus': '0', 'Result': 'success',
            'IPAccounting': 'yes', 'IPIngressBytes': '0',
            'IPEgressBytes': '0',
        }
        output = ''.join(f'{key}={props[key]}\n'
                         for key in plan.PROPERTY_FIELDS) if is_show else ''
        return SimpleNamespace(returncode=0, stdout=output,
                               stderr='private backend stderr')

    def invoke(self, operation=None, *, uid=UID, pid=PID,
               receipt_dir=CHANNEL, runner=None, result=None,
               guard=None, idle=None, ledger_factory=None):
        if result is None:
            result = dict(DEFERRED)
        def accounted(**kwargs):
            self.events.append('accounting')
            if operation is not None:
                operation(kwargs)
            return result
        def ledger():
            self.events.append('ledger')
            return nullcontext(object())
        with (patch.object(wiring.os, 'geteuid', return_value=0),
              patch.object(wiring.accounting, 'run_accounted',
                           side_effect=accounted),
              patch.object(wiring.systemd, 'require_private_channel')):
            return wiring.run_wired_with_transport(
                uid=uid, pid=pid, receipt_dir=receipt_dir,
                ledger_factory=ledger_factory if ledger_factory is not None
                else ledger,
                invoke=self.fake if runner is None else runner,
                guard_setup=guard if guard is not None else lambda: None,
                idle_window=idle if idle is not None else lambda: True,
                monotonic=lambda: 0.0,
                sleep=lambda _: None)

    def callback(self, kwargs):
        return kwargs['properties'].__self__._execute_argv

    def planned(self):
        return policy.ReplayCommandPolicy(
            uid=UID, pid=PID, receipt_dir=CHANNEL).planned_commands()

    def test_all_six_commands_cross_both_validation_layers(self):
        allowed = self.planned()
        result = self.invoke(lambda kwargs: [
            self.callback(kwargs)(argv, timeout)
            for argv, timeout in allowed])
        self.assertEqual(result, DEFERRED)
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(
            [(argv[0], timeout) for argv, timeout, _ in self.calls],
            [('/usr/bin/' + argv[0], timeout) for argv, timeout in allowed])

    def test_controller_preflight_uses_only_pinned_commands(self):
        self.invoke(lambda kwargs: kwargs['preflight']())
        allowed = self.planned()
        observed = [(c[0][0], c[0][1:], c[1]) for c in self.calls]
        expected = [(('/usr/bin/' + argv[0]), argv[1:], timeout)
                    for argv, timeout in
                    (allowed[0], allowed[1], allowed[2], allowed[1])]
        self.assertEqual(observed, expected)

    def test_worker_start_uses_protected_root_worker_path(self):
        self.invoke(lambda kwargs: kwargs['start_worker']())
        self.assertEqual(len(self.calls), 1)
        args, timeout, _env = self.calls[0]
        self.assertEqual(timeout, 15)
        self.assertEqual(args[-2], str(plan.WORKER))
        self.assertEqual(args[-1], '--execute')
        self.assertIn('apple-sync-drive-metered', args[-2])

    def test_worker_stop_uses_absolute_systemctl(self):
        self.invoke(lambda kwargs: kwargs['stop_worker']())
        self.assertEqual(self.calls[0][0],
                         ('/usr/bin/systemctl', '--system', 'stop', UNIT))

    def test_properties_are_strictly_parsed_and_returned(self):
        captured = []
        self.invoke(lambda kwargs:
                    captured.append(kwargs['properties']()))
        self.assertEqual(captured[0]['IPAccounting'], 'yes')
        self.assertEqual(captured[0]['IPIngressBytes'], '0')

    def test_ambient_python_and_cloud_environment_is_not_forwarded(self):
        with patch.dict('os.environ', {
                'PYTHONPATH': '/private/attacker',
                'RCLONE_CONFIG_PASS': 'secret',
                'DBUS_SESSION_BUS_ADDRESS': 'private'}):
            self.invoke(lambda kwargs: kwargs['stop_worker']())
        env = self.calls[0][2]
        self.assertEqual(env, boundary.FIXED_ENV)
        self.assertNotIn('RCLONE_CONFIG_PASS', env)
        self.assertNotIn('PYTHONPATH', env)

    def test_result_has_no_raw_stderr_or_cloud_details(self):
        received = []
        self.invoke(lambda kwargs:
                    received.append(self.callback(kwargs)(
                        plan.stop_argv(UNIT), 15)))
        self.assertEqual(received[0].stdout, '')
        self.assertFalse(hasattr(received[0], 'stderr'))
        self.assertNotIn('private', repr(received))

    def test_invalid_user_path_fails_before_ledger(self):
        with self.assertRaisesRegex(
                wiring.ReplayWiringError,
                '^REPLAY_EXECUTOR_BOUNDARY_UNTRUSTED$'):
            self.invoke(receipt_dir=Path('/tmp/bad'))
        self.assertEqual(self.calls, [])
        self.assertNotIn('ledger', self.events)

    def test_invalid_pid_fails_before_ledger(self):
        with self.assertRaisesRegex(
                wiring.ReplayWiringError,
                '^REPLAY_EXECUTOR_BOUNDARY_UNTRUSTED$'):
            self.invoke(pid=0)
        self.assertEqual(self.calls, [])
        self.assertNotIn('ledger', self.events)

    def test_missing_invoker_fails_before_ledger(self):
        with self.assertRaisesRegex(
                wiring.ReplayWiringError,
                '^REPLAY_EXECUTOR_BOUNDARY_UNTRUSTED$'):
            self.invoke(runner=42)
        self.assertEqual(self.calls, [])
        self.assertNotIn('ledger', self.events)

    def test_nonroot_refuses_before_constructing_executor(self):
        with patch.object(wiring.os, 'geteuid', return_value=1000), \
                patch.object(wiring.executor, 'ReplayExecutorBoundary') as facade:
            with self.assertRaisesRegex(
                    wiring.ReplayWiringError,
                    '^REPLAY_ROOT_SERVICE_REQUIRED$'):
                wiring.run_wired_with_transport(
                    uid=UID, pid=PID, receipt_dir=CHANNEL,
                    ledger_factory=lambda: nullcontext(object()),
                    invoke=self.fake,
                    guard_setup=lambda: None, idle_window=lambda: True,
                    monotonic=lambda: 0.0, sleep=lambda _: None)
            facade.assert_not_called()

    def test_arbitrary_systemctl_operation_rejected_before_callback(self):
        with self.assertRaisesRegex(
                wiring.ReplayWiringError,
                '^REPLAY_WIRING_UNVERIFIED$'):
            self.invoke(lambda kwargs: self.callback(kwargs)(
                ('systemctl', '--system', 'enable', UNIT), 15))
        self.assertEqual(self.calls, [])

    def test_wrong_runtime_channel_rejected_before_callback(self):
        bad = Path('/run/user/1000/.apple-sync-replay-Different12345')
        with self.assertRaisesRegex(
                wiring.ReplayWiringError, '^REPLAY_WIRING_UNVERIFIED$'):
            self.invoke(lambda kwargs: self.callback(kwargs)(
                plan.worker_start_argv(UNIT, uid=UID, receipt_dir=bad),
                15))
        self.assertEqual(self.calls, [])

    def test_modified_worker_executable_rejected_before_callback(self):
        altered = list(plan.worker_start_argv(
            UNIT, uid=UID, receipt_dir=CHANNEL))
        altered[-2] = '/tmp/private_worker.py'
        with self.assertRaisesRegex(
                wiring.ReplayWiringError, '^REPLAY_WIRING_UNVERIFIED$'):
            self.invoke(lambda kwargs: self.callback(kwargs)(
                tuple(altered), 15))
        self.assertEqual(self.calls, [])

    def test_wrong_timeout_rejected_before_callback(self):
        with self.assertRaisesRegex(
                wiring.ReplayWiringError, '^REPLAY_WIRING_UNVERIFIED$'):
            self.invoke(lambda kwargs: self.callback(kwargs)(
                plan.properties_argv(UNIT), 15))
        self.assertEqual(self.calls, [])

    def test_backend_exception_is_sanitized(self):
        self.fail_backend = True
        with self.assertRaisesRegex(
                wiring.ReplayWiringError,
                '^REPLAY_WIRING_UNVERIFIED$') as caught:
            self.invoke(lambda kwargs: kwargs['stop_worker']())
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(len(self.calls), 1)

    def test_failed_systemd_properties_refuse_wiring_result(self):
        self.override = SimpleNamespace(
            returncode=0, stdout='ActiveState=active\n',
            stderr='')
        with self.assertRaisesRegex(
                wiring.ReplayWiringError, '^REPLAY_WIRING_UNVERIFIED$'):
            self.invoke(lambda kwargs: kwargs['properties']())
        self.assertEqual(len(self.calls), 1)

    def test_invalid_accounting_status_is_still_refused(self):
        unsafe = {**DEFERRED, 'production_commit_authorized': True}
        with self.assertRaisesRegex(
                wiring.ReplayWiringError,
                '^REPLAY_RESULT_NOT_READ_ONLY$'):
            self.invoke(result=unsafe)
        self.assertEqual(self.calls, [])

    def test_dormant_injected_path_has_no_subprocess_or_cli(self):
        body = Path(wiring.__file__).read_text(encoding='utf-8')
        self.assertIn('run_wired_with_transport(', body)
        self.assertIn('executor.ReplayExecutorBoundary(', body)
        self.assertNotIn('import subprocess', body)
        self.assertNotIn("if __name__ == '__main__'", body)
        self.assertEqual(self.calls, [])

    def test_transport_integration_is_not_installed_or_activated(self):
        import install_metered_launcher as install
        text = Path(install.__file__).read_text(encoding='utf-8')
        self.assertNotIn('drive_batch_replay_root_executor.py', text)
        self.assertNotIn('drive_batch_replay_root_wiring.py', text)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()

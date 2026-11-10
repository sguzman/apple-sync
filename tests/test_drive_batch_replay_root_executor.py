"""No-execution QA for the future root replay command-transport boundary."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_root_executor as executor
import drive_batch_replay_command_policy as policy
import drive_batch_replay_systemd_plan as plan
import drive_batch_replay_root_trust as trust

UID = 1000
PID = 1234
UNIT = 'apple-sync-drive-metered-replay-1234.service'
CHANNEL = Path('/run/user/1000/.apple-sync-replay-aBcDE123')


def fields():
    data = {
        'ActiveState': 'inactive',
        'SubState': 'dead',
        'ExecMainStatus': '0',
        'Result': 'success',
        'IPAccounting': 'yes',
        'IPIngressBytes': '11',
        'IPEgressBytes': '17',
    }
    return ''.join(f'{name}={data[name]}\n'
                   for name in plan.PROPERTY_FIELDS)


class RootExecutorTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.fail = None
        self.response = None

    def fake(self, cmd, timeout, environment):
        self.calls.append((cmd, timeout, environment))
        if self.fail is not None:
            raise self.fail
        if self.response is not None:
            return self.response
        return SimpleNamespace(
            returncode=0, stdout=fields()
            if cmd[:3] == ('/usr/bin/systemctl', '--system', 'show')
            else 'private nonstatus output', stderr='private stderr')

    def instance(self, runner=None, **overrides):
        values = dict(uid=UID, pid=PID, receipt_dir=CHANNEL)
        values.update(overrides)
        return executor.ReplayExecutorBoundary(
            **values, invoke=self.fake if runner is None else runner)

    def call(self, argv, timeout):
        with patch.object(executor.os, 'geteuid', return_value=0):
            return self.instance()(argv, timeout)

    def test_six_exact_commands_are_forwarded_with_absolute_programs(self):
        gate = self.instance()
        allowed = policy.ReplayCommandPolicy(
            uid=UID, pid=PID, receipt_dir=CHANNEL).planned_commands()
        with patch.object(executor.os, 'geteuid', return_value=0):
            outputs = [gate(args, timeout) for args, timeout in allowed]
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(len(outputs), 6)
        for (planned, timeout), (actual, passed_timeout, _env) in zip(
                allowed, self.calls):
            self.assertEqual(actual[0],
                             '/usr/bin/' + planned[0])
            self.assertEqual(actual[1:], planned[1:])
            self.assertEqual(timeout, passed_timeout)

    def test_show_returns_only_strict_systemd_properties(self):
        result = self.call(plan.properties_argv(UNIT), 6)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, fields())
        self.assertFalse(hasattr(result, 'stderr'))

    def test_nonstatus_stdout_and_stderr_are_dropped(self):
        result = self.call(plan.stop_argv(UNIT), 15)
        self.assertEqual(result.stdout, '')
        self.assertFalse(hasattr(result, 'stderr'))
        self.assertNotIn('private', repr(result))

    def test_only_fixed_environment_passed_to_backend(self):
        self.call(plan.stop_argv(UNIT), 15)
        env = self.calls[0][2]
        self.assertEqual(env, executor.FIXED_ENV)
        self.assertEqual(env['PATH'], '/usr/bin')
        self.assertEqual(env['HOME'], '/root')
        self.assertNotIn('DBUS_SESSION_BUS_ADDRESS', env)
        self.assertNotIn('XDG_RUNTIME_DIR', env)
        self.assertNotIn('PYTHONPATH', env)

    def test_inherited_private_environment_not_forwarded(self):
        with patch.dict(os.environ, {
                'PYTHONPATH': '/private/evil', 'RCLONE_CONFIG_PASS': 'secret',
                'DBUS_SESSION_BUS_ADDRESS': 'private user bus'}):
            self.call(plan.stop_argv(UNIT), 15)
        env = self.calls[0][2]
        self.assertNotIn('/private', repr(env))
        self.assertNotIn('RCLONE_CONFIG_PASS', env)

    def test_mutating_one_environment_copy_does_not_poison_future_run(self):
        def mutate(cmd, timeout, env):
            env['PATH'] = '/tmp/private'
            self.calls.append((cmd, timeout, env))
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.object(executor.os, 'geteuid', return_value=0):
            self.instance(runner=mutate)(plan.stop_argv(UNIT), 15)
        self.call(plan.stop_argv(UNIT), 15)
        self.assertEqual(self.calls[1][2]['PATH'], '/usr/bin')

    def test_missing_invoker_refuses_at_construction(self):
        with self.assertRaisesRegex(
                executor.ReplayExecutorError,
                '^REPLAY_EXECUTOR_INVOKER_REQUIRED$'):
            self.instance(runner=42)

    def test_invalid_receipt_context_refuses_before_callback(self):
        with self.assertRaisesRegex(
                executor.ReplayExecutorError,
                '^REPLAY_EXECUTOR_CONTEXT_UNTRUSTED$'):
            self.instance(receipt_dir=Path('/tmp/result'))
        self.assertEqual(self.calls, [])

    def test_nonroot_invocation_refuses_before_runner(self):
        with patch.object(executor.os, 'geteuid', return_value=1000):
            with self.assertRaisesRegex(
                    executor.ReplayExecutorError,
                    '^REPLAY_EXECUTOR_ROOT_REQUIRED$'):
                self.instance()(plan.stop_argv(UNIT), 15)
        self.assertEqual(self.calls, [])

    def test_arbitrary_shell_command_refused(self):
        with self.assertRaisesRegex(
                executor.ReplayExecutorError,
                '^REPLAY_EXECUTOR_COMMAND_NOT_ALLOWED$'):
            self.call(('/bin/sh', '-c', 'env'), 15)
        self.assertEqual(self.calls, [])

    def test_other_systemd_unit_refused(self):
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(plan.stop_argv(
                'apple-sync-drive-metered-replay-2222.service'), 15)
        self.assertEqual(self.calls, [])

    def test_systemctl_enable_refused(self):
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(('systemctl', '--system', 'enable', UNIT), 15)
        self.assertEqual(self.calls, [])

    def test_wrong_timeout_refused(self):
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(plan.properties_argv(UNIT), 15)
        self.assertEqual(self.calls, [])

    def test_bool_timeout_refused(self):
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(plan.stop_argv(UNIT), True)
        self.assertEqual(self.calls, [])

    def test_substituting_worker_script_is_refused(self):
        command = list(plan.worker_start_argv(
            UNIT, uid=UID, receipt_dir=CHANNEL))
        command[-2] = '/tmp/attacker.py'
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(tuple(command), 15)
        self.assertEqual(self.calls, [])

    def test_list_command_refused(self):
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(list(plan.stop_argv(UNIT)), 15)
        self.assertEqual(self.calls, [])

    def test_runner_exception_sanitized(self):
        self.fail = OSError('/private/cloud/account.json')
        with self.assertRaisesRegex(
                executor.ReplayExecutorError,
                '^REPLAY_EXECUTOR_COMMAND_UNVERIFIED$') as caught:
            self.call(plan.stop_argv(UNIT), 15)
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(len(self.calls), 1)

    def test_nonzero_exit_refuses_status(self):
        self.response = SimpleNamespace(
            returncode=1, stdout=fields(), stderr='private')
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(plan.properties_argv(UNIT), 6)

    def test_boolean_exit_status_is_not_success(self):
        self.response = SimpleNamespace(
            returncode=False, stdout=fields(), stderr='')
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(plan.properties_argv(UNIT), 6)

    def test_nonstring_stdout_refused(self):
        self.response = SimpleNamespace(
            returncode=0, stdout=b'private', stderr='')
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(plan.stop_argv(UNIT), 15)

    def test_missing_stderr_attribute_refused(self):
        self.response = SimpleNamespace(returncode=0, stdout='')
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(plan.stop_argv(UNIT), 15)

    def test_oversized_output_refused(self):
        self.response = SimpleNamespace(
            returncode=0, stdout='A' * 4097, stderr='')
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(plan.stop_argv(UNIT), 15)

    def test_oversized_stderr_refused(self):
        self.response = SimpleNamespace(
            returncode=0, stdout='', stderr='A' * 4097)
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(plan.stop_argv(UNIT), 15)

    def test_nul_byte_output_refused(self):
        self.response = SimpleNamespace(
            returncode=0, stdout='private\x00', stderr='')
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(plan.stop_argv(UNIT), 15)

    def test_malformed_systemd_property_set_refused(self):
        self.response = SimpleNamespace(
            returncode=0, stdout='ActiveState=inactive\n', stderr='')
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(plan.properties_argv(UNIT), 6)

    def test_duplicate_systemd_property_refused(self):
        self.response = SimpleNamespace(
            returncode=0, stdout=fields() + 'ActiveState=dead\n',
            stderr='')
        with self.assertRaises(executor.ReplayExecutorError):
            self.call(plan.properties_argv(UNIT), 6)

    def test_dataclass_result_is_immutable(self):
        from dataclasses import FrozenInstanceError
        result = self.call(plan.stop_argv(UNIT), 15)
        with self.assertRaises(FrozenInstanceError):
            result.returncode = 20

    def test_file_has_no_live_subprocess_or_entrypoint(self):
        source = Path(executor.__file__).read_text(encoding='utf-8')
        for token in ('import subprocess', 'subprocess.run(', 'Popen(',
                      'run_lifecycle(', "if __name__ == '__main__'"):
            self.assertNotIn(token, source)

    def test_manifest_includes_trusted_executor(self):
        self.assertIn('drive_batch_replay_root_executor.py',
                      trust.ROOT_REPLAY_MODULES)
        self.assertEqual(len(trust.ROOT_REPLAY_MODULES), 13)

    def test_adapter_accepts_executor_return_shape(self):
        result = self.call(plan.stop_argv(UNIT), 15)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(type(result.stdout), str)


if __name__ == '__main__':
    unittest.main()

"""Pure tests for an UNINSTALLED systemd replay IO adapter."""
from __future__ import annotations

from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_systemd_adapter as adapter
import drive_batch_replay_systemd_plan as plan
import drive_batch_replay_accounting as accounting

CHANNEL = Path('/run/user/1000/.apple-sync-replay-aBcDE123')
UNIT = 'apple-sync-drive-metered-replay-1234.service'
FIELDS = plan.PROPERTY_FIELDS


def state(*, active='active', substate='running', ingress=0,
          egress=0, accounting_enabled='yes', exit_status='0'):
    values = {
        'ActiveState': active, 'SubState': substate,
        'ExecMainStatus': exit_status, 'Result': 'success',
        'IPAccounting': accounting_enabled, 'IPIngressBytes': str(ingress),
        'IPEgressBytes': str(egress),
    }
    return ''.join(f'{key}={values[key]}\n' for key in FIELDS)


def result(text='', code=0):
    return SimpleNamespace(returncode=code, stdout=text, stderr='SECRET')


class FakeRunner:
    """Record commands; never execute them."""
    def __init__(self):
        self.calls = []
        self.preflight_samples = [
            state(),
            state(active='inactive', substate='dead'),
        ]
        self.worker_samples = [
            state(active='inactive', substate='dead', ingress=10, egress=5),
        ]
        self.fail_start = False
        self.fail_stop = False

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        assert isinstance(argv, tuple)
        if argv[0] == 'systemd-run' and '/usr/bin/sleep' in argv:
            return result(code=2 if self.fail_start else 0)
        if argv[0] == 'systemd-run':
            return result()
        if argv[:3] == ('systemctl', '--system', 'stop'):
            return result(code=2 if self.fail_stop else 0)
        if argv[:3] == ('systemctl', '--system', 'show'):
            if 'ipaccounting-replay-check-' in argv[3]:
                return result(self.preflight_samples.pop(0))
            return result(self.worker_samples.pop(0))
        raise AssertionError('Unexpected command passed by planner')


def new(runner=None):
    return adapter.ReplaySystemdAdapter(
        uid=1000, pid=1234, receipt_dir=CHANNEL,
        execute_argv=runner if runner is not None else FakeRunner())


class ReplaySystemdAdapterTests(unittest.TestCase):
    def test_constructor_does_not_call_runner(self):
        run = FakeRunner()
        obj = new(run)
        self.assertEqual(obj.unit, UNIT)
        self.assertEqual(run.calls, [])

    def test_requires_injected_executor(self):
        with self.assertRaisesRegex(adapter.ReplayAdapterError,
                                    'REPLAY_EXECUTOR_REQUIRED'):
            adapter.ReplaySystemdAdapter(
                uid=1000, pid=1234, receipt_dir=CHANNEL,
                execute_argv=None)

    def test_invalid_unit_pid_rejected_during_construction(self):
        for pid in (-1, 0, True, False, 2**31, 'private'):
            with self.subTest(pid=str(pid)), self.assertRaisesRegex(
                    adapter.ReplayAdapterError,
                    'REPLAY_ADAPTER_PARAMETERS_UNTRUSTED'):
                adapter.ReplaySystemdAdapter(
                    uid=1000, pid=pid, receipt_dir=CHANNEL,
                    execute_argv=FakeRunner())

    def test_invalid_channel_rejected_before_executor(self):
        run = FakeRunner()
        with self.assertRaisesRegex(adapter.ReplayAdapterError,
                                    'REPLAY_ADAPTER_PARAMETERS_UNTRUSTED'):
            adapter.ReplaySystemdAdapter(
                uid=1000, pid=1234, receipt_dir=Path('/tmp/private'),
                execute_argv=run)
        self.assertEqual(run.calls, [])

    def test_private_channel_requires_trusted_ancestors(self):
        with patch.object(adapter, '_is_trusted_directory', return_value=False):
            with self.assertRaisesRegex(adapter.ReplayAdapterError,
                                        'REPLAY_RUNTIME_UNTRUSTED'):
                adapter.require_private_channel(uid=1000, directory=CHANNEL)

    def test_private_channel_requires_user_owned_0700_dirs(self):
        seen = []
        def trusted(path, owner, *, private):
            seen.append((str(path), owner, private))
            return True
        with patch.object(adapter, '_is_trusted_directory', side_effect=trusted):
            adapter.require_private_channel(uid=1000, directory=CHANNEL)
        self.assertEqual(seen, [
            ('/run', 0, False), ('/run/user', 0, False),
            ('/run/user/1000', 1000, True),
            (str(CHANNEL), 1000, True),
        ])

    def test_private_channel_untrusted_leaf_refuses_start(self):
        run = FakeRunner()
        def trusted(path, _owner, *, private):
            return path != CHANNEL
        with patch.object(adapter, '_is_trusted_directory', side_effect=trusted):
            with self.assertRaisesRegex(adapter.ReplayAdapterError,
                                        'REPLAY_CHANNEL_UNTRUSTED'):
                new(run).start_worker()
        self.assertEqual(run.calls, [])

    def test_symlink_directory_rejected_by_lstat(self):
        fake = SimpleNamespace(st_mode=stat.S_IFLNK | 0o700, st_uid=1000)
        with patch.object(adapter.os, 'lstat', return_value=fake):
            self.assertFalse(adapter._is_trusted_directory(
                CHANNEL, 1000, private=True))

    def test_wrong_owner_and_world_writable_directory_rejected(self):
        bad = [
            SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=2000),
            SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=1000),
        ]
        for sample in bad:
            with self.subTest(mode=sample.st_mode):
                with patch.object(adapter.os, 'lstat', return_value=sample):
                    self.assertFalse(adapter._is_trusted_directory(
                        CHANNEL, 1000, private=True))

    def test_valid_trusted_directory_accepted(self):
        sample = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=1000)
        with patch.object(adapter.os, 'lstat', return_value=sample):
            self.assertTrue(adapter._is_trusted_directory(
                CHANNEL, 1000, private=True))

    def test_start_uses_exact_planner_arguments_not_shell(self):
        run = FakeRunner()
        with patch.object(adapter, 'require_private_channel') as channel:
            new(run).start_worker()
        channel.assert_called_once_with(uid=1000, directory=CHANNEL)
        self.assertEqual(run.calls, [
            (plan.worker_start_argv(UNIT, uid=1000, receipt_dir=CHANNEL), 15),
        ])
        self.assertNotIn('/bin/sh', run.calls[0][0])

    def test_properties_reads_exact_fields_and_parses(self):
        run = FakeRunner()
        actual = new(run).properties()
        self.assertEqual(actual['IPAccounting'], 'yes')
        self.assertEqual(actual['ActiveState'], 'inactive')
        self.assertEqual(run.calls[0],
                         (plan.properties_argv(UNIT), 6))

    def test_stop_does_not_reset_failed_before_counters(self):
        run = FakeRunner()
        new(run).stop_worker()
        self.assertEqual(run.calls, [(plan.stop_argv(UNIT), 15)])

    def test_properties_rejects_duplicates_missing_extra_and_malformed(self):
        for bad in (
            state() + 'IPAccounting=yes\n',
            state().replace('IPAccounting=yes\n', ''),
            state() + 'Private=token\n',
            'totally malformed', '', 'X' * 4097,
        ):
            with self.subTest(length=len(bad)), self.assertRaisesRegex(
                    adapter.ReplayAdapterError, 'REPLAY_PROPERTIES_UNVERIFIED'):
                adapter.parse_systemd_properties(bad)

    def test_systemd_failure_messages_are_never_leaked(self):
        run = lambda *_: result('Private/Cloud', code=1)
        with self.assertRaisesRegex(adapter.ReplayAdapterError,
                                    'REPLAY_SYSTEMD_COMMAND_UNVERIFIED') as error:
            new(run).properties()
        self.assertNotIn('Private', str(error.exception))

    def test_runner_exception_never_leaks_private_values(self):
        def fail(*_):
            raise RuntimeError('/private/credential')
        with self.assertRaisesRegex(adapter.ReplayAdapterError,
                                    'REPLAY_SYSTEMD_COMMAND_UNVERIFIED') as error:
            new(fail).stop_worker()
        self.assertNotIn('private', str(error.exception))

    def test_preflight_exact_start_query_stop_final_query(self):
        run = FakeRunner()
        obj = new(run)
        obj.preflight()
        self.assertEqual(
            [call[0] for call in run.calls],
            [plan.preflight_start_argv(1234),
             plan.preflight_properties_argv(1234),
             plan.preflight_stop_argv(1234),
             plan.preflight_properties_argv(1234)],
        )

    def test_preflight_failed_launch_still_stops(self):
        run = FakeRunner()
        run.fail_start = True
        with self.assertRaises(adapter.ReplayAdapterError):
            new(run).preflight()
        self.assertIn(plan.preflight_stop_argv(1234),
                      [call[0] for call in run.calls])

    def test_preflight_nonzero_traffic_refused_after_stop(self):
        run = FakeRunner()
        run.preflight_samples[0] = state(ingress=1)
        with self.assertRaisesRegex(adapter.ReplayAdapterError,
                                    'REPLAY_PREFLIGHT_UNVERIFIED'):
            new(run).preflight()
        self.assertIn(plan.preflight_stop_argv(1234),
                      [call[0] for call in run.calls])

    def test_preflight_stop_failure_refuses_new_traffic(self):
        run = FakeRunner()
        run.fail_stop = True
        with self.assertRaisesRegex(adapter.ReplayAdapterError,
                                    'REPLAY_PREFLIGHT_STOP_UNCONFIRMED'):
            new(run).preflight()

    def test_preflight_final_counters_must_be_zero(self):
        run = FakeRunner()
        run.preflight_samples[-1] = state(
            active='inactive', substate='dead', egress=3)
        with self.assertRaisesRegex(adapter.ReplayAdapterError,
                                    'REPLAY_PREFLIGHT_STOP_UNCONFIRMED'):
            new(run).preflight()

    def test_module_has_no_live_entrypoint(self):
        source = Path(adapter.__file__).read_text(encoding='utf-8')
        self.assertNotIn('import subprocess', source)
        self.assertNotIn('subprocess.run(', source)
        self.assertNotIn("if __name__ == '__main__'", source)


if __name__ == '__main__':
    unittest.main()

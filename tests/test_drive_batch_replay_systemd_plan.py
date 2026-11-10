"""No-systemd, no-network tests for pure replay systemd argv construction."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_systemd_plan as plan
import drive_batch_replay_worker as worker

PID = 1234
UID = 1000
UNIT = 'apple-sync-drive-metered-replay-1234.service'
CHANNEL = Path('/run/user/1000/.apple-sync-replay-aBcDE123')


def start(**updates):
    args = {'unit': UNIT, 'uid': UID, 'receipt_dir': CHANNEL}
    args.update(updates)
    return plan.worker_start_argv(
        args['unit'], uid=args['uid'], receipt_dir=args['receipt_dir'])


class ReplaySystemdPlanTests(unittest.TestCase):
    def test_correct_unit_name_generation(self):
        self.assertEqual(plan.unit_name(PID), UNIT)
        self.assertTrue(plan.accounting.UNIT_PATTERN.fullmatch(UNIT))

    def test_unit_rejects_bool_zero_negative_and_huge_pid(self):
        for pid in (True, False, 0, -1, 2**31, '1234', 12.5):
            with self.subTest(pid=pid), self.assertRaises(
                    plan.ReplaySystemdPlanError):
                plan.unit_name(pid)

    def test_worker_command_is_bounded_systemd_argv(self):
        argv = start()
        self.assertIsInstance(argv, tuple)
        self.assertEqual(argv[:3], ('systemd-run', '--system', '--quiet'))
        self.assertIn('--unit=' + UNIT, argv)
        self.assertIn('--property=Type=exec', argv)
        self.assertIn('--property=IPAccounting=yes', argv)
        self.assertIn('--property=KillMode=control-group', argv)
        self.assertIn('--property=RemainAfterExit=yes', argv)
        self.assertIn('--property=Restart=no', argv)
        self.assertIn('--property=NoNewPrivileges=yes', argv)
        self.assertIn('--property=TimeoutStopSec=5s', argv)
        self.assertIn('--property=RuntimeMaxSec=95s', argv)
        self.assertNotIn('--on-calendar', argv)

    def test_worker_execution_target_remains_dormant(self):
        argv = start()
        self.assertEqual(argv[-3:], (
            '/usr/bin/python3',
            '/home/sguzman/Code/Sync/apple-sync/scripts/drive_batch_replay_worker.py',
            '--execute'))
        self.assertTrue(
            Path(worker.__file__).name == 'drive_batch_replay_worker.py')
        # Worker ignores arguments and prints the dormant refusal; this
        # merely plans argv and does not execute anything.
        self.assertIn('REPLAY_WORKER_DORMANT',
                      Path(worker.__file__).read_text(encoding='utf-8'))

    def test_worker_is_regular_user_with_private_runtime(self):
        argv = start()
        self.assertIn('--property=User=sguzman', argv)
        self.assertIn('--setenv=HOME=/home/sguzman', argv)
        self.assertIn('--setenv=XDG_RUNTIME_DIR=/run/user/1000', argv)
        self.assertIn(
            '--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
            argv)

    def test_worker_receipt_is_separate_from_old_probe(self):
        argv = start()
        self.assertIn('--setenv=APPLE_SYNC_REPLAY_METERED=1', argv)
        self.assertIn(
            '--setenv=APPLE_SYNC_REPLAY_RECEIPT_DIR=/run/user/1000/.apple-sync-replay-aBcDE123',
            argv)
        self.assertFalse(any('APPLE_SYNC_BATCH_RECEIPT_DIR' in x
                             for x in argv))

    def test_no_shell_expansion_or_attacker_arguments(self):
        argv = start()
        self.assertNotIn('-c', argv)
        self.assertNotIn('sh', argv)
        self.assertNotIn('bash', argv)
        self.assertNotIn('sudo', argv)
        self.assertFalse(any(';' in x or '\n' in x for x in argv))
        self.assertNotIn('--user', argv)

    def test_bad_unit_name_cannot_start(self):
        bad = (
            'apple-sync-drive-metered-batch-probe-1234.service',
            'apple-sync-drive-metered-replay-abc.service',
            'apple-sync-drive-metered-replay-0.service',
            'apple-sync-drive-metered-replay-1234.service;rm -rf /',
            'apple-sync-drive-metered-replay-1234.timer',
            '', None, Path(UNIT),
        )
        for unit in bad:
            with self.subTest(unit=str(unit)), self.assertRaisesRegex(
                    plan.ReplaySystemdPlanError, 'REPLAY_UNIT_UNTRUSTED'):
                start(unit=unit)

    def test_bad_unit_name_cannot_query_or_stop(self):
        for builder in (plan.properties_argv, plan.stop_argv):
            with self.subTest(builder=builder.__name__), self.assertRaisesRegex(
                    plan.ReplaySystemdPlanError, 'REPLAY_UNIT_UNTRUSTED'):
                builder('ssh.service')

    def test_bad_uid_cannot_construct_private_runtime(self):
        for uid in (0, True, False, -1, 2**31, '1000', None):
            with self.subTest(uid=uid), self.assertRaisesRegex(
                    plan.ReplaySystemdPlanError, 'REPLAY_RUNTIME_UID_INVALID'):
                start(uid=uid)

    def test_wrong_uid_runtime_channel_fails(self):
        with self.assertRaisesRegex(
                plan.ReplaySystemdPlanError, 'REPLAY_CHANNEL_PATH_INVALID'):
            start(uid=1001)

    def test_relative_runtime_channel_is_rejected(self):
        with self.assertRaisesRegex(
                plan.ReplaySystemdPlanError, 'REPLAY_CHANNEL_PATH_INVALID'):
            start(receipt_dir=Path('.apple-sync-replay-aBcDE123'))

    def test_other_runtime_trees_and_tmp_are_rejected(self):
        paths = (Path('/tmp/.apple-sync-replay-aBcDE123'),
                 Path('/run/user/1/.apple-sync-replay-aBcDE123'),
                 Path('/run/user/1000/private/.apple-sync-replay-aBcDE123'),
                 Path('/var/lib/.apple-sync-replay-aBcDE123'))
        for path in paths:
            with self.subTest(path=str(path)), self.assertRaisesRegex(
                    plan.ReplaySystemdPlanError, 'REPLAY_CHANNEL_PATH_INVALID'):
                start(receipt_dir=path)

    def test_short_channel_name_is_rejected(self):
        with self.assertRaisesRegex(
                plan.ReplaySystemdPlanError, 'REPLAY_CHANNEL_PATH_INVALID'):
            start(receipt_dir=Path('/run/user/1000/.apple-sync-replay-abc'))

    def test_malicious_or_wrong_prefix_channel_name_rejected(self):
        for name in ('abcDE123', '.apple-sync-batch-probe-abcDE123',
                     '.apple-sync-replay-abc;rm -rf',
                     '.apple-sync-replay-1234567 space',
                     '.apple-sync-replay-abc🚀def'):
            with self.subTest(name=name), self.assertRaisesRegex(
                    plan.ReplaySystemdPlanError, 'REPLAY_CHANNEL_PATH_INVALID'):
                start(receipt_dir=Path('/run/user/1000') / name)

    def test_non_path_channel_is_rejected(self):
        for value in ('/run/user/1000/.apple-sync-replay-aBcDE123',
                      None, False, 1234):
            with self.subTest(channel_type=str(type(value))), self.assertRaises(
                    plan.ReplaySystemdPlanError):
                start(receipt_dir=value)

    def test_show_command_uses_only_trusted_accounting_fields(self):
        argv = plan.properties_argv(UNIT)
        self.assertEqual(argv[:4],
                         ('systemctl', '--system', 'show', UNIT))
        names = tuple(argv[i + 1] for i, v in enumerate(argv[:-1])
                      if v == '-p')
        self.assertEqual(names, plan.PROPERTY_FIELDS)
        self.assertEqual(len(set(names)), len(names))

    def test_stop_command_is_exact_and_unconditional(self):
        self.assertEqual(plan.stop_argv(UNIT),
                         ('systemctl', '--system', 'stop', UNIT))

    def test_sleep_preflight_is_no_apple_traffic(self):
        argv = plan.preflight_start_argv(PID)
        self.assertEqual(argv[:3], ('systemd-run', '--system', '--quiet'))
        self.assertEqual(argv[-2:], ('/usr/bin/sleep', '4'))
        self.assertIn(
            '--unit=apple-sync-ipaccounting-replay-check-1234.service',
            argv)
        self.assertIn('--property=IPAccounting=yes', argv)
        self.assertIn('--property=KillMode=control-group', argv)
        self.assertIn('--property=Restart=no', argv)
        self.assertNotIn(str(plan.WORKER), argv)

    def test_preflight_bad_pid_never_builds_a_command(self):
        for pid in (-1, 0, True, False, 2**31, '1234', None):
            with self.subTest(pid=pid), self.assertRaisesRegex(
                    plan.ReplaySystemdPlanError,
                    'REPLAY_PREFLIGHT_UNIT_INVALID'):
                plan.preflight_start_argv(pid)

    def test_pure_module_has_no_execution_entrypoint(self):
        source = Path(plan.__file__).read_text(encoding='utf-8')
        self.assertNotIn('import subprocess', source)
        self.assertNotIn('subprocess.run(', source)
        self.assertNotIn('os.system(', source)
        self.assertNotIn("if __name__ == '__main__'", source)

    def test_only_fixed_user_and_working_directory(self):
        argv = start()
        self.assertIn(
            '--property=WorkingDirectory=/home/sguzman/Code/Sync/apple-sync',
            argv)
        self.assertEqual(sum(x.startswith('--property=User=') for x in argv), 1)
        self.assertFalse(any('EnvironmentFile=' in x for x in argv))


if __name__ == '__main__':
    unittest.main()

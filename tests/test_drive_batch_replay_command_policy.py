"""Pure, offline QA for a future root-owned replay command allowlist.

No real subprocess, systemd, network, ledger, installer, or Apple operations.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_command_policy as policy
import drive_batch_replay_root_trust as trust
import drive_batch_replay_systemd_plan as plan

PID = 1234
UID = 1000
UNIT = 'apple-sync-drive-metered-replay-1234.service'
RECEIPT = Path('/run/user/1000/.apple-sync-replay-aBcDE123')


def approved():
    return policy.ReplayCommandPolicy(
        uid=UID, pid=PID, receipt_dir=RECEIPT)


class ReplayCommandPolicyTests(unittest.TestCase):
    def denied(self, argv, timeout):
        with self.assertRaisesRegex(
                policy.ReplayCommandPolicyError,
                '^REPLAY_COMMAND_NOT_ALLOWED$'):
            approved().validate(argv, timeout)

    def test_exact_six_approved_operations(self):
        commands = approved().planned_commands()
        self.assertEqual(len(commands), 6)
        self.assertEqual(len(set(commands)), 6)
        for args, timeout in commands:
            with self.subTest(args=args[:4]):
                self.assertIsNone(approved().validate(args, timeout))

    def test_preflight_is_listed_before_worker_operations(self):
        commands = approved().planned_commands()
        self.assertEqual([timeout for _, timeout in commands],
                         [15, 6, 15, 15, 6, 15])
        self.assertEqual(commands[0][0], plan.preflight_start_argv(PID))
        self.assertEqual(commands[1][0], plan.preflight_properties_argv(PID))
        self.assertEqual(commands[2][0], plan.preflight_stop_argv(PID))
        self.assertEqual(commands[3][0],
                         plan.worker_start_argv(
                             UNIT, uid=UID, receipt_dir=RECEIPT))
        self.assertEqual(commands[4][0], plan.properties_argv(UNIT))
        self.assertEqual(commands[5][0], plan.stop_argv(UNIT))

    def test_policy_context_is_fixed_at_construction(self):
        gate = approved()
        self.assertEqual(gate.uid, UID)
        self.assertEqual(gate.pid, PID)
        self.assertEqual(gate.receipt_dir, RECEIPT)

    def test_invalid_worker_pid_rejected(self):
        for pid in (True, False, 0, -1, 2**31, '1234', None):
            with self.subTest(pid=pid), self.assertRaisesRegex(
                    policy.ReplayCommandPolicyError,
                    '^REPLAY_COMMAND_CONTEXT_UNTRUSTED$'):
                policy.ReplayCommandPolicy(
                    uid=UID, pid=pid, receipt_dir=RECEIPT)

    def test_invalid_uid_rejected(self):
        for uid in (True, False, 0, -5, 2**31, '1000', None):
            with self.subTest(uid=uid), self.assertRaisesRegex(
                    policy.ReplayCommandPolicyError,
                    '^REPLAY_COMMAND_CONTEXT_UNTRUSTED$'):
                policy.ReplayCommandPolicy(
                    uid=uid, pid=PID, receipt_dir=RECEIPT)

    def test_mismatched_uid_and_receipt_path_rejected(self):
        with self.assertRaisesRegex(
                policy.ReplayCommandPolicyError,
                'REPLAY_COMMAND_CONTEXT_UNTRUSTED'):
            policy.ReplayCommandPolicy(
                uid=1001, pid=PID, receipt_dir=RECEIPT)

    def test_relative_or_wrong_receipt_path_rejected(self):
        for path in (Path('local'), Path('/tmp/result'),
                     Path('/run/user/1000/other'),
                     Path('/run/user/1000/.apple-sync-replay-x')):
            with self.subTest(path=str(path)), self.assertRaisesRegex(
                    policy.ReplayCommandPolicyError,
                    'REPLAY_COMMAND_CONTEXT_UNTRUSTED'):
                policy.ReplayCommandPolicy(
                    uid=UID, pid=PID, receipt_dir=path)

    def test_injected_arbitrary_command_rejected(self):
        self.denied(('id',), 15)

    def test_shell_command_rejected(self):
        self.denied(('/bin/sh', '-c', 'echo hello'), 15)

    def test_systemctl_enable_rejected(self):
        self.denied(('systemctl', '--system', 'enable', UNIT), 15)

    def test_systemctl_daemon_reload_rejected(self):
        self.denied(('systemctl', 'daemon-reload'), 15)

    def test_systemctl_restart_rejected(self):
        self.denied(('systemctl', '--system', 'restart', UNIT), 15)

    def test_unrelated_service_stop_rejected(self):
        self.denied(('systemctl', '--system', 'stop',
                     'google-drive-rclone.service'), 15)

    def test_other_well_formed_replay_unit_rejected(self):
        self.denied(plan.stop_argv(
            'apple-sync-drive-metered-replay-1235.service'), 15)

    def test_other_preflight_pid_rejected(self):
        self.denied(plan.preflight_start_argv(1235), 15)

    def test_wrong_worker_channel_rejected(self):
        different = Path('/run/user/1000/.apple-sync-replay-OtherChannel123')
        self.denied(plan.worker_start_argv(
            UNIT, uid=UID, receipt_dir=different), 15)

    def test_substitute_worker_executable_rejected(self):
        cmd, timeout = approved().planned_commands()[3]
        updated = list(cmd)
        updated[-2] = '/home/sguzman/Code/Sync/apple-sync/scripts/drive_batch_replay_worker.py'
        self.denied(tuple(updated), timeout)

    def test_python_import_override_rejected(self):
        cmd, timeout = approved().planned_commands()[3]
        updated = tuple(x for x in cmd if x != '-E')
        self.denied(updated, timeout)

    def test_additional_systemd_property_rejected(self):
        cmd, timeout = approved().planned_commands()[3]
        self.denied(cmd[:4] + ('--property=Environment=PYTHONPATH=/tmp',)
                    + cmd[4:], timeout)

    def test_command_text_not_accepted_instead_of_argv_tuple(self):
        self.denied('systemctl --system stop ' + UNIT, 15)

    def test_list_argv_not_accepted(self):
        cmd, timeout = approved().planned_commands()[5]
        self.denied(list(cmd), timeout)

    def test_non_string_element_rejected(self):
        self.denied(('systemctl', '--system', 'stop', 1234), 15)

    def test_nested_unhashable_element_rejected_safely(self):
        self.denied(('systemctl', ['--system'], 'stop', UNIT), 15)

    def test_extra_token_rejected(self):
        cmd, timeout = approved().planned_commands()[5]
        self.denied(cmd + ('--force',), timeout)

    def test_each_approved_command_rejects_wrong_timeout(self):
        for cmd, timeout in approved().planned_commands():
            with self.subTest(command=cmd[:4]):
                self.denied(cmd, 6 if timeout == 15 else 15)

    def test_noninteger_or_boolean_timeout_rejected(self):
        cmd, _ = approved().planned_commands()[0]
        for bad in (True, False, '15', 15.0, None, -1, 0, 600):
            with self.subTest(bad=bad):
                self.denied(cmd, bad)

    def test_unauthorized_command_rejects_even_good_timeout(self):
        self.denied(('systemd-run', '--system', '--quiet',
                     '--unit=' + UNIT, '/usr/bin/curl',
                     'https://example.invalid'), 15)

    def test_policy_never_authorizes_install_or_python_exec(self):
        for cmd, timeout in approved().planned_commands():
            self.assertIn(cmd[0], ('systemd-run', 'systemctl'))
            self.assertNotIn('pkexec', cmd)
            self.assertNotIn('sudo', cmd)
            self.assertNotIn('--on-calendar', cmd)
            self.assertNotIn('daemon-reload', cmd)

    def test_manifest_contains_root_command_policy(self):
        self.assertIn('drive_batch_replay_command_policy.py',
                      trust.ROOT_REPLAY_MODULES)
        self.assertNotIn('drive_batch_replay_command_policy.py',
                         trust.ROOT_WORKER_MODULES)
        self.assertEqual(len(trust.ROOT_REPLAY_MODULES), 12)

    def test_source_has_no_live_executor_or_cli(self):
        code = Path(policy.__file__).read_text(encoding='utf-8')
        for prohibited in (
            'import subprocess', 'subprocess.run(', 'subprocess.Popen(',
            'os.system(', 'os.execv(', 'BudgetLedger(', 'replay_once(',
            "if __name__ == '__main__'"):
            self.assertNotIn(prohibited, code)


if __name__ == '__main__':
    unittest.main()

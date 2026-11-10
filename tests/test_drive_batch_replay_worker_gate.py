"""Offline-only replay worker gate and planner identity contract tests."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_worker_gate as gate
import drive_batch_replay_systemd_plan as plan

UNIT = 'apple-sync-drive-metered-replay-4123.service'
RECEIPT = '/run/user/1000/.apple-sync-replay-aBcDE123'
CGROUP = '0::/system.slice/' + UNIT + '\n'
ENV = {
    'APPLE_SYNC_REPLAY_METERED': '1',
    'APPLE_SYNC_REPLAY_UNIT': UNIT,
    'APPLE_SYNC_REPLAY_RECEIPT_DIR': RECEIPT,
    'INVOCATION_ID': 'a1b2c3d4e5f60123456789abcdef0123',
}


class WorkerGateTests(unittest.TestCase):
    def checked(self, *, uid=1000, euid=1000, environ=None, cgroup=CGROUP):
        return gate.check_launch(
            uid=uid, euid=euid,
            environ=ENV if environ is None else environ,
            cgroup=cgroup)

    def test_exact_matching_cgroup_and_unit_is_accepted(self):
        result = self.checked()
        self.assertEqual(result.unit, UNIT)
        self.assertEqual(result.uid, 1000)
        self.assertEqual(result.receipt_dir, Path(RECEIPT))

    def test_launch_identity_is_immutable(self):
        identity = self.checked()
        with self.assertRaises(FrozenInstanceError):
            identity.unit = 'other.service'

    def test_planner_provides_matching_unit_marker(self):
        argv = plan.worker_start_argv(
            UNIT, uid=1000, receipt_dir=Path(RECEIPT))
        self.assertIn('--setenv=APPLE_SYNC_REPLAY_UNIT=' + UNIT, argv)
        self.assertIn('--setenv=APPLE_SYNC_REPLAY_METERED=1', argv)
        self.assertIn(
            '--setenv=APPLE_SYNC_REPLAY_RECEIPT_DIR=' + RECEIPT, argv)
        self.assertNotIn('--on-calendar', argv)

    def test_valid_cgroup_without_final_newline_is_accepted(self):
        self.assertEqual(self.checked(cgroup=CGROUP.rstrip('\n')).unit, UNIT)

    def test_wrong_uid_or_root_denied(self):
        for uid in (0, -1, True, False, '1000', None, 2**31):
            with self.subTest(uid=uid), self.assertRaisesRegex(
                    gate.ReplayWorkerGateError, 'REPLAY_WORKER_USER_UNTRUSTED'):
                self.checked(uid=uid)

    def test_wrong_effective_uid_denied(self):
        for euid in (0, 1001, True, False, '1000', None):
            with self.subTest(euid=euid), self.assertRaisesRegex(
                    gate.ReplayWorkerGateError, 'REPLAY_WORKER_USER_UNTRUSTED'):
                self.checked(euid=euid)

    def test_non_mapping_environment_denied(self):
        with self.assertRaisesRegex(
                gate.ReplayWorkerGateError, 'REPLAY_WORKER_ENV_UNTRUSTED'):
            self.checked(environ='APPLE_SYNC_REPLAY_METERED=1')

    def test_missing_required_environment_key_denied(self):
        for key in ENV:
            settings = dict(ENV)
            del settings[key]
            with self.subTest(key=key), self.assertRaisesRegex(
                    gate.ReplayWorkerGateError, 'REPLAY_WORKER_ENV_UNTRUSTED'):
                self.checked(environ=settings)

    def test_wrong_metered_marker_denied(self):
        for marker in ('0', 'true', True, 1, None, ''):
            settings = {**ENV, 'APPLE_SYNC_REPLAY_METERED': marker}
            with self.subTest(value=marker), self.assertRaisesRegex(
                    gate.ReplayWorkerGateError, 'REPLAY_WORKER_ENV_UNTRUSTED'):
                self.checked(environ=settings)

    def test_spoofed_or_malformed_invocation_id_denied(self):
        for marker in ('xyz', '', 'a'*31, 'a'*33, 'a'*32 + '\n', None):
            settings = {**ENV, 'INVOCATION_ID': marker}
            with self.subTest(value=marker), self.assertRaisesRegex(
                    gate.ReplayWorkerGateError, 'REPLAY_WORKER_ENV_UNTRUSTED'):
                self.checked(environ=settings)

    def test_wrong_unit_name_denied_before_cgroup(self):
        for unit in ('other.service',
                     'apple-sync-drive-metered-batch-probe-4123.service',
                     'apple-sync-drive-metered-replay-0.service',
                     UNIT + '\n', '', None, 1):
            settings = {**ENV, 'APPLE_SYNC_REPLAY_UNIT': unit}
            with self.subTest(value=unit), self.assertRaisesRegex(
                    gate.ReplayWorkerGateError, 'REPLAY_WORKER_ENV_UNTRUSTED'):
                self.checked(environ=settings)

    def test_well_formed_but_different_unit_is_not_accepted(self):
        settings = {**ENV, 'APPLE_SYNC_REPLAY_UNIT':
                    'apple-sync-drive-metered-replay-4124.service'}
        with self.assertRaisesRegex(
                gate.ReplayWorkerGateError, 'REPLAY_WORKER_CGROUP_UNTRUSTED'):
            self.checked(environ=settings)

    def test_mismatched_actual_cgroup_denied(self):
        other = '0::/system.slice/apple-sync-drive-metered-replay-4124.service\n'
        with self.assertRaisesRegex(
                gate.ReplayWorkerGateError, 'REPLAY_WORKER_CGROUP_UNTRUSTED'):
            self.checked(cgroup=other)

    def test_user_slice_and_scope_denied(self):
        for group in (
                '0::/user.slice/user-1000.slice/user@1000.service/app.slice/\n',
                '0::/user.slice/' + UNIT + '\n',
                '0::/system.slice/run-123.scope\n'):
            with self.subTest(group=group), self.assertRaisesRegex(
                    gate.ReplayWorkerGateError, 'REPLAY_WORKER_CGROUP_UNTRUSTED'):
                self.checked(cgroup=group)

    def test_legacy_cgroup_memberships_denied(self):
        for group in ('1:name=systemd:/system.slice/' + UNIT + '\n',
                      '11:memory:/system.slice/' + UNIT + '\n'):
            with self.subTest(group=group), self.assertRaisesRegex(
                    gate.ReplayWorkerGateError, 'REPLAY_WORKER_CGROUP_UNTRUSTED'):
                self.checked(cgroup=group)

    def test_nested_cgroup_and_suffix_denied(self):
        for group in (CGROUP.rstrip('\n') + '/child\n',
                      CGROUP.rstrip('\n') + '/subgroup\n',
                      CGROUP.rstrip('\n') + ' ', CGROUP + '\n'):
            with self.subTest(group=group), self.assertRaisesRegex(
                    gate.ReplayWorkerGateError, 'REPLAY_WORKER_CGROUP_UNTRUSTED'):
                self.checked(cgroup=group)

    def test_multiple_cgroup_lines_denied(self):
        with self.assertRaisesRegex(
                gate.ReplayWorkerGateError, 'REPLAY_WORKER_CGROUP_UNTRUSTED'):
            self.checked(cgroup=CGROUP + CGROUP)

    def test_missing_or_giant_cgroup_denied(self):
        for value in ('', 'x' * (gate.MAX_CGROUP_BYTES + 1), None, 1234):
            with self.subTest(value_type=type(value).__name__), \
                    self.assertRaisesRegex(
                        gate.ReplayWorkerGateError,
                        'REPLAY_WORKER_CGROUP_UNTRUSTED'):
                self.checked(cgroup=value)

    def test_wrong_user_receipt_runtime_denied(self):
        settings = {**ENV, 'APPLE_SYNC_REPLAY_RECEIPT_DIR':
                    '/run/user/1001/.apple-sync-replay-aBcDE123'}
        with self.assertRaisesRegex(
                gate.ReplayWorkerGateError, 'REPLAY_WORKER_CHANNEL_UNTRUSTED'):
            self.checked(environ=settings)

    def test_world_writable_receipt_location_is_not_accepted_by_shape(self):
        for value in ('/tmp/.apple-sync-replay-aBcDE123',
                      '/var/tmp/.apple-sync-replay-aBcDE123',
                      '/run/user/1000/private/.apple-sync-replay-aBcDE123',
                      'relative/path', '', None):
            settings = {**ENV, 'APPLE_SYNC_REPLAY_RECEIPT_DIR': value}
            with self.subTest(value=value), self.assertRaisesRegex(
                    gate.ReplayWorkerGateError, 'REPLAY_WORKER_CHANNEL_UNTRUSTED'):
                self.checked(environ=settings)

    def test_malformed_receipt_channel_token_denied(self):
        for value in ('/run/user/1000/.apple-sync-replay-short',
                      '/run/user/1000/.apple-sync-replay-abc defghi',
                      '/run/user/1000/.apple-sync-replay-abcdefghi;rm'):
            settings = {**ENV, 'APPLE_SYNC_REPLAY_RECEIPT_DIR': value}
            with self.subTest(value=value), self.assertRaisesRegex(
                    gate.ReplayWorkerGateError, 'REPLAY_WORKER_CHANNEL_UNTRUSTED'):
                self.checked(environ=settings)

    def test_forged_environment_cannot_compensate_for_wrong_cgroup(self):
        settings = dict(ENV)
        settings['INVOCATION_ID'] = 'f'*32
        with self.assertRaisesRegex(
                gate.ReplayWorkerGateError, 'REPLAY_WORKER_CGROUP_UNTRUSTED'):
            self.checked(environ=settings, cgroup='0::/user.slice/forged.service')

    def test_gate_does_not_claim_ip_accounting_or_equivalence(self):
        identity = self.checked()
        self.assertEqual(set(identity.__dict__),
                         {'unit', 'receipt_dir', 'uid'})
        self.assertNotIn('equivalent', str(identity))
        self.assertNotIn('ip_accounting_verified', str(identity))

    def test_gate_module_has_no_live_cli_or_io(self):
        src = Path(gate.__file__).read_text(encoding='utf-8')
        for forbidden in ('import subprocess', 'subprocess.run(',
                          'os.system(', 'RcloneRCReader(', 'replay_once(',
                          "if __name__ == '__main__'"):
            self.assertNotIn(forbidden, src)

    def test_gate_has_no_implicit_checkpoint_writes(self):
        src = Path(gate.__file__).read_text(encoding='utf-8')
        self.assertNotIn('save_progress(', src)
        self.assertNotIn('ledger.begin(', src)
        self.assertNotIn('ledger.complete(', src)


if __name__ == '__main__':
    unittest.main()

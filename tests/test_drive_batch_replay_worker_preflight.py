"""No-Apple, no-systemd tests for the worker's actual-process preflight."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_worker_preflight as preflight
import drive_batch_replay_systemd_adapter as adapter

UNIT = 'apple-sync-drive-metered-replay-4123.service'
CGROUP = '0::/system.slice/' + UNIT + '\n'
CHANNEL = Path('/run/user/1000/.apple-sync-replay-aBcDE123')
ENV = {
    'APPLE_SYNC_REPLAY_METERED': '1',
    'APPLE_SYNC_REPLAY_UNIT': UNIT,
    'APPLE_SYNC_REPLAY_RECEIPT_DIR': str(CHANNEL),
    'INVOCATION_ID': 'a1b2c3d4e5f60123456789abcdef0123',
}


class WorkerPreflightTests(unittest.TestCase):
    def validated(self, *, uid=1000, euid=1000, cgroup=CGROUP, env=None,
                  directory_check=None):
        if env is None:
            env = ENV
        if directory_check is None:
            directory_check = lambda **kwargs: None
        with (patch.object(preflight.os, 'getuid', return_value=uid),
              patch.object(preflight.os, 'geteuid', return_value=euid),
              patch.object(preflight.os, 'environ', env),
              patch.object(preflight, '_own_cgroup', return_value=cgroup) as own,
              patch.object(preflight.systemd, 'require_private_channel',
                           side_effect=directory_check) as channel):
            identity = preflight.validate_own_process()
            return identity, own.call_count, channel.call_args

    def test_valid_process_identity_and_channel_accepted(self):
        identity, reads, checked = self.validated()
        self.assertEqual(identity.unit, UNIT)
        self.assertEqual(identity.uid, 1000)
        self.assertEqual(identity.receipt_dir, CHANNEL)
        self.assertEqual(reads, 1)
        self.assertEqual(checked.kwargs,
                         {'uid': 1000, 'directory': CHANNEL})

    def test_real_uid_root_refuses_before_proc_read(self):
        with (patch.object(preflight.os, 'getuid', return_value=0),
              patch.object(preflight.os, 'geteuid', return_value=0),
              patch.object(preflight, '_own_cgroup') as proc):
            with self.assertRaisesRegex(
                    preflight.ReplayWorkerPreflightError,
                    'REPLAY_WORKER_USER_UNTRUSTED'):
                preflight.validate_own_process()
            proc.assert_not_called()

    def test_mismatched_effective_uid_refused_before_proc_read(self):
        with (patch.object(preflight.os, 'getuid', return_value=1000),
              patch.object(preflight.os, 'geteuid', return_value=1001),
              patch.object(preflight, '_own_cgroup') as proc):
            with self.assertRaisesRegex(
                    preflight.ReplayWorkerPreflightError,
                    'REPLAY_WORKER_USER_UNTRUSTED'):
                preflight.validate_own_process()
            proc.assert_not_called()

    def test_proc_open_uses_fixed_nofollow_source(self):
        with (patch.object(preflight.os, 'open', return_value=99) as opening,
              patch.object(preflight.os, 'read',
                           return_value=CGROUP.encode()) as reading,
              patch.object(preflight.os, 'close') as closing):
            self.assertEqual(preflight._own_cgroup(), CGROUP)
        opening.assert_called_once_with(
            '/proc/self/cgroup', preflight.CGROUP_OPEN_FLAGS)
        reading.assert_called_once_with(99, preflight.gate.MAX_CGROUP_BYTES + 1)
        closing.assert_called_once_with(99)
        self.assertTrue(preflight.CGROUP_OPEN_FLAGS & os.O_NOFOLLOW)

    def test_proc_open_error_is_sanitized(self):
        with patch.object(preflight.os, 'open',
                          side_effect=OSError('/private/proc error')):
            with self.assertRaisesRegex(
                    preflight.ReplayWorkerPreflightError,
                    'REPLAY_WORKER_CGROUP_UNAVAILABLE') as caught:
                preflight._own_cgroup()
        self.assertNotIn('private', str(caught.exception))

    def test_proc_read_error_closes_descriptor(self):
        with (patch.object(preflight.os, 'open', return_value=99),
              patch.object(preflight.os, 'read',
                           side_effect=OSError('/private')) as reading,
              patch.object(preflight.os, 'close') as closing):
            with self.assertRaisesRegex(
                    preflight.ReplayWorkerPreflightError,
                    'REPLAY_WORKER_CGROUP_UNAVAILABLE'):
                preflight._own_cgroup()
        self.assertEqual(reading.call_count, 1)
        closing.assert_called_once_with(99)

    def test_empty_proc_membership_rejected(self):
        with (patch.object(preflight.os, 'open', return_value=99),
              patch.object(preflight.os, 'read', return_value=b''),
              patch.object(preflight.os, 'close')):
            with self.assertRaisesRegex(
                    preflight.ReplayWorkerPreflightError,
                    'REPLAY_WORKER_CGROUP_UNTRUSTED'):
                preflight._own_cgroup()

    def test_oversize_proc_membership_rejected(self):
        oversized = b'A' * (preflight.gate.MAX_CGROUP_BYTES + 1)
        with (patch.object(preflight.os, 'open', return_value=99),
              patch.object(preflight.os, 'read', return_value=oversized),
              patch.object(preflight.os, 'close')):
            with self.assertRaisesRegex(
                    preflight.ReplayWorkerPreflightError,
                    'REPLAY_WORKER_CGROUP_UNTRUSTED'):
                preflight._own_cgroup()

    def test_invalid_utf8_proc_membership_rejected(self):
        with (patch.object(preflight.os, 'open', return_value=99),
              patch.object(preflight.os, 'read', return_value=b'\xff\xfe'),
              patch.object(preflight.os, 'close')):
            with self.assertRaisesRegex(
                    preflight.ReplayWorkerPreflightError,
                    'REPLAY_WORKER_CGROUP_UNTRUSTED'):
                preflight._own_cgroup()

    def test_wrong_actual_systemd_unit_rejected(self):
        wrong = ('0::/system.slice/'
                 'apple-sync-drive-metered-replay-4124.service\n')
        with self.assertRaisesRegex(
                preflight.ReplayWorkerPreflightError,
                'REPLAY_WORKER_CGROUP_UNTRUSTED'):
            self.validated(cgroup=wrong)

    def test_user_session_cgroup_rejected(self):
        with self.assertRaisesRegex(
                preflight.ReplayWorkerPreflightError,
                'REPLAY_WORKER_CGROUP_UNTRUSTED'):
            self.validated(cgroup='0::/user.slice/user-1000.slice\n')

    def test_forged_environment_marker_rejected(self):
        with self.assertRaisesRegex(
                preflight.ReplayWorkerPreflightError,
                'REPLAY_WORKER_ENV_UNTRUSTED'):
            self.validated(env={**ENV, 'APPLE_SYNC_REPLAY_METERED': '1;true'})

    def test_missing_receipt_environment_rejected(self):
        settings = dict(ENV)
        del settings['APPLE_SYNC_REPLAY_RECEIPT_DIR']
        with self.assertRaisesRegex(
                preflight.ReplayWorkerPreflightError,
                'REPLAY_WORKER_ENV_UNTRUSTED'):
            self.validated(env=settings)

    def test_wrong_receipt_environment_path_rejected(self):
        with self.assertRaisesRegex(
                preflight.ReplayWorkerPreflightError,
                'REPLAY_WORKER_CHANNEL_UNTRUSTED'):
            self.validated(env={
                **ENV, 'APPLE_SYNC_REPLAY_RECEIPT_DIR':
                '/tmp/.apple-sync-replay-aBcDE123'})

    def test_directory_not_owned_or_wrong_mode_rejected(self):
        def reject(**_kwargs):
            raise adapter.ReplayAdapterError('REPLAY_CHANNEL_UNTRUSTED')
        with self.assertRaisesRegex(
                preflight.ReplayWorkerPreflightError,
                'REPLAY_WORKER_CHANNEL_UNTRUSTED'):
            self.validated(directory_check=reject)

    def test_directory_oserror_is_sanitized(self):
        def reject(**_kwargs):
            raise OSError('/private/user/path')
        with self.assertRaisesRegex(
                preflight.ReplayWorkerPreflightError,
                'REPLAY_WORKER_CHANNEL_UNTRUSTED') as caught:
            self.validated(directory_check=reject)
        self.assertNotIn('private', str(caught.exception))

    def test_real_directory_validator_checks_expected_components(self):
        with (patch.object(preflight.os, 'getuid', return_value=1000),
              patch.object(preflight.os, 'geteuid', return_value=1000),
              patch.object(preflight.os, 'environ', ENV),
              patch.object(preflight, '_own_cgroup', return_value=CGROUP),
              patch.object(adapter, '_is_trusted_directory',
                           return_value=True) as trusted):
            result = preflight.validate_own_process()
        self.assertEqual(result.receipt_dir, CHANNEL)
        self.assertEqual(trusted.call_count, 4)
        self.assertEqual([call.args[0] for call in trusted.call_args_list],
                         [Path('/run'), Path('/run/user'),
                          Path('/run/user/1000'), CHANNEL])

    def test_untrusted_real_runtime_component_refused(self):
        with (patch.object(preflight.os, 'getuid', return_value=1000),
              patch.object(preflight.os, 'geteuid', return_value=1000),
              patch.object(preflight.os, 'environ', ENV),
              patch.object(preflight, '_own_cgroup', return_value=CGROUP),
              patch.object(adapter, '_is_trusted_directory',
                           return_value=False)):
            with self.assertRaisesRegex(
                    preflight.ReplayWorkerPreflightError,
                    'REPLAY_WORKER_CHANNEL_UNTRUSTED'):
                preflight.validate_own_process()

    def test_preflight_does_not_call_replay_or_ledger(self):
        source = Path(preflight.__file__).read_text(encoding='utf-8')
        for invocation in ('replay_once(', 'ledger.begin(',
                           'ledger.complete(', 'RcloneRCReader(',
                           'systemd-run'):
            self.assertNotIn(invocation, source)

    def test_preflight_has_no_network_cli_or_subprocess(self):
        source = Path(preflight.__file__).read_text(encoding='utf-8')
        self.assertNotIn('import subprocess', source)
        self.assertNotIn('subprocess.run(', source)
        self.assertNotIn("if __name__ == '__main__'", source)


if __name__ == '__main__':
    unittest.main()

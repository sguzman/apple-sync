"""Offline-only replay lease lifecycle tests: never launch Apple or systemd."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_channel as channel
import drive_batch_replay_lifecycle as lifecycle
import drive_batch_replay_root_wiring as wiring


def settled(status='REPLAY_OBSERVATION'):
    result = {
        'status': status,
        'production_commit_authorized': False,
        'checkpoint_writes': 0,
        'measured_ip_bytes': 227110,
    }
    if status == 'REPLAY_OBSERVATION':
        result['outcome'] = 'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED'
    if status == 'REPLAY_WORKER_FAILED':
        result['code'] = 'REPLAY_BACKUP_INTERRUPTED'
    if status == 'REPLAY_DEFERRED_NO_TRAFFIC':
        result.pop('measured_ip_bytes')
    return result


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        if os.getuid() == 0:
            self.skipTest('test channel allocation uses ordinary unprivileged uid')
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runtime = Path(temp.name) / 'user-runtime'
        self.runtime.mkdir(mode=0o700)
        self.runtime.chmod(0o700)
        self.lease = self._new_lease()
        self.addCleanup(self.lease.close)

    def _new_lease(self):
        fd = os.open(self.runtime, channel.O_DIR)
        try:
            return channel._create_at_fd(
                fd, uid=os.getuid(), gid=os.getgid(),
                runtime_path=self.runtime)
        finally:
            os.close(fd)

    def call(self, result=None, *, error=None, lease=None, allocation=None,
             executor=None, guard=None):
        if result is None:
            result = settled()
        chosen = self.lease if lease is None else lease
        events = []
        def run(**kwargs):
            events.append('run')
            if error is not None:
                raise error
            return result
        def make():
            events.append('allocate')
            if allocation is not None:
                return allocation()
            return chosen
        with (patch.object(lifecycle.os, 'geteuid', return_value=0),
              patch.object(lifecycle.wiring, 'run_wired',
                           side_effect=run) as root):
            output = lifecycle.run_lifecycle(
                pid=1234, allocate_channel=make,
                ledger_factory=lambda: object(),
                execute_argv=executor if executor is not None
                else (lambda argv, timeout: None),
                guard_setup=guard if guard is not None else (lambda: None),
                idle_window=lambda: True,
                monotonic=lambda: 0,
                sleep=lambda _seconds: None)
        return output, events, root

    def test_observation_settles_and_removes_private_channel(self):
        path = self.lease.path
        result, events, root = self.call()
        self.assertEqual(events, ['allocate', 'run'])
        self.assertEqual(result['status'], 'REPLAY_OBSERVATION')
        self.assertFalse(path.exists())
        self.assertTrue(self.lease.closed)
        self.assertEqual(root.call_count, 1)

    def test_root_wiring_receives_exact_lease_path_and_uid(self):
        _, _, root = self.call()
        args = root.call_args.kwargs
        self.assertEqual(args['uid'], self.lease.uid)
        self.assertEqual(args['receipt_dir'], self.lease.path)
        self.assertEqual(args['pid'], 1234)

    def test_cleanup_removes_only_single_trusted_receipt(self):
        receipt = self.lease.path / channel.RESULT_NAME
        receipt.write_bytes(b'{"version": 1}')
        receipt.chmod(0o600)
        self.call()
        self.assertFalse(receipt.exists())
        self.assertFalse(self.lease.path.exists())

    def test_interrupted_settled_run_may_cleanup_after_confirmed_stop(self):
        result, _, _ = self.call(settled('REPLAY_WORKER_INTERRUPTED'))
        self.assertEqual(result['status'], 'REPLAY_WORKER_INTERRUPTED')
        self.assertFalse(self.lease.path.exists())

    def test_failed_worker_with_verified_settlement_may_cleanup(self):
        result, _, _ = self.call(settled('REPLAY_WORKER_FAILED'))
        self.assertEqual(result['code'], 'REPLAY_BACKUP_INTERRUPTED')
        self.assertFalse(self.lease.path.exists())

    def test_failed_receipt_verification_still_allows_stopped_cleanup(self):
        result, _, _ = self.call(settled('REPLAY_OBSERVATION_UNVERIFIED'))
        self.assertEqual(result['status'], 'REPLAY_OBSERVATION_UNVERIFIED')
        self.assertFalse(self.lease.path.exists())

    def test_no_traffic_defer_did_not_launch_and_cleanup_is_safe(self):
        result, _, _ = self.call(settled('REPLAY_DEFERRED_NO_TRAFFIC'))
        self.assertEqual(result['status'], 'REPLAY_DEFERRED_NO_TRAFFIC')
        self.assertFalse(self.lease.path.exists())

    def test_shutdown_failure_preserves_directory(self):
        path = self.lease.path
        with self.assertRaisesRegex(lifecycle.ReplayLifecycleError,
                                    'REPLAY_LIFECYCLE_UNVERIFIED'):
            self.call(error=wiring.ReplayWiringError(
                'REPLAY_STOP_OR_ACCOUNTING_UNCONFIRMED'))
        self.assertTrue(path.exists())
        self.assertTrue(self.lease.closed)

    def test_ledger_commit_failure_preserves_directory(self):
        path = self.lease.path
        with self.assertRaises(lifecycle.ReplayLifecycleError):
            self.call(error=wiring.ReplayWiringError(
                'REPLAY_LEDGER_RECONCILIATION_FAILED'))
        self.assertTrue(path.exists())

    def test_other_errors_preserve_file_and_hide_private_exception(self):
        path = self.lease.path
        private = path / 'unsettled'
        private.write_bytes(b'private evidence')
        with self.assertRaises(lifecycle.ReplayLifecycleError) as caught:
            self.call(error=RuntimeError('/private/cloud/token'))
        self.assertEqual(str(caught.exception),
                         'REPLAY_LIFECYCLE_UNVERIFIED')
        self.assertTrue(private.exists())

    def test_receipt_directory_replacement_refuses_cleanup(self):
        path = self.lease.path
        old = path.with_name(self.lease.name + '-moved')
        def move():
            path.rename(old)
            path.mkdir(mode=0o700)
            return settled()
        with (patch.object(lifecycle.os, 'geteuid', return_value=0),
              patch.object(lifecycle.wiring, 'run_wired',
                           side_effect=lambda **_k: move())):
            with self.assertRaisesRegex(
                    lifecycle.ReplayLifecycleError,
                    'REPLAY_CHANNEL_CLEANUP_UNVERIFIED'):
                lifecycle.run_lifecycle(
                    pid=1234, allocate_channel=lambda: self.lease,
                    ledger_factory=lambda: object(),
                    execute_argv=lambda *_a: None,
                    guard_setup=lambda: None,
                    idle_window=lambda: True,
                    monotonic=lambda: 0,
                    sleep=lambda _: None)
        self.assertTrue(path.exists())
        self.assertTrue(old.exists())

    def test_unexpected_child_blocks_deletion_and_preserves_evidence(self):
        private = self.lease.path / 'other-private-file'
        private.write_bytes(b'preserve me')
        with self.assertRaisesRegex(lifecycle.ReplayLifecycleError,
                                    'REPLAY_CHANNEL_CLEANUP_UNVERIFIED'):
            self.call()
        self.assertEqual(private.read_bytes(), b'preserve me')
        self.assertTrue(self.lease.path.exists())

    def test_invalid_lease_refuses_before_wiring(self):
        self.lease.close()
        with self.assertRaisesRegex(lifecycle.ReplayLifecycleError,
                                    'REPLAY_CHANNEL_LEASE_UNTRUSTED'):
            self.call()

    def test_wrong_allocation_type_refused_and_wiring_not_called(self):
        events = []
        def wrong():
            events.append('invalid')
            return object()
        with self.assertRaisesRegex(lifecycle.ReplayLifecycleError,
                                    'REPLAY_CHANNEL_LEASE_UNTRUSTED'):
            self.call(allocation=wrong)
        self.assertEqual(events, ['invalid'])

    def test_invalid_status_leaves_channel(self):
        bad = settled()
        bad['status'] = 'PRIVATE_UNKNOWN_STATUS'
        with self.assertRaisesRegex(lifecycle.ReplayLifecycleError,
                                    'REPLAY_SETTLEMENT_UNVERIFIED'):
            self.call(bad)
        self.assertTrue(self.lease.path.exists())

    def test_unverified_count_or_authorization_refuses_cleanup(self):
        for change in (
            {'checkpoint_writes': 1},
            {'checkpoint_writes': True},
            {'production_commit_authorized': True},
            {'measured_ip_bytes': -1},
            {'measured_ip_bytes': None},
            {'measured_ip_bytes': True},
            {'untrusted': 'private'},
        ):
            lease = self.lease
            sample = settled()
            sample.update(change)
            with self.subTest(change=change), self.assertRaisesRegex(
                    lifecycle.ReplayLifecycleError,
                    'REPLAY_SETTLEMENT_UNVERIFIED'):
                self.call(sample, lease=lease)
            self.assertTrue(lease.path.exists())

    def test_bad_success_outcome_refuses_cleanup(self):
        bad = settled()
        bad['outcome'] = 'REPLAY_EQUIVALENCE'
        with self.assertRaises(lifecycle.ReplayLifecycleError):
            self.call(bad)
        self.assertTrue(self.lease.path.exists())

    def test_bad_failure_code_refuses_cleanup(self):
        bad = settled('REPLAY_WORKER_FAILED')
        bad['code'] = '/private/path'
        with self.assertRaises(lifecycle.ReplayLifecycleError):
            self.call(bad)
        self.assertTrue(self.lease.path.exists())

    def test_deferred_with_measured_bytes_is_not_safe(self):
        bad = settled('REPLAY_DEFERRED_NO_TRAFFIC')
        bad['measured_ip_bytes'] = 1
        with self.assertRaises(lifecycle.ReplayLifecycleError):
            self.call(bad)
        self.assertTrue(self.lease.path.exists())

    def test_root_only_entry_refuses_before_any_allocation(self):
        events = []
        with (patch.object(lifecycle.os, 'geteuid', return_value=self.lease.uid),
              self.assertRaisesRegex(lifecycle.ReplayLifecycleError,
                                     'REPLAY_ROOT_SERVICE_REQUIRED')):
            lifecycle.run_lifecycle(
                pid=1234,
                allocate_channel=lambda: events.append('allocate'),
                ledger_factory=lambda: object(),
                execute_argv=lambda *_a: None,
                guard_setup=lambda: None,
                idle_window=lambda: True,
                monotonic=lambda: 0,
                sleep=lambda _: None)
        self.assertEqual(events, [])

    def test_missing_dependency_refuses_before_channel_allocation(self):
        events = []
        with (patch.object(lifecycle.os, 'geteuid', return_value=0),
              self.assertRaisesRegex(lifecycle.ReplayLifecycleError,
                                     'REPLAY_TRUSTED_DEPENDENCY_MISSING')):
            lifecycle.run_lifecycle(
                pid=1234,
                allocate_channel=lambda: events.append('allocate'),
                ledger_factory=None,
                execute_argv=lambda *_a: None,
                guard_setup=lambda: None,
                idle_window=lambda: True,
                monotonic=lambda: 0,
                sleep=lambda _: None)
        self.assertEqual(events, [])

    def test_no_default_live_execution_cli_or_services(self):
        text = Path(lifecycle.__file__).read_text(encoding='utf-8')
        self.assertNotIn('subprocess.run(', text)
        self.assertNotIn('import subprocess', text)
        self.assertNotIn("if __name__ == '__main__'", text)


if __name__ == '__main__':
    unittest.main()

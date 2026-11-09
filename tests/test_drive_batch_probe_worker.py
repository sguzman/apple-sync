"""No cloud calls: gate the read-only worker and archive lock locally."""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_probe_worker as worker


def summary():
    return {'folders_matched': 2, 'entries_matched': 1,
            'files_matched': 1, 'directories_matched': 0,
            'equivalent': True, 'checkpoint_writes': 0,
            'checkpoints_advanced': 0,
            'core_fields_verified': True, 'timestamps_verified': True}


class ProbeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.lock = self.root / '.apple-sync.lock'
        self.lock.touch()
        self.checkpoint = self.state / worker.inventory.PROGRESS_NAME
        self.checkpoint.write_bytes(b'checkpoint is sacred')
        self.binary = self.root / 'patched-rclone'
        self.binary.write_bytes(b'fake executable')
        self.binary.chmod(0o700)

    def fake_daemon(self):
        class Reader:
            socket = Path('/private/fake.sock')
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
        return Reader()

    def probe(self):
        with (patch.object(worker.archive, 'ROOT', self.root),
              patch.object(worker.inventory, 'load_progress',
                           return_value={'pending': ['test']}),
              patch.object(worker.build_identity, 'verify_binary_identity',
                           return_value='rclone v1.75.2'),
              patch.object(worker.diagnosis, 'backup_busy',
                           return_value=False),
              patch.object(worker, 'RcloneRCReader',
                           return_value=self.fake_daemon()),
              patch.object(worker.comparison, 'compare_readonly',
                           return_value=summary()) as compare,
              patch.object(worker.inventory, 'save_progress') as save):
            result = worker.probe_once(binary=self.binary,
                                       state_root=self.state)
        save.assert_not_called()
        self.assertEqual(compare.call_count, 1)
        self.assertTrue(compare.call_args.kwargs['require_core_fields'])
        self.assertEqual(result, summary())
        return result

    def test_read_only_worker_retains_original_checkpoint_bytes(self):
        original = self.checkpoint.read_bytes()
        self.probe()
        self.assertEqual(self.checkpoint.read_bytes(), original)

    def test_probe_refuses_second_archive_lock_holder(self):
        with (self.lock.open('r+') as lock,
              patch.object(worker.archive, 'ROOT', self.root),
              patch.object(worker.diagnosis, 'backup_busy',
                           return_value=False),
              patch.object(worker.build_identity, 'verify_binary_identity',
                           return_value='rclone v1.75.2'),
              patch.object(worker.inventory, 'load_progress') as progress):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(worker.WorkerError, 'OTHER_APPLE_ARCHIVE_ACTIVE'):
                worker.probe_once(binary=self.binary, state_root=self.state)
            progress.assert_not_called()

    def test_busy_backup_never_reads_checkpoint_or_calls_rclone(self):
        with (patch.object(worker.diagnosis, 'backup_busy',
                           return_value=True),
              patch.object(worker.build_identity, 'verify_binary_identity',
                           return_value='rclone v1.75.2'),
              patch.object(worker.inventory, 'load_progress') as progress,
              patch.object(worker, 'RcloneRCReader') as daemon):
            with self.assertRaisesRegex(worker.WorkerError, 'BACKUP_ACTIVE'):
                worker.probe_once(binary=self.binary, state_root=self.state)
        progress.assert_not_called()
        daemon.assert_not_called()

    def test_checkpoint_changed_during_probe_fails(self):
        def mutate(*_args, **_kwargs):
            self.checkpoint.write_bytes(b'changed')
            return summary()
        with (patch.object(worker.archive, 'ROOT', self.root),
              patch.object(worker.inventory, 'load_progress', return_value={'pending': ['test']}),
              patch.object(worker.diagnosis, 'backup_busy', return_value=False),
              patch.object(worker.build_identity, 'verify_binary_identity',
                           return_value='rclone v1.75.2'),
              patch.object(worker, 'RcloneRCReader', return_value=self.fake_daemon()),
              patch.object(worker.comparison, 'compare_readonly', side_effect=mutate),
              self.assertRaisesRegex(worker.WorkerError, 'INVENTORY_CHECKPOINT_CHANGED')):
            worker.probe_once(binary=self.binary, state_root=self.state)

    def test_core_field_mismatch_uses_sanitized_status_code(self):
        def fail(*_args, **_kwargs):
            raise worker.comparison.BatchProbeError('PROBE_CORE_FIELD_MISMATCH')
        with (patch.object(worker.archive, 'ROOT', self.root),
              patch.object(worker.inventory, 'load_progress', return_value={'pending': ['test']}),
              patch.object(worker.diagnosis, 'backup_busy', return_value=False),
              patch.object(worker.build_identity, 'verify_binary_identity',
                           return_value='rclone v1.75.2'),
              patch.object(worker, 'RcloneRCReader', return_value=self.fake_daemon()),
              patch.object(worker.comparison, 'compare_readonly', side_effect=fail)):
            with self.assertRaisesRegex(worker.WorkerError,
                                        'PROBE_CORE_FIELD_MISMATCH') as caught:
                worker.probe_once(binary=self.binary, state_root=self.state)
        self.assertEqual(str(caught.exception), 'PROBE_CORE_FIELD_MISMATCH')

    def test_untrusted_binary_rejected(self):
        self.binary.chmod(0o722)
        with self.assertRaisesRegex(worker.WorkerError, 'UNTRUSTED'):
            worker.validate_binary(self.binary)

    def test_wrong_binary_identity_refused_before_rclone(self):
        with (patch.object(worker.build_identity, 'verify_binary_identity',
                           side_effect=worker.build_identity.BuildError('WRONG_TAG')),
              patch.object(worker, 'RcloneRCReader') as daemon,
              self.assertRaisesRegex(worker.WorkerError, 'IDENTITY_MISMATCH')):
            worker.probe_once(binary=self.binary, state_root=self.state)
        daemon.assert_not_called()

    def test_missing_or_symlinked_binary_rejected(self):
        symlink = self.root / 'alias'
        symlink.symlink_to(self.binary)
        for candidate in (symlink, self.root / 'absent'):
            with self.subTest(candidate=candidate), \
                    self.assertRaisesRegex(worker.WorkerError, 'MISSING_OR_UNSAFE'):
                worker.validate_binary(candidate)

    def test_preview_never_invokes_probe(self):
        with (patch.object(sys, 'argv', ['worker']),
              patch.object(worker, 'probe_once') as probe,
              contextlib.redirect_stdout(io.StringIO()) as out):
            self.assertEqual(worker.main(), 0)
        probe.assert_not_called()
        self.assertIn('no Apple network requests', out.getvalue())

    def test_private_receipt_writes_only_aggregate_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            directory.chmod(0o700)
            worker.publish_receipt(summary(), directory=directory)
            path = directory / worker.RECEIPT_NAME
            result = json.loads(path.read_text())
            self.assertEqual(result['version'], 2)
            self.assertTrue(result['core_fields_verified'])
            self.assertTrue(result['timestamps_verified'])
            self.assertTrue(result['equivalent'])
            self.assertEqual(result['folders_matched'], 2)
            self.assertEqual(result['checkpoint_writes'], 0)
            self.assertEqual(result['checkpoints_advanced'], 0)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('Path', result)
            self.assertNotIn('ID', result)
            with self.assertRaises(FileExistsError):
                worker.publish_receipt(summary(), directory=directory)

    def test_identity_only_success_cannot_write_parity_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            directory.chmod(0o700)
            for absent_flag in ('core_fields_verified', 'timestamps_verified'):
                weak = summary()
                weak[absent_flag] = False
                with self.subTest(flag=absent_flag), self.assertRaisesRegex(
                        worker.WorkerError, 'INVALID_PROBE_COMPARISON_RESULT'):
                    worker.publish_receipt(weak, directory=directory)
            self.assertFalse((directory / worker.RECEIPT_NAME).exists())

    def test_invalid_receipt_result_does_not_write(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            directory.chmod(0o700)
            malformed = summary()
            malformed['checkpoint_writes'] = 1
            with self.assertRaisesRegex(worker.WorkerError,
                                        'INVALID_PROBE_COMPARISON_RESULT'):
                worker.publish_receipt(malformed, directory=directory)
            self.assertFalse((directory / worker.RECEIPT_NAME).exists())

    def test_metered_worker_refuses_success_if_receipt_channel_missing(self):
        with (patch.object(sys, 'argv', ['worker', '--execute']),
              patch.object(worker.os, 'geteuid', return_value=1000),
              patch.dict(os.environ, {
                  'INVOCATION_ID': 'test', 'APPLE_SYNC_BATCH_METERED': '1'
              }, clear=True),
              patch.object(worker, 'probe_once', return_value=summary()),
              contextlib.redirect_stderr(io.StringIO()) as err):
            self.assertEqual(worker.main(), 2)
        self.assertIn('PROBE_RESULT_CHANNEL_MISSING', err.getvalue())

    def test_execute_without_systemd_gate_refused(self):
        with (patch.object(sys, 'argv', ['worker', '--execute']),
              patch.dict(os.environ, {'APPLE_SYNC_BATCH_METERED': '1'},
                         clear=True),
              patch.object(worker, 'probe_once') as probe,
              contextlib.redirect_stderr(io.StringIO()) as err):
            self.assertEqual(worker.main(), 2)
        probe.assert_not_called()
        self.assertIn('METERED_SERVICE_REQUIRED', err.getvalue())


if __name__ == '__main__':
    unittest.main()

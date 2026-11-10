"""Offline-only tests of dormant replay worker locking and receipt contracts."""
from __future__ import annotations

from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_worker as worker


def observation():
    return {
        'version': 1,
        'outcome': 'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED',
        'directory_children_checked': 2,
        'directory_children_stable': 1,
        'directory_children_batch_only_difference': 1,
        'directory_children_single_read_difference': 0,
        'checkpoint_writes': 0, 'checkpoints_advanced': 0,
        'production_commit_authorized': False,
        'replay_requests_completed': 3, 'replay_folders_selected': 2,
        'measured_ip_bytes': None, 'ip_accounting_verified': False,
    }


class ReplayWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.lock = self.root / '.apple-sync.lock'
        self.lock.touch()
        self.checkpoint = self.state / worker.inventory.PROGRESS_NAME
        self.checkpoint.write_bytes(b'sacred checkpoint bytes')
        self.binary = self.root / 'patched-rclone'
        self.binary.write_bytes(b'test binary')
        self.binary.chmod(0o700)
        self.channel = self.root / 'channel'
        self.channel.mkdir(mode=0o700)

    def fake_reader(self):
        class Reader:
            socket = Path('/fake/private/rc.sock')
            def __enter__(self):
                return self
            def __exit__(self, *_unused):
                return False
        return Reader()

    def probe(self, *, run=None):
        if run is None:
            run = observation()
        with (patch.object(worker.archive, 'ROOT', self.root),
              patch.object(worker.original_worker, 'validate_binary'),
              patch.object(worker.diagnosis, 'backup_busy', return_value=False),
              patch.object(worker.inventory, 'load_progress',
                           return_value={'pending': ['private'], 'entries': []}),
              patch.object(worker, 'RcloneRCReader',
                           return_value=self.fake_reader()),
              patch.object(worker.protocol, 'run_bracketed',
                           return_value=run) as protocol,
              patch.object(worker.inventory, 'save_progress') as save):
            result = worker.replay_once(
                binary=self.binary, state_root=self.state)
        self.assertEqual(result, run)
        self.assertEqual(protocol.call_count, 1)
        self.assertTrue(callable(protocol.call_args.kwargs['backup_busy']))
        self.assertEqual(self.checkpoint.read_bytes(), b'sacred checkpoint bytes')
        save.assert_not_called()

    def test_worker_uses_archive_lock_and_preserves_checkpoint(self):
        self.probe()

    def test_rejects_active_backup_before_rclone(self):
        with (patch.object(worker.original_worker, 'validate_binary'),
              patch.object(worker.diagnosis, 'backup_busy', return_value=True),
              patch.object(worker, 'RcloneRCReader') as reader):
            with self.assertRaisesRegex(
                    worker.ReplayWorkerError,
                    'REPLAY_BACKUP_ACTIVE_OR_UNCERTAIN'):
                worker.replay_once(binary=self.binary, state_root=self.state)
        reader.assert_not_called()

    def test_rejects_unverified_patched_binary(self):
        with (patch.object(worker.original_worker, 'validate_binary',
                           side_effect=worker.original_worker.WorkerError(
                               'PRIVATE_PATH_SHOULD_NOT_LEAK')),
              patch.object(worker, 'RcloneRCReader') as reader,
              self.assertRaisesRegex(
                  worker.ReplayWorkerError,
                  'REPLAY_EXPERIMENTAL_BINARY_UNVERIFIED') as error):
            worker.replay_once(binary=self.binary, state_root=self.state)
        reader.assert_not_called()
        self.assertNotIn('PRIVATE_PATH', str(error.exception))

    def test_rejects_another_archive_lock_holder(self):
        with (self.lock.open('r+') as handle,
              patch.object(worker.archive, 'ROOT', self.root),
              patch.object(worker.original_worker, 'validate_binary'),
              patch.object(worker.diagnosis, 'backup_busy', return_value=False),
              patch.object(worker, 'RcloneRCReader') as reader):
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(worker.ReplayWorkerError,
                                        'REPLAY_ARCHIVE_LOCK_BUSY'):
                worker.replay_once(binary=self.binary, state_root=self.state)
        reader.assert_not_called()

    def test_checkpoint_hash_change_fails_closed(self):
        def corrupt(*_args, **_kwargs):
            self.checkpoint.write_bytes(b'changed')
            return observation()
        with (patch.object(worker.archive, 'ROOT', self.root),
              patch.object(worker.original_worker, 'validate_binary'),
              patch.object(worker.diagnosis, 'backup_busy', return_value=False),
              patch.object(worker.inventory, 'load_progress',
                           return_value={'pending': ['private']}),
              patch.object(worker, 'RcloneRCReader',
                           return_value=self.fake_reader()),
              patch.object(worker.protocol, 'run_bracketed',
                           side_effect=corrupt),
              self.assertRaisesRegex(worker.ReplayWorkerError,
                                     'REPLAY_CHECKPOINT_CHANGED')):
            worker.replay_once(binary=self.binary, state_root=self.state)

    def test_missing_checkpoint_stops_without_reading(self):
        self.checkpoint.unlink()
        with (patch.object(worker.archive, 'ROOT', self.root),
              patch.object(worker.original_worker, 'validate_binary'),
              patch.object(worker.diagnosis, 'backup_busy', return_value=False),
              patch.object(worker, 'RcloneRCReader') as reader,
              self.assertRaisesRegex(worker.ReplayWorkerError,
                                     'REPLAY_CHECKPOINT_MISSING')):
            worker.replay_once(binary=self.binary, state_root=self.state)
        reader.assert_not_called()

    def test_symlink_checkpoint_stops_before_reading(self):
        original = self.root / 'another'
        original.write_bytes(b'secret')
        self.checkpoint.unlink()
        self.checkpoint.symlink_to(original)
        with (patch.object(worker.archive, 'ROOT', self.root),
              patch.object(worker.original_worker, 'validate_binary'),
              patch.object(worker.diagnosis, 'backup_busy', return_value=False),
              patch.object(worker, 'RcloneRCReader') as reader,
              self.assertRaisesRegex(worker.ReplayWorkerError,
                                     'REPLAY_CHECKPOINT_MISSING')):
            worker.replay_once(binary=self.binary, state_root=self.state)
        reader.assert_not_called()

    def test_protocol_deferred_is_fixed_safe_failure(self):
        with (patch.object(worker.archive, 'ROOT', self.root),
              patch.object(worker.original_worker, 'validate_binary'),
              patch.object(worker.diagnosis, 'backup_busy', return_value=False),
              patch.object(worker.inventory, 'load_progress',
                           return_value={'pending': ['private']}),
              patch.object(worker, 'RcloneRCReader',
                           return_value=self.fake_reader()),
              patch.object(worker.protocol, 'run_bracketed',
                           side_effect=worker.protocol.ReplayProtocolDeferred(
                               'REPLAY_BACKUP_ACTIVE')),
              self.assertRaisesRegex(worker.ReplayWorkerError,
                                     'REPLAY_BACKUP_INTERRUPTED')):
            worker.replay_once(binary=self.binary, state_root=self.state)

    def test_protocol_identity_failure_preserved_without_cloud_values(self):
        with (patch.object(worker.archive, 'ROOT', self.root),
              patch.object(worker.original_worker, 'validate_binary'),
              patch.object(worker.diagnosis, 'backup_busy', return_value=False),
              patch.object(worker.inventory, 'load_progress',
                           return_value={'pending': ['private']}),
              patch.object(worker, 'RcloneRCReader',
                           return_value=self.fake_reader()),
              patch.object(worker.protocol, 'run_bracketed',
                           side_effect=worker.protocol.ReplayProtocolError(
                               'REPLAY_CHILD_IDENTITY_CHANGED')),
              self.assertRaisesRegex(worker.ReplayWorkerError,
                                     'REPLAY_CHILD_IDENTITY_CHANGED')):
            worker.replay_once(binary=self.binary, state_root=self.state)

    def test_observation_receipt_is_0600_and_not_an_equivalence_proof(self):
        worker.publish_observation(observation(), directory=self.channel)
        path = self.channel / worker.RESULT_NAME
        result = json.loads(path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result['version'], 1)
        self.assertEqual(result['kind'], 'observation')
        self.assertFalse(result['production_commit_authorized'])
        self.assertEqual(result['checkpoint_writes'], 0)
        self.assertEqual(result['directory_children_checked'], 2)
        for prohibited in ('equivalent', 'timestamps_verified',
                           'core_fields_verified', 'measured_ip_bytes',
                           'Private', 'DateModified', 'ID', 'Path'):
            self.assertNotIn(prohibited, str(result))

    def test_failure_receipt_has_code_but_no_private_data(self):
        worker.publish_failure('REPLAY_NON_TIME_METADATA_CHANGED',
                               directory=self.channel)
        result = json.loads((self.channel / worker.RESULT_NAME).read_text())
        self.assertEqual(result, {
            'version': 1, 'kind': 'failure',
            'code': 'REPLAY_NON_TIME_METADATA_CHANGED',
            'checkpoint_writes': 0,
            'production_commit_authorized': False,
        })

    def test_unknown_failure_code_is_sanitized(self):
        worker.publish_failure('Private folder token', directory=self.channel)
        result = json.loads((self.channel / worker.RESULT_NAME).read_text())
        self.assertEqual(result['code'], 'REPLAY_LOCAL_FAILURE')
        self.assertNotIn('Private', str(result))

    def test_unhashable_failure_code_is_sanitized(self):
        worker.publish_failure({'raw': 'private'}, directory=self.channel)
        result = json.loads((self.channel / worker.RESULT_NAME).read_text())
        self.assertEqual(result['code'], 'REPLAY_LOCAL_FAILURE')

    def test_bad_observation_is_rejected_before_writing(self):
        invalid = (
            {'outcome': 'OTHER'},
            {'outcome': ['unhashable']},
            {'outcome': 'REPLAY_ALL_DIRECTORY_TIMESTAMPS_IDENTICAL'},
            {'directory_children_checked': 1},
            {'directory_children_stable': -1},
            {'directory_children_batch_only_difference': 2},
            {'checkpoint_writes': 1},
            {'checkpoints_advanced': 1},
            {'production_commit_authorized': True},
            {'measured_ip_bytes': 10},
            {'ip_accounting_verified': True},
            {'replay_requests_completed': True},
            {'extra_private_data': 'secret'},
        )
        for mutation in invalid:
            sample = deepcopy(observation())
            sample.update(mutation)
            with self.subTest(mutation=tuple(mutation)), self.assertRaisesRegex(
                    worker.ReplayWorkerError, 'REPLAY_RESULT_INVALID'):
                worker.publish_observation(sample, directory=self.channel)
            self.assertFalse((self.channel / worker.RESULT_NAME).exists())

    def test_symlink_or_world_readable_channel_is_rejected(self):
        alias = self.root / 'symlink'
        alias.symlink_to(self.channel)
        readable = self.root / 'readable'
        readable.mkdir(mode=0o755)
        readable.chmod(0o755)
        for target in (alias, readable):
            with self.subTest(path=target.name), self.assertRaisesRegex(
                    worker.ReplayWorkerError, 'REPLAY_RESULT_CHANNEL_UNTRUSTED'):
                worker.publish_observation(observation(), directory=target)
            self.assertFalse((target / worker.RESULT_NAME).exists())

    def test_receipt_is_exclusive_and_will_not_overwrite(self):
        worker.publish_observation(observation(), directory=self.channel)
        with self.assertRaises(FileExistsError):
            worker.publish_failure('REPLAY_LOCAL_FAILURE',
                                   directory=self.channel)

    def test_wrong_total_counts_cannot_claim_observation(self):
        changed = observation()
        changed['directory_children_single_read_difference'] = 1
        with self.assertRaisesRegex(
                worker.ReplayWorkerError, 'REPLAY_RESULT_INVALID'):
            worker.publish_observation(changed, directory=self.channel)

    def test_running_file_directly_only_prints_dormant_message(self):
        # Direct script invocation must never call replay_once or rc.
        path = Path(worker.__file__)
        child = subprocess.run([sys.executable, str(path)],
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(child.returncode, 0)
        self.assertIn('REPLAY_WORKER_DORMANT', child.stdout)
        self.assertEqual(child.stderr, '')


if __name__ == '__main__':
    unittest.main()

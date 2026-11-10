"""Offline-only root-side replay receipt tests. No Apple traffic or systemd."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import pwd
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import metered_drive_batch_replay as controller
import drive_batch_replay_worker as worker


def observation():
    return {
        'version': 1, 'kind': 'observation',
        'outcome': 'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED',
        'directory_children_checked': 2,
        'directory_children_stable': 1,
        'directory_children_batch_only_difference': 1,
        'directory_children_single_read_difference': 0,
        'checkpoint_writes': 0, 'checkpoints_advanced': 0,
        'production_commit_authorized': False,
        'replay_requests_completed': 3, 'replay_folders_selected': 2,
    }


class RootReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'private'
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / controller.RESULT_NAME
        patcher = patch.object(controller.pwd, 'getpwnam',
                               return_value=SimpleNamespace(pw_uid=os.getuid()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, payload, *, mode=0o600):
        self.path.write_text(json.dumps(payload), encoding='ascii')
        self.path.chmod(mode)

    def test_roundtrip_worker_observation_is_not_equivalence(self):
        source = {
            'version': 1, 'outcome': 'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED',
            'directory_children_checked': 2,
            'directory_children_stable': 1,
            'directory_children_batch_only_difference': 1,
            'directory_children_single_read_difference': 0,
            'checkpoint_writes': 0, 'checkpoints_advanced': 0,
            'production_commit_authorized': False,
            'replay_requests_completed': 3, 'replay_folders_selected': 2,
            'measured_ip_bytes': None, 'ip_accounting_verified': False,
        }
        worker.publish_observation(source, directory=self.directory)
        result = controller.read_observation(self.directory)
        self.assertEqual(result, observation())
        self.assertNotIn('equivalent', result)
        self.assertFalse(result['production_commit_authorized'])

    def test_all_four_observation_categories_are_constrained_by_counts(self):
        outcomes = (
            ('REPLAY_ALL_DIRECTORY_TIMESTAMPS_IDENTICAL', 2, 0, 0),
            ('REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED', 1, 1, 0),
            ('REPLAY_SINGLE_READS_DIFFER_OBSERVED', 1, 0, 1),
            ('REPLAY_MIXED_DIFFERENCES_OBSERVED', 0, 1, 1),
        )
        for outcome, stable, batch, single in outcomes:
            receipt = observation()
            receipt.update(outcome=outcome,
                           directory_children_stable=stable,
                           directory_children_batch_only_difference=batch,
                           directory_children_single_read_difference=single)
            with self.subTest(outcome=outcome):
                self.write(receipt)
                self.assertEqual(
                    controller.read_observation(self.directory)['outcome'],
                    outcome)

    def test_worker_failure_receipt_roundtrip(self):
        worker.publish_failure('REPLAY_BACKUP_INTERRUPTED',
                               directory=self.directory)
        self.assertEqual(
            controller.read_failure(self.directory),
            'REPLAY_BACKUP_INTERRUPTED')
        with self.assertRaisesRegex(controller.ReplayMeterError,
                                    'REPLAY_RECEIPT_INVALID'):
            controller.read_observation(self.directory)

    def test_success_receipt_cannot_be_used_as_failure(self):
        self.write(observation())
        with self.assertRaisesRegex(controller.ReplayMeterError,
                                    'REPLAY_RECEIPT_INVALID'):
            controller.read_failure(self.directory)

    def test_old_equivalence_receipt_is_always_rejected(self):
        self.write({
            'version': 2, 'equivalent': True,
            'core_fields_verified': True, 'timestamps_verified': True,
            'folders_matched': 2, 'entries_matched': 1,
            'files_matched': 0, 'directories_matched': 1,
            'checkpoint_writes': 0, 'checkpoints_advanced': 0,
        })
        for method in (controller.read_observation, controller.read_failure):
            with self.subTest(method=method.__name__), self.assertRaisesRegex(
                    controller.ReplayMeterError, 'REPLAY_RECEIPT_INVALID'):
                method(self.directory)

    def test_private_fields_cannot_leak_through_observation(self):
        for key in ('Path', 'ID', 'ModTime', 'DateModified', 'equivalent',
                    'timestamps_verified', 'core_fields_verified'):
            sample = observation()
            sample[key] = 'private'
            self.write(sample)
            with self.subTest(key=key), self.assertRaisesRegex(
                    controller.ReplayMeterError, 'REPLAY_RECEIPT_INVALID') as error:
                controller.read_observation(self.directory)
            self.assertNotIn('private', str(error.exception))

    def test_invalid_counts_are_never_accepted(self):
        changes = (
            {'directory_children_checked': 0},
            {'directory_children_checked': True},
            {'directory_children_checked': 1},
            {'directory_children_stable': -1},
            {'directory_children_batch_only_difference': 100},
            {'directory_children_single_read_difference': True},
            {'outcome': 'REPLAY_ALL_DIRECTORY_TIMESTAMPS_IDENTICAL'},
            {'replay_requests_completed': 2},
            {'replay_requests_completed': True},
            {'replay_folders_selected': 1},
            {'checkpoint_writes': 1},
            {'checkpoints_advanced': 1},
            {'production_commit_authorized': True},
        )
        for change in changes:
            bad = deepcopy(observation())
            bad.update(change)
            self.write(bad)
            with self.subTest(change=tuple(change)), self.assertRaisesRegex(
                    controller.ReplayMeterError, 'REPLAY_RECEIPT_INVALID'):
                controller.read_observation(self.directory)

    def test_missing_or_oversized_receipt_rejected(self):
        with self.assertRaisesRegex(controller.ReplayMeterError,
                                    'REPLAY_RECEIPT_UNAVAILABLE'):
            controller.read_observation(self.directory)
        self.path.write_bytes(b'X' * (controller.MAX_RESULT_BYTES + 1))
        self.path.chmod(0o600)
        with self.assertRaisesRegex(controller.ReplayMeterError,
                                    'REPLAY_RECEIPT_UNTRUSTED'):
            controller.read_observation(self.directory)

    def test_symlinked_receipt_cannot_be_followed(self):
        another = self.directory / 'other.json'
        another.write_text(json.dumps(observation()))
        another.chmod(0o600)
        self.path.symlink_to(another)
        with self.assertRaisesRegex(controller.ReplayMeterError,
                                    'REPLAY_RECEIPT_UNAVAILABLE'):
            controller.read_observation(self.directory)

    def test_symlinked_directory_cannot_be_followed(self):
        alias = Path(self.temp.name) / 'alias'
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaisesRegex(controller.ReplayMeterError,
                                    'REPLAY_RECEIPT_CHANNEL_UNTRUSTED'):
            controller.read_observation(alias)

    def test_world_accessible_directory_or_file_is_rejected(self):
        self.write(observation())
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(controller.ReplayMeterError,
                                    'REPLAY_RECEIPT_CHANNEL_UNTRUSTED'):
            controller.read_observation(self.directory)
        self.directory.chmod(0o700)
        self.path.chmod(0o644)
        with self.assertRaisesRegex(controller.ReplayMeterError,
                                    'REPLAY_RECEIPT_UNTRUSTED'):
            controller.read_observation(self.directory)

    def test_wrong_file_or_directory_owner_is_rejected(self):
        self.write(observation())
        with patch.object(controller.pwd, 'getpwnam',
                          return_value=SimpleNamespace(pw_uid=os.getuid() + 1)):
            with self.assertRaisesRegex(controller.ReplayMeterError,
                                        'REPLAY_RECEIPT_CHANNEL_UNTRUSTED'):
                controller.read_observation(self.directory)

    def test_duplicate_json_keys_are_rejected(self):
        self.path.write_bytes(b'{"version":1,"version":1}')
        self.path.chmod(0o600)
        with self.assertRaisesRegex(controller.ReplayMeterError,
                                    'REPLAY_RECEIPT_INVALID'):
            controller.read_observation(self.directory)

    def test_non_object_json_is_rejected(self):
        self.path.write_bytes(b'[1, 2, 3]')
        self.path.chmod(0o600)
        with self.assertRaisesRegex(controller.ReplayMeterError,
                                    'REPLAY_RECEIPT_INVALID'):
            controller.read_observation(self.directory)

    def test_invalid_failure_codes_are_rejected(self):
        sample = {
            'version': 1, 'kind': 'failure',
            'code': 'REPLAY_BACKUP_INTERRUPTED',
            'checkpoint_writes': 0, 'production_commit_authorized': False,
        }
        for update in ({'code': 'Private/Token'}, {'code': []},
                       {'checkpoint_writes': True},
                       {'production_commit_authorized': True},
                       {'kind': 'observation'},
                       {'version': 2}):
            invalid = deepcopy(sample)
            invalid.update(update)
            self.write(invalid)
            with self.subTest(update=tuple(update)), self.assertRaisesRegex(
                    controller.ReplayMeterError, 'REPLAY_RECEIPT_INVALID'):
                controller.read_failure(self.directory)

    def test_worker_and_root_allowlists_agree(self):
        self.assertEqual(controller.OBSERVATION_OUTCOMES,
                         worker.REPLAY_OUTCOMES)
        self.assertEqual(controller.FAILURE_CODES, worker.REPLAY_FAILURES)

    def test_hardlinked_receipt_is_refused(self):
        original = self.directory / 'original.json'
        original.write_text(json.dumps(observation()))
        original.chmod(0o600)
        os.link(original, self.path)
        with self.assertRaisesRegex(controller.ReplayMeterError,
                                    'REPLAY_RECEIPT_UNTRUSTED'):
            controller.read_observation(self.directory)

    def test_dormant_controller_has_no_live_cli(self):
        child = subprocess.run(
            [sys.executable, str(Path(controller.__file__))],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(child.returncode, 0)
        self.assertIn('REPLAY_METER_DORMANT', child.stdout)
        self.assertEqual(child.stderr, '')


if __name__ == '__main__':
    unittest.main()

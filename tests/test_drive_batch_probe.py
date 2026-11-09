"""Offline-only verification for the two-folder batch comparison orchestration."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_probe as probe

A = 'FOLDER::com.apple.CloudDocs::first'
B = 'FOLDER::com.apple.CloudDocs::second'
F1 = 'FILE::com.apple.CloudDocs::file1'
F2 = 'FILE::com.apple.CloudDocs::file2'


def checkpoint():
    return {
        'version': 1, 'source': 'icloud:',
        'pending': ['Private/one', 'Private/two'],
        'finished': [''],
        'entries': [
            {'Path': 'Private/one', 'IsDir': True, 'ID': A + '#tag-one'},
            {'Path': 'Private/two', 'IsDir': True, 'ID': B + '#tag-two'},
        ],
    }


def native(folder, file_id, name):
    return {'drivewsid': folder, 'type': 'FOLDER', 'status': 'OK',
            'directChildrenCount': 1,
            'items': [{'drivewsid': file_id, 'type': 'FILE',
                       'name': name, 'extension': 'txt', 'size': 7}]}


def references(folder):
    return {
        'Private/one': [{'ID': F1, 'Path': 'one.txt',
                         'IsDir': False, 'Size': 7}],
        'Private/two': [{'ID': F2, 'Path': 'two.txt',
                         'IsDir': False, 'Size': 7}],
    }[folder]


def replies(_batch):
    return [native(B, F2, 'two'), native(A, F1, 'one')]


class ReadOnlyComparisonTests(unittest.TestCase):
    def test_two_folders_match_and_checkpoint_object_unchanged(self):
        progress = checkpoint()
        original = deepcopy(progress)
        fetched = []
        listed = []
        result = probe.compare_readonly(
            progress, read_native=lambda batch: (fetched.append(batch), replies(batch))[1],
            read_legacy=lambda folder: (listed.append(folder), references(folder))[1],
            backup_busy=lambda: False)
        self.assertEqual(fetched, [[('Private/one', A), ('Private/two', B)]])
        self.assertEqual(listed, ['Private/one', 'Private/two'])
        self.assertEqual(result['folders_matched'], 2)
        self.assertEqual(result['entries_matched'], 2)
        self.assertEqual(result['checkpoint_writes'], 0)
        self.assertEqual(result['checkpoints_advanced'], 0)
        self.assertIsNone(result['measured_ip_bytes'])
        self.assertFalse(result['timestamps_verified'])
        self.assertEqual(progress, original)

    def test_limit_one_never_fetches_second_folder(self):
        seen = []
        result = probe.compare_readonly(
            checkpoint(), folder_limit=1,
            read_native=lambda batch: (seen.append(batch), [native(A, F1, 'one')])[1],
            read_legacy=references, backup_busy=lambda: False)
        self.assertEqual(seen, [[('Private/one', A)]])
        self.assertEqual(result['sampled_pending_folders'], 1)

    def test_invalid_limits_never_contact_callbacks(self):
        native_read = Mock()
        legacy_read = Mock()
        for limit in (0, 3, True, 1.5, None):
            with self.subTest(limit=limit), self.assertRaisesRegex(
                    probe.BatchProbeError, 'INVALID_PROBE_FOLDER_LIMIT'):
                probe.compare_readonly(
                    checkpoint(), folder_limit=limit, read_native=native_read,
                    read_legacy=legacy_read, backup_busy=lambda: False)
        native_read.assert_not_called()
        legacy_read.assert_not_called()

    def test_backup_busy_prevents_any_reads(self):
        read_native = Mock()
        with self.assertRaisesRegex(probe.BatchProbeDeferred,
                                    'PROBE_OTHER_BACKUP_ACTIVE'):
            probe.compare_readonly(checkpoint(), read_native=read_native,
                                   read_legacy=Mock(), backup_busy=lambda: True)
        read_native.assert_not_called()

    def test_becoming_busy_after_batch_stops_before_reference(self):
        checks = iter([False, True])
        reader = Mock()
        with self.assertRaises(probe.BatchProbeDeferred):
            probe.compare_readonly(checkpoint(), read_native=replies,
                                   read_legacy=reader,
                                   backup_busy=lambda: next(checks))
        reader.assert_not_called()

    def test_unknown_backup_status_blocks_without_reads(self):
        for busy in (lambda: None, lambda: 'inactive',
                     Mock(side_effect=RuntimeError('private status'))):
            native_read = Mock()
            with self.subTest(busy=busy), self.assertRaisesRegex(
                    probe.BatchProbeDeferred, 'PROBE_BACKUP_STATE_UNKNOWN'):
                probe.compare_readonly(checkpoint(), read_native=native_read,
                                       read_legacy=Mock(), backup_busy=busy)
            native_read.assert_not_called()

    def test_invalid_checkpoint_does_not_start_reads(self):
        progress = checkpoint()
        progress['entries'][0]['ID'] = None
        batch_read = Mock()
        with self.assertRaisesRegex(probe.BatchProbeError,
                                    'PROBE_INELIGIBLE_CHECKPOINT'):
            probe.compare_readonly(progress, read_native=batch_read,
                                   read_legacy=Mock(), backup_busy=lambda: False)
        batch_read.assert_not_called()

    def test_native_errors_are_sanitized(self):
        with self.assertRaisesRegex(probe.BatchProbeError,
                                    'PROBE_NATIVE_READ_FAILED') as raised:
            probe.compare_readonly(
                checkpoint(), read_native=Mock(side_effect=RuntimeError('private-secret')),
                read_legacy=references, backup_busy=lambda: False)
        self.assertNotIn('private-secret', str(raised.exception))

    def test_legacy_errors_are_sanitized(self):
        with self.assertRaisesRegex(probe.BatchProbeError,
                                    'PROBE_LEGACY_READ_FAILED') as raised:
            probe.compare_readonly(
                checkpoint(), read_native=replies,
                read_legacy=Mock(side_effect=RuntimeError('secret-folder')),
                backup_busy=lambda: False)
        self.assertNotIn('secret-folder', str(raised.exception))

    def test_mismatched_identity_fails_closed(self):
        def wrong(_batch):
            return [native(A, F2, 'one'), native(B, F2, 'two')]
        with self.assertRaisesRegex(probe.BatchProbeError,
                                    'PROBE_METADATA_MISMATCH'):
            probe.compare_readonly(
                checkpoint(), read_native=wrong,
                read_legacy=references, backup_busy=lambda: False)

    def test_mutating_callback_cannot_change_checkpoint(self):
        state = checkpoint()
        original = deepcopy(state)
        def changes(batch):
            batch[0] = ('modified-path', A)
            return replies(batch)
        with self.assertRaises(probe.BatchProbeError):
            probe.compare_readonly(state, read_native=changes,
                                   read_legacy=references, backup_busy=lambda: False)
        self.assertEqual(state, original)

    def test_backup_becomes_busy_during_legacy_read(self):
        calls = iter([False, False, False, True])
        with self.assertRaisesRegex(probe.BatchProbeDeferred,
                                    'PROBE_OTHER_BACKUP_ACTIVE'):
            probe.compare_readonly(
                checkpoint(), read_native=replies,
                read_legacy=references,
                backup_busy=lambda: next(calls))


if __name__ == '__main__':
    unittest.main()

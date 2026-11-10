"""No-cloud acceptance tests for a future root-metered three-read replay."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_protocol as protocol
import drive_inventory as inventory

A = 'FOLDER::zone::alpha'
B = 'FOLDER::zone::beta'
D = 'FOLDER::zone::child'
STAMP = '2026-11-10T10:00:00Z'
NEW = '2026-11-10T10:00:09Z'


def progress():
    return {'version': 1, 'source': 'icloud:',
            'pending': ['Private/A', 'Private/B'], 'finished': [''],
            'entries': [
                {'Path': 'Private/A', 'IsDir': True, 'ID': A + '#revision'},
                {'Path': 'Private/B', 'IsDir': True, 'ID': B + '#revision'},
            ]}


def reply(identifier, timestamp=STAMP):
    if identifier == B:
        return {'drivewsid': B, 'type': 'FOLDER', 'status': 'OK',
                'directChildrenCount': 0, 'items': []}
    child = {'drivewsid': D, 'name': 'child', 'extension': '',
             'type': 'FOLDER', 'assetQuota': 7, 'etag': 'rev-1',
             'dateModified': timestamp}
    return {'drivewsid': A, 'type': 'FOLDER', 'status': 'OK',
            'directChildrenCount': 1, 'items': [child]}


def native_replies(first=STAMP, batch=STAMP, last=STAMP):
    return [[reply(A, first)], [reply(B), reply(A, batch)],
            [reply(A, last)]]


def callbacks(responses):
    seen = []
    iterator = iter(responses)
    def reader(folder_batch):
        seen.append(deepcopy(folder_batch))
        return next(iterator)
    return reader, seen


class ReplayProtocolTests(unittest.TestCase):
    def test_uses_single_batch_single_in_correct_order(self):
        reader, calls = callbacks(native_replies(batch=NEW))
        before = progress()
        snapshot = deepcopy(before)
        result = protocol.run_bracketed(
            before, read_native=reader, backup_busy=lambda: False)
        self.assertEqual(calls, [[('Private/A', A)],
                                 [('Private/A', A), ('Private/B', B)],
                                 [('Private/A', A)]])
        self.assertEqual(result['outcome'],
                         'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED')
        self.assertEqual(result['replay_requests_completed'], 3)
        self.assertEqual(result['replay_folders_selected'], 2)
        self.assertEqual(before, snapshot)
        self.assertIsNone(result['measured_ip_bytes'])
        self.assertFalse(result['ip_accounting_verified'])
        self.assertFalse(result['production_commit_authorized'])
        self.assertEqual(result['checkpoint_writes'], 0)
        self.assertEqual(result['checkpoints_advanced'], 0)

    def test_all_same_does_not_authorize_checkpoint(self):
        reader, calls = callbacks(native_replies())
        result = protocol.run_bracketed(
            progress(), read_native=reader, backup_busy=lambda: False)
        self.assertEqual(len(calls), 3)
        self.assertEqual(result['outcome'],
                         'REPLAY_ALL_DIRECTORY_TIMESTAMPS_IDENTICAL')
        self.assertFalse(result['production_commit_authorized'])

    def test_single_change_is_not_conflated_with_batch_only_difference(self):
        reader, _ = callbacks(native_replies(last=NEW))
        result = protocol.run_bracketed(
            progress(), read_native=reader, backup_busy=lambda: False)
        self.assertEqual(result['outcome'],
                         'REPLAY_SINGLE_READS_DIFFER_OBSERVED')

    def test_two_folders_required_before_any_callback(self):
        for pending in ([], ['Private/A']):
            state = progress()
            state['pending'] = pending
            reader = Mock()
            with self.subTest(pending=len(pending)), self.assertRaises(
                    protocol.ReplayProtocolError):
                protocol.run_bracketed(
                    state, read_native=reader, backup_busy=lambda: False)
            reader.assert_not_called()

    def test_bad_checkpoint_fails_before_any_icloud_read(self):
        state = progress()
        state['entries'][0]['ID'] = 'private-invalid-id'
        reader = Mock()
        with self.assertRaisesRegex(
                protocol.ReplayProtocolError, 'REPLAY_INVALID_CHECKPOINT'):
            protocol.run_bracketed(
                state, read_native=reader, backup_busy=lambda: False)
        reader.assert_not_called()

    def test_backup_busy_blocks_before_first_read(self):
        reader = Mock()
        with self.assertRaisesRegex(
                protocol.ReplayProtocolDeferred, 'REPLAY_BACKUP_ACTIVE'):
            protocol.run_bracketed(
                progress(), read_native=reader, backup_busy=lambda: True)
        reader.assert_not_called()

    def test_backup_activation_after_first_read_blocks_second(self):
        reader, seen = callbacks(native_replies())
        checks = iter([False, True])
        with self.assertRaisesRegex(
                protocol.ReplayProtocolDeferred, 'REPLAY_BACKUP_ACTIVE'):
            protocol.run_bracketed(
                progress(), read_native=reader,
                backup_busy=lambda: next(checks))
        self.assertEqual(len(seen), 1)

    def test_backup_activation_before_third_read_blocks_it(self):
        reader, seen = callbacks(native_replies())
        checks = iter([False, False, False, False, True])
        with self.assertRaisesRegex(
                protocol.ReplayProtocolDeferred, 'REPLAY_BACKUP_ACTIVE'):
            protocol.run_bracketed(
                progress(), read_native=reader,
                backup_busy=lambda: next(checks))
        self.assertEqual(len(seen), 2)

    def test_backup_activation_after_all_reads_defers_result(self):
        reader, seen = callbacks(native_replies())
        checks = iter([False] * 6 + [True])
        with self.assertRaisesRegex(
                protocol.ReplayProtocolDeferred, 'REPLAY_BACKUP_ACTIVE'):
            protocol.run_bracketed(
                progress(), read_native=reader,
                backup_busy=lambda: next(checks))
        self.assertEqual(len(seen), 3)

    def test_unknown_status_or_exception_refuses_reads(self):
        for busy in (lambda: None, lambda: 'false',
                     Mock(side_effect=RuntimeError('private server token'))):
            reader = Mock()
            with self.subTest(busy=str(type(busy))), self.assertRaisesRegex(
                    protocol.ReplayProtocolDeferred,
                    'REPLAY_BACKUP_STATUS_UNKNOWN') as error:
                protocol.run_bracketed(
                    progress(), read_native=reader, backup_busy=busy)
            reader.assert_not_called()
            self.assertNotIn('private server token', str(error.exception))

    def test_read_failure_is_sanitized_and_stops(self):
        reader = Mock(side_effect=RuntimeError('private path /secret'))
        with self.assertRaisesRegex(
                protocol.ReplayProtocolError, 'REPLAY_NATIVE_READ_FAILED') as error:
            protocol.run_bracketed(
                progress(), read_native=reader, backup_busy=lambda: False)
        self.assertEqual(reader.call_count, 1)
        self.assertNotIn('private', str(error.exception))

    def test_malformed_response_does_not_count_as_success(self):
        malformed = native_replies()
        malformed[1][0]['directChildrenCount'] = 1
        reader, seen = callbacks(malformed)
        with self.assertRaisesRegex(
                protocol.ReplayProtocolError,
                'REPLAY_UNVERIFIED_NATIVE_RESPONSE'):
            protocol.run_bracketed(
                progress(), read_native=reader, backup_busy=lambda: False)
        self.assertEqual(len(seen), 2)

    def test_invalid_first_response_stops_before_batch(self):
        responses = native_replies()
        responses[0][0]['items'][0].pop('dateModified')
        reader, seen = callbacks(responses)
        with self.assertRaisesRegex(
                protocol.ReplayProtocolError, 'REPLAY_UNVERIFIED_NATIVE_RESPONSE'):
            protocol.run_bracketed(
                progress(), read_native=reader, backup_busy=lambda: False)
        self.assertEqual(len(seen), 1)

    def test_invalid_batch_response_stops_before_final_single(self):
        responses = native_replies()
        responses[1][1]['items'][0]['assetQuota'] = 'bad'
        reader, seen = callbacks(responses)
        with self.assertRaisesRegex(
                protocol.ReplayProtocolError, 'REPLAY_UNVERIFIED_NATIVE_RESPONSE'):
            protocol.run_bracketed(
                progress(), read_native=reader, backup_busy=lambda: False)
        self.assertEqual(len(seen), 2)

    def test_changed_directory_identity_is_rejected_without_details(self):
        replies = native_replies(batch=NEW)
        replies[1][1]['items'][0]['drivewsid'] = 'FOLDER::zone::different'
        reader, _ = callbacks(replies)
        with self.assertRaisesRegex(
                protocol.ReplayProtocolError, 'REPLAY_CHILD_IDENTITY_CHANGED') as error:
            protocol.run_bracketed(
                progress(), read_native=reader, backup_busy=lambda: False)
        self.assertNotIn('different', str(error.exception))

    def test_no_source_ids_names_or_timestamps_in_result(self):
        reader, _ = callbacks(native_replies(batch=NEW))
        result = protocol.run_bracketed(
            progress(), read_native=reader, backup_busy=lambda: False)
        for secret in ('Private/A', 'Private/B', A, B, D, STAMP, NEW):
            self.assertNotIn(secret, str(result))

    def test_read_callback_can_mutate_inputs_without_mutating_progress(self):
        checkpoint = progress()
        saved = deepcopy(checkpoint)
        replies = iter(native_replies())
        def mutating_reader(batch):
            batch[0] = ('CompletelyDifferent', A)
            return next(replies)
        # The legacy checkpoint cannot be affected even when a callback
        # modifies the private planning copy supplied to that callback.
        result = protocol.run_bracketed(
            checkpoint, read_native=mutating_reader, backup_busy=lambda: False)
        self.assertEqual(checkpoint, saved)
        self.assertEqual(result['replay_requests_completed'], 3)

    def test_offline_tests_guard_no_checkpoint_or_network_calls(self):
        reader, _ = callbacks(native_replies(batch=NEW))
        with (patch.object(inventory, 'save_progress',
                           side_effect=AssertionError('saved checkpoint')),
              patch.object(inventory.subprocess, 'run',
                           side_effect=AssertionError('launched network'))):
            result = protocol.run_bracketed(
                progress(), read_native=reader, backup_busy=lambda: False)
        self.assertEqual(result['checkpoint_writes'], 0)


if __name__ == '__main__':
    unittest.main()

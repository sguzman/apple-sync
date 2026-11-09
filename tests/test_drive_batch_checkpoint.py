"""Offline batched metadata-to-checkpoint planning, without Apple traffic."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_checkpoint as batch
import drive_inventory as inventory

TIME = '2026-11-09T11:50:00Z'


def directory(path, identifier, etag='previous'):
    return {'Path': path, 'IsDir': True, 'Size': 0,
            'ID': identifier + '#' + etag, 'ModTime': TIME}


def new_child(identifier, name, *, kind='FILE', extension='txt',
              modified=TIME, size=7, quota=0, etag='version'):
    return {'drivewsid': identifier, 'name': name,
            'extension': extension, 'type': kind, 'size': size,
            'assetQuota': quota, 'dateModified': modified, 'etag': etag}


def envelope(identifier, children):
    return {'drivewsid': identifier, 'type': 'FOLDER', 'status': 'OK',
            'directChildrenCount': len(children), 'items': children}


A, B, C = 'FOLDER::zone::a', 'APP_CONTAINER::zone::b', 'FOLDER::zone::c'


def checkpoint():
    return {'version': 1, 'source': 'icloud:',
            'pending': ['A', 'B'], 'finished': [''],
            'entries': [directory('A', A), directory('B', B)]}


def reply():
    return [
        envelope(A, [new_child('FILE::zone::f1', 'first'),
                     new_child(C, 'C', kind='FOLDER',
                               extension='', etag='latest', quota=500)]),
        envelope(B, [new_child('FILE::zone::f2', 'other')]),
    ]


class BatchCheckpointTests(unittest.TestCase):
    def test_conversion_creates_lsjson_compatible_fields(self):
        folder = new_child(C, 'data', kind='FOLDER', extension='',
                           quota=999, etag='revision')
        converted = batch.native_child_to_record(folder)
        self.assertEqual(converted, {
            'Path': 'data', 'Name': 'data', 'Size': 999, 'ID': C + '#revision',
            'ModTime': TIME, 'IsDir': True,
        })
        file = batch.native_child_to_record(new_child(
            'FILE::zone::f', 'report', extension='pdf', size=13))
        self.assertEqual(file['ID'], 'FILE::zone::f')
        self.assertEqual(file['Path'], 'report.pdf')
        self.assertFalse(file['IsDir'])
        self.assertEqual(file['ModTime'], TIME)
        self.assertEqual(file['Size'], 13)

    def test_first_two_folders_prepare_one_atomic_resume_checkpoint(self):
        original = checkpoint()
        before = deepcopy(original)
        planned, stats = batch.plan_checkpoint(original, reply(), batch_size=2)
        self.assertEqual(original, before)
        self.assertEqual(planned['pending'], ['A/C'])
        self.assertEqual(planned['finished'], ['', 'A', 'B'])
        self.assertEqual(len(planned['entries']), 5)
        self.assertEqual(planned['entries'][2]['Path'], 'A/first.txt')
        self.assertEqual(planned['entries'][3]['Path'], 'A/C')
        self.assertEqual(planned['entries'][4]['Path'], 'B/other.txt')
        self.assertEqual((stats['folders_prepared'], stats['entries_prepared'],
                          stats['directories_prepared']), (2, 3, 1))
        self.assertEqual(stats['checkpoints_written'], 0)
        self.assertFalse(stats['production_commit_authorized'])
        self.assertFalse(stats['timestamps_verified'])
        self.assertFalse(stats['full_field_parity_verified'])

    def test_completed_batch_matches_original_sequential_queue_order(self):
        original = checkpoint()
        expected = deepcopy(original)
        for parent, child_list in [('A', [
                batch.native_child_to_record(c) for c in reply()[0]['items']]),
                                   ('B', [
                batch.native_child_to_record(c) for c in reply()[1]['items']])]:
            entries, subdirs = inventory.rebase_folder(parent, child_list)
            known = set(expected['pending'] + expected['finished'])
            added = [name for name in subdirs if name not in known]
            expected = {'version': 1, 'source': 'icloud:',
                        'pending': expected['pending'][1:] + added,
                        'finished': expected['finished'] + [parent],
                        'entries': expected['entries'] + entries}
        planned, _ = batch.plan_checkpoint(original, reply(), batch_size=2)
        self.assertEqual(planned, expected)

    def test_out_of_order_native_results_match_requested_checkpoint_order(self):
        native = list(reversed(reply()))
        planned, _ = batch.plan_checkpoint(checkpoint(), native, batch_size=2)
        self.assertEqual(planned['finished'], ['', 'A', 'B'])

    def test_one_folder_only_keeps_second_folder_pending(self):
        planned, stats = batch.plan_checkpoint(checkpoint(), reply()[:1],
                                               batch_size=1)
        self.assertEqual(planned['pending'], ['B', 'A/C'])
        self.assertEqual(planned['finished'], ['', 'A'])
        self.assertEqual(stats['folders_prepared'], 1)

    def test_batched_conversion_does_not_call_save_or_network(self):
        with (patch.object(inventory, 'save_progress',
                           side_effect=AssertionError('must not write')),
              patch.object(inventory, 'list_one_folder',
                           side_effect=AssertionError('must not list')),
              patch.object(inventory.subprocess, 'run',
                           side_effect=AssertionError('must not start subprocess'))):
            planned, stats = batch.plan_checkpoint(checkpoint(), reply(), batch_size=2)
        self.assertEqual(stats['checkpoints_written'], 0)
        self.assertEqual(planned['finished'], ['', 'A', 'B'])

    def test_incomplete_second_folder_rolls_back_entire_planning_operation(self):
        state = checkpoint()
        original = deepcopy(state)
        malformed = reply()
        malformed[1]['directChildrenCount'] = 2
        with self.assertRaisesRegex(batch.BatchCheckpointError, 'NOT_VERIFIED'):
            batch.plan_checkpoint(state, malformed, batch_size=2)
        self.assertEqual(state, original)

    def test_invalid_modtime_is_fail_closed_even_with_valid_ids(self):
        state = checkpoint()
        malformed = reply()
        malformed[1]['items'][0]['dateModified'] = 'not a date'
        with self.assertRaisesRegex(batch.BatchCheckpointError, 'NOT_VERIFIED'):
            batch.plan_checkpoint(state, malformed, batch_size=2)
        self.assertEqual(state, checkpoint())

    def test_rejects_recursive_or_control_names(self):
        for name in ('../secret', 'top/secret', 'a\\b', '.', '..',
                     'bad\nname', ''):
            with self.subTest(name=name), self.assertRaisesRegex(
                    batch.BatchCheckpointError, 'UNSAFE|INVALID'):
                batch.native_child_to_record(
                    new_child('FILE::zone::f', name, extension=''))

    def test_rejects_wrong_type_ids_and_missing_file_size(self):
        for kwargs in (
            {'identifier': 'FOLDER::zone::x', 'name': 'bad'},
            {'identifier': 'FILE::zone::x', 'name': 'bad', 'kind': 'FOLDER'},
            {'identifier': 'FILE::zone::x#etag', 'name': 'bad'},
            {'identifier': 'FILE::zone::x', 'name': 'bad', 'size': True},
            {'identifier': 'FILE::zone::x', 'name': 'bad', 'size': -2},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(batch.BatchCheckpointError):
                batch.native_child_to_record(new_child(**kwargs))

    def test_rejects_unsafe_etag_or_missing_directory_quota(self):
        for overrides in ({'etag': 'bad#etag'}, {'etag': None},
                          {'quota': None}, {'quota': True}, {'quota': -2}):
            with self.subTest(overrides=overrides), self.assertRaises(batch.BatchCheckpointError):
                batch.native_child_to_record(new_child(
                    C, 'C', kind='FOLDER', extension='', **overrides))

    def test_rejects_ambiguous_directory_path_before_checkpoint_transition(self):
        state = checkpoint()
        malformed = reply()
        malformed[0]['items'].append(new_child(
            'FOLDER::zone::different', 'C', kind='FOLDER', extension=''))
        malformed[0]['directChildrenCount'] += 1
        with self.assertRaisesRegex(batch.BatchCheckpointError, 'NOT_VERIFIED'):
            batch.plan_checkpoint(state, malformed, batch_size=2)
        self.assertEqual(state, checkpoint())

    def test_rejects_pending_directory_ids_which_cannot_be_resumed(self):
        state = checkpoint()
        state['entries'][1]['ID'] = 'invalid'
        with self.assertRaisesRegex(batch.BatchCheckpointError, 'NOT_ELIGIBLE'):
            batch.plan_checkpoint(state, reply(), batch_size=2)

    def test_rejects_unsupported_sizes_and_invalid_batch_size(self):
        for size in (0, 5, -1, True, 1.25):
            with self.subTest(size=size), self.assertRaisesRegex(
                    batch.BatchCheckpointError, 'INVALID_BATCH_CHECKPOINT_SIZE'):
                batch.plan_checkpoint(checkpoint(), reply(), batch_size=size)

    def test_rejects_invalid_modtime_formats(self):
        invalid = ('', None, '2026-11-09', '2026-11-09T11:50:00',
                   '2026-11-09T25:50:00Z', '2026-11-09T11:50:00.1234567890Z',
                   '2026-11-09T11:50:00+24:00')
        for value in invalid:
            with self.subTest(value=value):
                self.assertFalse(batch.valid_timestamp(value))
        for value in (TIME, '2026-11-09T11:50:00.123456789Z',
                      '2026-11-09T05:50:00-06:00',
                      '0001-01-01T00:00:00Z'):
            with self.subTest(value=value):
                self.assertTrue(batch.valid_timestamp(value))

    def test_progress_and_native_envelopes_are_never_mutated(self):
        progress = checkpoint()
        incoming = reply()
        originals = deepcopy(progress), deepcopy(incoming)
        batch.plan_checkpoint(progress, incoming, batch_size=2)
        self.assertEqual((progress, incoming), originals)

    def test_duplicate_pending_directory_path_and_limits_are_rejected(self):
        state = checkpoint()
        state['pending'].append('B')
        with self.assertRaises(batch.BatchCheckpointError):
            batch.plan_checkpoint(state, reply(), batch_size=2)
        state = checkpoint()
        with patch.object(inventory, 'MAX_TOTAL_RECORDS', 4):
            with self.assertRaisesRegex(batch.BatchCheckpointError, 'LIMIT_EXCEEDED'):
                batch.plan_checkpoint(state, reply(), batch_size=2)

    def test_zero_pending_or_wrong_source_rejected(self):
        state = checkpoint()
        state['pending'] = []
        with self.assertRaisesRegex(batch.BatchCheckpointError, 'INVALID_BATCH_CHECKPOINT'):
            batch.plan_checkpoint(state, [], batch_size=2)
        state = checkpoint()
        state['source'] = 'other:'
        with self.assertRaisesRegex(batch.BatchCheckpointError, 'INVALID_BATCH_CHECKPOINT'):
            batch.plan_checkpoint(state, reply(), batch_size=2)


if __name__ == '__main__':
    unittest.main()

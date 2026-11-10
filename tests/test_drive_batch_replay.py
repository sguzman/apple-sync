"""Offline replay tests for bracketing single-folder responses and one batch."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay as replay
import drive_inventory as inventory

FIRST = 'FOLDER::zone::first'
SECOND = 'FOLDER::zone::second'
CHILD = 'FOLDER::zone::child'
OTHER_CHILD = 'FOLDER::zone::other'
FILE = 'FILE::zone::file'
REQ = [('Private', FIRST), ('Secondary', SECOND)]
OLD = '2026-11-10T10:00:00Z'
NEW = '2026-11-10T10:00:07Z'


def directory(stamp=OLD, *, identifier=CHILD, name='child', etag='rev',
              quota=11):
    return {'drivewsid': identifier, 'name': name, 'extension': '',
            'type': 'FOLDER', 'dateModified': stamp,
            'assetQuota': quota, 'etag': etag}


def file_record():
    return {'drivewsid': FILE, 'name': 'note', 'extension': 'txt',
            'type': 'FILE', 'dateModified': OLD, 'size': 4}


def envelope(folder=FIRST, *, timestamp=OLD, second_directory=None,
             include_directory=True, extra_items=None):
    children = [file_record()]
    if include_directory:
        children.append(directory(timestamp))
    if second_directory is not None:
        children.append(directory(second_directory,
                                  identifier=OTHER_CHILD, name='other'))
    children.extend(extra_items or [])
    return {'drivewsid': folder, 'type': 'FOLDER', 'status': 'OK',
            'directChildrenCount': len(children), 'items': children}


def secondary():
    return {'drivewsid': SECOND, 'type': 'FOLDER', 'status': 'OK',
            'directChildrenCount': 0, 'items': []}


def triplet(*, before=OLD, batched=OLD, after=OLD, extras=None):
    extras = extras or {}
    return {
        'single_before': [envelope(timestamp=before,
                                   second_directory=extras.get('before'))],
        'batched': [secondary(), envelope(timestamp=batched,
                                          second_directory=extras.get('batch'))],
        'single_after': [envelope(timestamp=after,
                                  second_directory=extras.get('after'))],
    }


class ReplayTests(unittest.TestCase):
    def test_identical_bracketing_and_batch_reads(self):
        result = replay.compare_triplet(REQ, **triplet())
        self.assertEqual(result['outcome'],
                         'REPLAY_ALL_DIRECTORY_TIMESTAMPS_IDENTICAL')
        self.assertEqual(result['directory_children_checked'], 1)
        self.assertEqual(result['directory_children_stable'], 1)
        self.assertEqual(result['directory_children_batch_only_difference'], 0)
        self.assertEqual(result['directory_children_single_read_difference'], 0)
        self.assertFalse(result['production_commit_authorized'])
        self.assertEqual(result['checkpoint_writes'], 0)
        self.assertEqual(result['checkpoints_advanced'], 0)

    def test_stable_singles_differing_batch_is_only_an_observation(self):
        result = replay.compare_triplet(REQ, **triplet(batched=NEW))
        self.assertEqual(result['outcome'],
                         'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED')
        self.assertEqual(result['directory_children_batch_only_difference'], 1)
        self.assertFalse(result['production_commit_authorized'])

    def test_second_single_differs_when_directory_changes_between_reads(self):
        result = replay.compare_triplet(REQ, **triplet(after=NEW))
        self.assertEqual(result['outcome'],
                         'REPLAY_SINGLE_READS_DIFFER_OBSERVED')
        self.assertEqual(result['directory_children_single_read_difference'], 1)

    def test_batch_matches_last_single_but_not_first(self):
        result = replay.compare_triplet(
            REQ, **triplet(before=OLD, batched=NEW, after=NEW))
        self.assertEqual(result['outcome'],
                         'REPLAY_SINGLE_READS_DIFFER_OBSERVED')

    def test_mixed_directories_remain_explicitly_ambiguous(self):
        result = replay.compare_triplet(REQ, **triplet(
            batched=NEW, extras={'before': OLD, 'batch': OLD, 'after': NEW}))
        self.assertEqual(result['outcome'], 'REPLAY_MIXED_DIFFERENCES_OBSERVED')
        self.assertEqual(result['directory_children_batch_only_difference'], 1)
        self.assertEqual(result['directory_children_single_read_difference'], 1)
        self.assertEqual(result['directory_children_checked'], 2)
        self.assertFalse(result['production_commit_authorized'])

    def test_empty_target_directory_is_not_a_useful_probe(self):
        source = [envelope(include_directory=False)]
        batch = [secondary(), envelope(include_directory=False)]
        with self.assertRaisesRegex(replay.ReplayError,
                                    'REPLAY_NO_DIRECTORY_CHILDREN'):
            replay.compare_triplet(
                REQ, single_before=source, batched=batch, single_after=source)

    def test_different_child_identity_fails_closed(self):
        samples = triplet()
        samples['batched'][1]['items'][1]['drivewsid'] = 'FOLDER::zone::changed'
        with self.assertRaisesRegex(replay.ReplayError,
                                    'REPLAY_CHILD_IDENTITY_CHANGED'):
            replay.compare_triplet(REQ, **samples)

    def test_different_etag_fails_closed(self):
        samples = triplet()
        samples['batched'][1]['items'][1]['etag'] = 'new-revision'
        with self.assertRaisesRegex(replay.ReplayError,
                                    'REPLAY_CHILD_IDENTITY_CHANGED'):
            replay.compare_triplet(REQ, **samples)

    def test_different_directory_quota_fails_closed(self):
        samples = triplet()
        samples['batched'][1]['items'][1]['assetQuota'] = 12
        with self.assertRaisesRegex(replay.ReplayError,
                                    'REPLAY_NON_TIME_METADATA_CHANGED'):
            replay.compare_triplet(REQ, **samples)

    def test_different_filename_fails_closed(self):
        samples = triplet()
        samples['batched'][1]['items'][0]['name'] = 'other'
        with self.assertRaisesRegex(replay.ReplayError,
                                    'REPLAY_NON_TIME_METADATA_CHANGED'):
            replay.compare_triplet(REQ, **samples)

    def test_invalid_native_envelope_fails_closed(self):
        samples = triplet()
        samples['batched'][0]['directChildrenCount'] = 1
        with self.assertRaisesRegex(replay.ReplayError,
                                    'REPLAY_UNVERIFIED_NATIVE_RESPONSE'):
            replay.compare_triplet(REQ, **samples)

    def test_invalid_modification_date_fails_closed(self):
        samples = triplet()
        samples['single_after'][0]['items'][1]['dateModified'] = 'private secret'
        with self.assertRaisesRegex(replay.ReplayError,
                                    'REPLAY_UNVERIFIED_NATIVE_RESPONSE') as error:
            replay.compare_triplet(REQ, **samples)
        self.assertNotIn('private secret', str(error.exception))

    def test_one_folder_or_many_folders_is_not_a_valid_replay(self):
        samples = triplet()
        for request in (REQ[:1], REQ + [('Third', 'FOLDER::zone::third')],
                        None, [], 'private names'):
            with self.subTest(request_type=type(request).__name__):
                with self.assertRaisesRegex(
                        replay.ReplayError, 'REPLAY_REQUIRES_TWO_FOLDERS'):
                    replay.compare_triplet(request, **samples)

    def test_no_output_contains_private_paths_or_identifiers(self):
        result = replay.compare_triplet(REQ, **triplet(batched=NEW))
        values = repr(result)
        for private in ('Private', 'Secondary', FIRST, SECOND, CHILD, OLD, NEW):
            self.assertNotIn(private, values)

    def test_no_mutation_network_or_checkpoint_io(self):
        samples = triplet(batched=NEW)
        original = deepcopy(samples)
        with (patch.object(inventory, 'save_progress',
                           side_effect=AssertionError('checkpoint write')),
              patch.object(inventory.subprocess, 'run',
                           side_effect=AssertionError('network subprocess'))):
            result = replay.compare_triplet(REQ, **samples)
        self.assertEqual(samples, original)
        self.assertEqual(result['outcome'],
                         'REPLAY_BATCH_ONLY_DIFFERENCE_OBSERVED')

    def test_unrelated_second_folder_response_must_be_well_formed(self):
        samples = triplet()
        samples['batched'][0]['status'] = 'ERROR'
        with self.assertRaisesRegex(replay.ReplayError,
                                    'REPLAY_UNVERIFIED_NATIVE_RESPONSE'):
            replay.compare_triplet(REQ, **samples)


if __name__ == '__main__':
    unittest.main()

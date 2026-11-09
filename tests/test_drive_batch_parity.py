"""Fail-closed timestamp and core-field comparison; no cloud requests."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_checkpoint as checkpoint
import drive_batch_parity as parity
import drive_inventory as inventory

ROOT = 'FOLDER::zone::parent'
CHILD = 'FOLDER::zone::child'
STAMP = '2026-11-09T11:50:00.123456789Z'
REQUEST = [('Private', ROOT)]


def response():
    children = [
        {'drivewsid': 'FILE::zone::f', 'type': 'FILE',
         'name': 'media', 'extension': 'mov', 'size': 1923,
         'dateModified': STAMP, 'etag': 'ignored-for-files'},
        {'drivewsid': CHILD, 'type': 'FOLDER',
         'name': 'child', 'extension': '', 'assetQuota': 500,
         'dateModified': STAMP, 'etag': 'revision'},
    ]
    return [{
        'drivewsid': ROOT, 'type': 'FOLDER', 'status': 'OK',
        'directChildrenCount': 2, 'items': children,
    }]


def references():
    converted = checkpoint.convert_batch(REQUEST, response())
    return [(name, children) for name, children in converted]


class CoreParityTests(unittest.TestCase):
    def test_verified_ids_paths_etags_sizes_names_and_nanos(self):
        report = parity.compare_core_fields(REQUEST, response(), references())
        self.assertTrue(report['identity_verified'])
        self.assertTrue(report['core_fields_verified'])
        self.assertTrue(report['timestamps_verified'])
        self.assertEqual(report['folders_matched'], 1)
        self.assertEqual(report['entries_matched'], 2)
        self.assertFalse(report['mime_types_verified'])
        self.assertFalse(report['unmodeled_fields_verified'])
        self.assertFalse(report['production_commit_authorized'])
        self.assertEqual(report['checkpoint_writes'], 0)

    def test_one_nanosecond_difference_fails_closed(self):
        legacy = references()
        legacy[0][1][0]['ModTime'] = '2026-11-09T11:50:00.123456788Z'
        with self.assertRaisesRegex(parity.BatchParityError, 'CORE_FIELD_MISMATCH'):
            parity.compare_core_fields(REQUEST, response(), legacy)

    def test_equivalent_time_with_different_timezone_still_needs_review(self):
        legacy = references()
        legacy[0][1][1]['ModTime'] = '2026-11-09T05:50:00.123456789-06:00'
        with self.assertRaisesRegex(parity.BatchParityError, 'CORE_FIELD_MISMATCH'):
            parity.compare_core_fields(REQUEST, response(), legacy)

    def test_missing_modtime_or_name_is_rejected(self):
        for field in ('ModTime', 'Name', 'Size', 'Path', 'IsDir', 'ID'):
            legacy = references()
            legacy[0][1][0].pop(field)
            with self.subTest(field=field), self.assertRaises(parity.BatchParityError):
                parity.compare_core_fields(REQUEST, response(), legacy)

    def test_size_or_type_differences_never_reconcile_by_guessing(self):
        for key, val in (('Size', 1924), ('IsDir', True), ('Name', 'wrong.mov'),
                         ('Path', 'other.mov'), ('ID', 'FILE::zone::other')):
            legacy = references()
            legacy[0][1][0][key] = val
            with self.subTest(key=key), self.assertRaises(parity.BatchParityError):
                parity.compare_core_fields(REQUEST, response(), legacy)

    def test_invalid_native_timestamp_never_commits(self):
        native = response()
        native[0]['items'][0]['dateModified'] = 'invalid'
        with (patch.object(inventory, 'save_progress',
                           side_effect=AssertionError('write forbidden')),
              self.assertRaises(parity.BatchParityError)):
            parity.compare_core_fields(REQUEST, native, references())

    def test_reference_duplicates_and_missing_children_fail(self):
        extra = references()
        extra[0][1].append(deepcopy(extra[0][1][0]))
        with self.assertRaises(parity.BatchParityError):
            parity.compare_core_fields(REQUEST, response(), extra)
        fewer = references()
        fewer[0][1].pop()
        with self.assertRaises(parity.BatchParityError):
            parity.compare_core_fields(REQUEST, response(), fewer)

    def test_does_not_mutate_inputs_or_start_network(self):
        native = response()
        legacy = references()
        before = deepcopy(native), deepcopy(legacy)
        with (patch.object(inventory, 'save_progress',
                           side_effect=AssertionError('write')),
              patch.object(inventory, 'list_one_folder',
                           side_effect=AssertionError('network')),
              patch.object(inventory.subprocess, 'run',
                           side_effect=AssertionError('network'))):
            parity.compare_core_fields(REQUEST, native, legacy)
        self.assertEqual((native, legacy), before)

    def test_no_raw_user_paths_in_errors(self):
        legacy = references()
        legacy[0][1][0]['ModTime'] = 'Private folder secret goes here'
        try:
            parity.compare_core_fields(REQUEST, response(), legacy)
        except parity.BatchParityError as exc:
            self.assertNotIn('Private', str(exc))
            self.assertNotIn('secret', str(exc))
        else:
            self.fail('unverified timestamp should fail')


if __name__ == '__main__':
    unittest.main()

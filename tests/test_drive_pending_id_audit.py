"""Privacy-safe, offline tests for the pending folder ID audit."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_pending_id_audit as audit


class PendingIdAuditTests(unittest.TestCase):
    def test_eligible_ids(self):
        progress = {
            'pending': ['private/a', 'private/b'],
            'entries': [
                {'Path': 'private/a', 'IsDir': True, 'ID': 'FOLDER::com.apple.CloudDocs::one#etag'},
                {'Path': 'private/b', 'IsDir': True, 'ID': 'APP_CONTAINER::zone.example::two#etag'},
                {'Path': 'private/file', 'IsDir': False, 'ID': 'FILE::zone::foo'},
            ],
        }
        result = audit.summarize(progress)
        self.assertTrue(result['all_pending_have_distinct_candidate_ids'])
        self.assertEqual(result['candidate_ids'], 2)
        self.assertEqual(result['missing_directory_records'], 0)
        self.assertNotIn('private', str(result))
        self.assertNotIn('one', str(result))

    def test_missing_duplicate_and_unrecognized(self):
        progress = {
            'pending': ['a', 'b', 'c', 'd', 'e', 'f', 'g'],
            'entries': [
                {'Path': 'a', 'IsDir': True},
                {'Path': 'b', 'IsDir': True, 'ID': 'wrong'},
                {'Path': 'c', 'IsDir': True, 'ID': 'FOLDER::z::same#etag1'},
                {'Path': 'd', 'IsDir': True, 'ID': 'FOLDER::z::same#etag2'},
                {'Path': 'e', 'IsDir': True, 'ID': 'FOLDER::z::e'},
                {'Path': 'e', 'IsDir': True, 'ID': 'FOLDER::z::e'},
                {'Path': 'f', 'IsDir': False, 'ID': 'FOLDER::z::f'},
            ],
        }
        result = audit.summarize(progress)
        self.assertEqual(result['missing_ids'], 1)
        self.assertEqual(result['unrecognized_ids'], 1)
        self.assertEqual(result['candidate_ids'], 2)
        self.assertEqual(result['duplicate_candidate_ids'], 1)
        self.assertEqual(result['ambiguous_directory_records'], 1)
        self.assertEqual(result['missing_directory_records'], 2)
        self.assertFalse(result['all_pending_have_distinct_candidate_ids'])
        self.assertEqual(sum(result[x] for x in (
            'candidate_ids', 'missing_directory_records',
            'ambiguous_directory_records', 'missing_ids',
            'unrecognized_ids', 'root_pending')),
            result['pending_folders'])

    def test_root_pending_before_first_listing(self):
        result = audit.summarize({'pending': [''], 'entries': []})
        self.assertEqual(result['root_pending'], 1)
        self.assertFalse(result['all_pending_have_distinct_candidate_ids'])

    def test_id_validation_rejects_secrets_or_paths(self):
        bad = ['secret', 'FOLDER::zone::with whitespace',
               'FOLDER::zone::path/with?query#etag',
               'FILE::zone::document']
        for item in bad:
            with self.subTest(item=item):
                self.assertIsNone(audit.DRIVE_ID.fullmatch(item))


if __name__ == '__main__':
    unittest.main()

"""Pure offline checks for native Apple metadata versus rclone lsjson output."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_equivalence as comparison


PARENT = 'FOLDER::z::parent'
OTHER = 'FOLDER::z::other'
FILE1 = 'FILE::z::f1'
FILE2 = 'FILE::z::f2'
CHILD = 'FOLDER::z::child'


def target(identifier, children):
    return {'drivewsid': identifier, 'status': 'OK', 'type': 'FOLDER',
            'directChildrenCount': len(children), 'items': children}


def native_file(identifier, size=7):
    return {'drivewsid': identifier, 'type': 'FILE', 'size': size, 'etag': 'raw-f'}


def native_dir(identifier):
    return {'drivewsid': identifier, 'type': 'FOLDER', 'etag': 'raw-d'}


def legacy_file(identifier, name='file.dat', size=7):
    # Actual rclone iCloud file Object.ID() emits bare Drive ID.
    return {'Path': name, 'ID': identifier, 'IsDir': False, 'Size': size}


def legacy_dir(identifier, name='folder'):
    # rclone's directories use the joined DriveID#ETag form.
    return {'Path': name, 'ID': identifier + '#raw-d', 'IsDir': True, 'Size': 0}


class EquivalenceTests(unittest.TestCase):
    def test_matches_file_directory_and_out_of_order_folders(self):
        batch = [('private/a', PARENT), ('private/b', OTHER)]
        native = [target(OTHER, []),
                  target(PARENT, [native_file(FILE1), native_dir(CHILD)])]
        legacy = [('private/b', []),
                  ('private/a', [legacy_dir(CHILD), legacy_file(FILE1)])]
        self.assertEqual(comparison.compare_with_legacy(batch, native, legacy),
                         {'folders_matched': 2, 'entries_matched': 2,
                          'files_matched': 1, 'directories_matched': 1,
                          'equivalent': True, 'checkpoint_writes': 0})

    def test_preserves_same_visible_name_for_distinct_file_ids(self):
        batch = [('private/a', PARENT)]
        native = [target(PARENT, [native_file(FILE1), native_file(FILE2)])]
        legacy = [('private/a', [legacy_file(FILE1, name='same'),
                                 legacy_file(FILE2, name='same')])]
        self.assertEqual(comparison.compare_with_legacy(batch, native, legacy)
                         ['files_matched'], 2)

    def test_rejects_identity_mismatch_and_wrong_size(self):
        batch = [('private/a', PARENT)]
        native = [target(PARENT, [native_file(FILE1)])]
        for legacy in ([legacy_file(FILE2)], [legacy_file(FILE1, size=8)],
                       [legacy_file(FILE1, name='../unsafe')],
                       [legacy_file(FILE1, name='unsafe/name')]):
            with self.subTest(legacy=legacy), self.assertRaises(comparison.BatchEquivalenceError):
                comparison.compare_with_legacy(batch, native, [('private/a', legacy)])

    def test_rejects_missing_target_and_reference(self):
        batch = [('private/a', PARENT)]
        native = [target(PARENT, [])]
        for ref in ([], [('private/elsewhere', [])], [('private/a', []), ('private/a', [])]):
            with self.subTest(ref=ref), self.assertRaises(comparison.BatchEquivalenceError):
                comparison.compare_with_legacy(batch, native, ref)

    def test_rejects_incorrect_directory_id_and_file_suffix(self):
        batch = [('private/a', PARENT)]
        for raw, row in ((native_dir(CHILD), legacy_dir(CHILD) | {'ID': CHILD}),
                         (native_dir(CHILD), legacy_dir(CHILD) | {'ID': CHILD + '#wrong'}),
                         (native_file(FILE1), legacy_file(FILE1) | {'ID': FILE1 + '#e'})):
            with self.subTest(row=row), self.assertRaises(comparison.BatchEquivalenceError):
                comparison.compare_with_legacy(batch, [target(PARENT, [raw])],
                                                [('private/a', [row])])

    def test_rejects_unknown_child_type_and_missing_file_size(self):
        batch = [('private/a', PARENT)]
        for raw in (native_file(FILE1) | {'type': 'ALIEN'},
                    native_file(FILE1) | {'size': None},
                    native_file(FILE1) | {'size': True}):
            with self.subTest(raw=raw), self.assertRaises(comparison.BatchEquivalenceError):
                comparison.compare_with_legacy(batch, [target(PARENT, [raw])],
                                                [('private/a', [legacy_file(FILE1)])])

    def test_rejects_wrong_type_or_missing_reference_size(self):
        batch = [('private/a', PARENT)]
        native = [target(PARENT, [native_file(FILE1)])]
        for record in (legacy_file(FILE1) | {'IsDir': True},
                       legacy_file(FILE1) | {'IsDir': 0},
                       legacy_file(FILE1) | {'Size': None}):
            with self.subTest(record=record), self.assertRaises(comparison.BatchEquivalenceError):
                comparison.compare_with_legacy(batch, native, [('private/a', [record])])

    def test_rejects_incomplete_native_response(self):
        batch = [('private/a', PARENT)]
        native = [target(PARENT, [native_file(FILE1)])]
        native[0]['directChildrenCount'] = 2
        with self.assertRaises(comparison.BatchEquivalenceError):
            comparison.compare_with_legacy(batch, native,
                                            [('private/a', [legacy_file(FILE1)])])

    def test_rejections_do_not_disclose_paths_or_ids(self):
        with self.assertRaises(comparison.BatchEquivalenceError) as caught:
            comparison.compare_with_legacy(
                [('secret/folder', PARENT)], [target(PARENT, [native_file(FILE1)])],
                [('secret/folder', [legacy_file(FILE2)])])
        for secret in ('secret', PARENT, FILE1, FILE2):
            self.assertNotIn(secret, str(caught.exception))


if __name__ == '__main__':
    unittest.main()

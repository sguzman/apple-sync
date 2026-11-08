"""Offline coverage for proposed batched iCloud Drive metadata envelopes."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_contract as batch


def folder(identifier: str, children=(), **overrides):
    response = {
        'drivewsid': identifier, 'type': 'FOLDER', 'status': 'OK',
        'items': list(children), 'directChildrenCount': len(children),
    }
    response.update(overrides)
    return response


class BatchContractTests(unittest.TestCase):
    def setUp(self):
        self.ids = ['FOLDER::z::1', 'APP_CONTAINER::z::2']
        self.request = [('private/a', self.ids[0]), ('private/b', self.ids[1])]

    def test_plan_preserves_order_and_drops_etag(self):
        progress = {
            'pending': ['private/a', 'private/b'],
            'entries': [
                {'Path': 'private/a', 'IsDir': True, 'ID': self.ids[0] + '#one'},
                {'Path': 'private/b', 'IsDir': True, 'ID': self.ids[1] + '#two'},
            ],
        }
        self.assertEqual(batch.plan_batches(progress, size=1),
                         [[self.request[0]], [self.request[1]]])
        self.assertEqual(batch.plan_batches(progress, size=2), [self.request])

    def test_plan_rejects_untrusted_checkpoint(self):
        with self.assertRaisesRegex(batch.BatchContractError, 'INELIGIBLE'):
            batch.plan_batches({'pending': ['private/a'], 'entries': []})
        for value in (0, 9, True, 1.5):
            with self.subTest(value=value), self.assertRaises(batch.BatchContractError):
                batch.plan_batches({'pending': [], 'entries': []}, size=value)

    def test_empty_pending_is_safe(self):
        self.assertEqual(batch.plan_batches({'pending': [], 'entries': []}), [])

    def test_accepts_out_of_order_exact_mapping(self):
        child = {'drivewsid': 'FILE::z::child'}
        result = batch.validate_response(self.request,
            [folder(self.ids[1]), folder(self.ids[0], [child])])
        self.assertEqual(result, [('private/a', [child]), ('private/b', [])])

    def test_rejects_missing_extra_and_duplicate_folders(self):
        for response in ([folder(self.ids[0])],
                         [folder(self.ids[0]), folder('FOLDER::z::unrequested')],
                         [folder(self.ids[0]), folder(self.ids[0])]):
            with self.subTest(response=response), self.assertRaises(batch.BatchContractError):
                batch.validate_response(self.request, response)

    def test_rejects_non_string_response_identity(self):
        with self.assertRaises(batch.BatchContractError):
            batch.validate_response([self.request[0]],
                [folder(self.ids[0], drivewsid=['bad'])])

    def test_rejects_unknown_status_and_non_directory(self):
        for override in ({'status': 'NOT_FOUND'}, {'type': 'FILE'}):
            with self.subTest(override=override), self.assertRaises(batch.BatchContractError):
                batch.validate_response([self.request[0]],
                                        [folder(self.ids[0], **override)])

    def test_rejects_missing_or_wrong_counts(self):
        child = {'drivewsid': 'FILE::z::child'}
        for payload in (
            {'drivewsid': self.ids[0], 'type': 'FOLDER', 'items': [child]},
            folder(self.ids[0], [child], directChildrenCount=2),
            folder(self.ids[0], [child], numberOfItems=0),
            folder(self.ids[0], [child], numberOfItems=True),
        ):
            with self.subTest(payload=payload), self.assertRaises(batch.BatchContractError):
                batch.validate_response([self.request[0]], [payload])

    def test_rejects_missing_and_duplicate_child_ids(self):
        for children in ([{}], [{'drivewsid': 'x'}, {'drivewsid': 'x'}]):
            with self.subTest(children=children), self.assertRaises(batch.BatchContractError):
                batch.validate_response([self.request[0]],
                                        [folder(self.ids[0], children)])

    def test_rejects_bad_requests_without_leaking_paths(self):
        for request in ([], [('private/secret', self.ids[0])]*9,
                        [('private/secret', self.ids[0] + '#etag')],
                        [self.request[0], ('another', self.ids[0])],
                        [('private/secret', self.ids[0]),
                         ('private/secret', self.ids[1])]):
            with self.subTest(request=request):
                with self.assertRaises(batch.BatchContractError) as caught:
                    batch.validate_response(request, [])
                self.assertNotIn('private', str(caught.exception))
                self.assertNotIn(self.ids[0], str(caught.exception))


if __name__ == '__main__':
    unittest.main()

"""Offline buffered batch reader with the real resumable-walk interface."""
from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_reader as reader
import drive_batch_checkpoint as converter
import drive_inventory as inventory

A = 'FOLDER::zone::a'
B = 'FOLDER::zone::b'
C = 'FOLDER::zone::c'
TIME = '2026-11-09T11:50:00Z'


def item(identifier, kind, name, *, size=1, etag='e'):
    return {
        'drivewsid': identifier, 'type': kind, 'name': name,
        'extension': '', 'size': size, 'assetQuota': 0,
        'etag': etag, 'dateModified': TIME,
    }


def response(requested):
    data = {
        A: [item('FILE::zone::first', 'FILE', 'first'),
            item(C, 'FOLDER', 'new', etag='child-etag')],
        B: [item('FILE::zone::second', 'FILE', 'second')]
    }
    return [{'drivewsid': drive_id, 'status': 'OK', 'type': 'FOLDER',
             'directChildrenCount': len(data[drive_id]), 'items': data[drive_id]}
            for _folder, drive_id in requested]


def progress():
    return {'version': 1, 'source': 'icloud:', 'pending': ['A', 'B'],
            'finished': [''], 'entries': [
                {'Path': 'A', 'IsDir': True, 'Size': 0, 'ID': A+'#v',
                 'ModTime': TIME},
                {'Path': 'B', 'IsDir': True, 'Size': 0, 'ID': B+'#v',
                 'ModTime': TIME},
            ]}


class BufferedBatchTests(unittest.TestCase):
    def test_parity_gate_prevents_all_native_requests(self):
        read = Mock()
        check = Mock(side_effect=AssertionError('must not read checkpoint'))
        current = reader.BufferedBatchReader(
            load_checkpoint=check, read_native=read,
            backup_busy=lambda: False)
        with self.assertRaisesRegex(reader.BatchReaderError,
                                    'FIELD_PARITY_NOT_AUTHORIZED'):
            current('A', timeout=20)
        read.assert_not_called()
        check.assert_not_called()

    def test_buffered_reads_use_one_native_call_for_two_folders(self):
        state = progress()
        calls = []
        def fetch(request, *, timeout):
            calls.append((request, timeout))
            return response(request)
        current = reader.BufferedBatchReader(
            load_checkpoint=lambda: state, read_native=fetch,
            backup_busy=lambda: False, size=2, verified_field_parity=True)
        a_children = current('A', timeout=25)
        self.assertEqual(len(a_children), 2)
        state['pending'] = ['B', 'A/new']
        state['finished'].append('A')
        b_children = current('B', timeout=25)
        self.assertEqual(len(b_children), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual([x[0] for x in calls[0][0]], ['A', 'B'])
        self.assertEqual(calls[0][1], 25)

    def test_actual_inventory_walker_preserves_per_folder_resume_semantics(self):
        with TemporaryDirectory() as temp:
            path = Path(temp)
            initial = progress()
            inventory.save_progress(path, initial)
            requests = []
            def fetch(batch, *, timeout):
                requests.append(len(batch))
                if batch[0][0] == 'A':
                    return response(batch)
                if batch[0][0] == 'A/new':
                    return [{'drivewsid': C, 'status': 'OK',
                             'type': 'FOLDER', 'directChildrenCount': 0,
                             'items': []}]
                raise AssertionError('unexpected folder')
            current = reader.BufferedBatchReader(
                load_checkpoint=lambda: inventory.load_progress(path),
                read_native=fetch, backup_busy=lambda: False, size=2,
                verified_field_parity=True)
            with patch.object(inventory.diagnosis, 'backup_busy',
                              return_value=False):
                finished, stats = inventory.walk_resumable(
                    path, reader=current, timeout=30)
            self.assertTrue(finished)
            self.assertEqual(requests, [2, 1])
            self.assertEqual(stats['count'], 5)
            self.assertEqual(len(inventory.load(path)[0]), 5)
            self.assertIsNone(inventory.load_progress(path))

    def test_mismatch_after_first_success_never_reorders_buffer(self):
        state = progress()
        current = reader.BufferedBatchReader(
            load_checkpoint=lambda: state,
            read_native=lambda req, **kw: response(req),
            backup_busy=lambda: False, size=2, verified_field_parity=True)
        current('A', timeout=20)
        with self.assertRaisesRegex(reader.BatchReaderError, 'HEAD_CHANGED'):
            current('B', timeout=20)
        self.assertEqual(current.buffer, [])

    def test_invalid_native_second_folder_does_not_return_first(self):
        state = progress()
        def invalid(req, **kw):
            raw = response(req)
            raw[1]['directChildrenCount'] = 3
            return raw
        current = reader.BufferedBatchReader(
            load_checkpoint=lambda: state, read_native=invalid,
            backup_busy=lambda: False, size=2, verified_field_parity=True)
        with self.assertRaisesRegex(reader.BatchReaderError, 'NOT_VERIFIED'):
            current('A', timeout=20)
        self.assertEqual(current.buffer, [])
        self.assertEqual(state, progress())

    def test_native_callback_errors_are_sanitized(self):
        current = reader.BufferedBatchReader(
            load_checkpoint=progress, read_native=Mock(
                side_effect=RuntimeError('private path name /secret/a')),
            backup_busy=lambda: False, size=2, verified_field_parity=True)
        with self.assertRaises(reader.BatchReaderError) as context:
            current('A', timeout=20)
        self.assertEqual(str(context.exception), 'BATCH_NATIVE_REQUEST_FAILED')
        self.assertNotIn('secret', str(context.exception))

    def test_busy_or_unknown_backup_status_does_not_request_batch(self):
        for callback in (lambda: True, lambda: None,
                         lambda: (_ for _ in ()).throw(OSError('unknown'))):
            calls = Mock()
            current = reader.BufferedBatchReader(
                load_checkpoint=progress, read_native=calls,
                backup_busy=callback, size=2, verified_field_parity=True)
            with self.subTest(callback=callback), self.assertRaises(inventory.InventoryDeferred):
                current('A', timeout=20)
            calls.assert_not_called()

    def test_callback_mutation_cannot_modify_request_validation(self):
        original = progress()
        def attack(batch, **_kw):
            output = response(batch)
            batch[0] = ('another-folder', 'FOLDER::zone::evil')
            return output
        current = reader.BufferedBatchReader(
            load_checkpoint=lambda: original, read_native=attack,
            backup_busy=lambda: False, size=2, verified_field_parity=True)
        self.assertEqual(len(current('A', timeout=20)), 2)
        self.assertEqual(original, progress())

    def test_refuses_no_pending_root_without_alternative_reader(self):
        state = progress()
        state['pending'] = ['']
        current = reader.BufferedBatchReader(
            load_checkpoint=lambda: state,
            read_native=Mock(), backup_busy=lambda: False,
            verified_field_parity=True)
        with self.assertRaises(reader.BatchReaderError):
            current('', timeout=20)

    def test_invalid_batch_size_or_timeout_is_rejected(self):
        for size in (0, 5, True, '2'):
            with self.subTest(size=size), self.assertRaises(reader.BatchReaderError):
                reader.BufferedBatchReader(
                    load_checkpoint=progress, read_native=Mock(), size=size)
        current = reader.BufferedBatchReader(
            load_checkpoint=progress, read_native=Mock(),
            backup_busy=lambda: False, verified_field_parity=True)
        for timeout in (0, 121, -1, '20'):
            with self.subTest(timeout=timeout), self.assertRaises(reader.BatchReaderError):
                current('A', timeout=timeout)

    def test_reader_never_persists_checkpoint_by_itself(self):
        state = progress()
        with patch.object(inventory, 'save_progress',
                          side_effect=AssertionError('no disk writes')):
            current = reader.BufferedBatchReader(
                load_checkpoint=lambda: state,
                read_native=lambda req, **kw: response(req),
                backup_busy=lambda: False, verified_field_parity=True)
            records = current('A', timeout=20)
        self.assertEqual(len(records), 2)
        self.assertEqual(state, progress())


if __name__ == '__main__':
    unittest.main()

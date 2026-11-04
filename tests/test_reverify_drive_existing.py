import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / 'scripts' / 'reverify_drive_existing.py'
spec = importlib.util.spec_from_file_location('reverify_drive_existing', SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def file(path='folder/legacy.bin', size=16):
    return {'Path': path, 'Size': size, 'ID': 'same-source',
            'ModTime': 'same', 'IsDir': False}


class ReverifyDriveExistingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.dest = self.root / 'drive'
        self.dest.mkdir()
        self.data = b'legacy bytes abc'
        self.entry = file(size=len(self.data))
        self.target = self.dest / self.entry['Path']
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.data)
        self.reads = []

    def whole(self, remote, target, deadline):
        self.reads.append(remote)
        target.write_bytes(self.data)
        return len(self.data), hashlib.sha256(self.data).hexdigest()

    def current(self, scope, deadline, folder):
        self.assertEqual(scope, 'icloud:folder')
        self.assertEqual(folder, 'folder')
        return [{**self.entry, 'Path': 'legacy.bin'}]

    def test_existing_legacy_file_receipted_without_overwrite(self):
        before = self.target.stat()
        with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
            outcome = mod.verify_one(self.entry, self.dest, self.state,
                                     time.monotonic() + 90,
                                     whole_reader=self.whole,
                                     list_reader=self.current)
        self.assertEqual(outcome, 'REVERIFIED_RECEIPTED')
        after = self.target.stat()
        self.assertEqual(before.st_ino, after.st_ino)
        self.assertEqual(self.target.read_bytes(), self.data)
        self.assertEqual(self.reads, [
            'icloud:folder/legacy.bin', 'icloud:folder/legacy.bin'])
        self.assertEqual(
            mod.receipts.check_receipt(self.state, self.entry, self.target),
            'verified')

    def test_remote_same_size_different_bytes_never_certify(self):
        bad_data = b'X' * len(self.data)
        def different(remote, target, deadline):
            target.write_bytes(bad_data)
            return len(bad_data), hashlib.sha256(bad_data).hexdigest()
        with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
            outcome = mod.verify_one(self.entry, self.dest, self.state,
                                     time.monotonic() + 90,
                                     whole_reader=different,
                                     list_reader=self.current)
        self.assertEqual(outcome, 'DEFER_CONTENT_MISMATCH')
        self.assertEqual(
            mod.receipts.check_receipt(self.state, self.entry, self.target),
            'missing')
        self.assertEqual(self.target.read_bytes(), self.data)

    def test_two_remote_copies_disagree_never_certify(self):
        calls = 0
        def changing(remote, target, deadline):
            nonlocal calls
            calls += 1
            data = self.data if calls == 1 else b'Z' * len(self.data)
            target.write_bytes(data)
            return len(data), hashlib.sha256(data).hexdigest()
        with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
            outcome = mod.verify_one(self.entry, self.dest, self.state,
                                     time.monotonic() + 90,
                                     whole_reader=changing,
                                     list_reader=self.current)
        self.assertEqual(outcome, 'DEFER_CONTENT_MISMATCH')
        self.assertFalse(mod.receipts.receipt_path(
            self.state, self.entry['Path']).exists())

    def test_changed_parent_metadata_never_certify(self):
        with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
            outcome = mod.verify_one(
                self.entry, self.dest, self.state, time.monotonic() + 90,
                whole_reader=self.whole,
                list_reader=lambda *_: [
                    {**self.entry, 'Path': 'legacy.bin', 'ID': 'changed'}])
        self.assertEqual(outcome, 'DEFER_SOURCE_CHANGED')
        self.assertFalse(mod.receipts.receipt_path(
            self.state, self.entry['Path']).exists())

    def test_changed_local_file_during_remote_read_never_certify(self):
        def mutate(remote, target, deadline):
            if not self.reads:
                # A same-size concurrent local modification must not get proof.
                self.target.write_bytes(b'Z' * len(self.data))
            return self.whole(remote, target, deadline)
        with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
            outcome = mod.verify_one(self.entry, self.dest, self.state,
                                     time.monotonic() + 90,
                                     whole_reader=mutate,
                                     list_reader=self.current)
        self.assertEqual(outcome, 'DEFER_LOCAL_CHANGED')
        self.assertFalse(mod.receipts.receipt_path(
            self.state, self.entry['Path']).exists())

    def test_existing_valid_receipt_never_re_downloaded(self):
        mod.receipts.create_receipt(
            self.state, self.entry, hashlib.sha256(self.data).hexdigest())
        with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
            outcome = mod.verify_one(self.entry, self.dest, self.state,
                                     time.monotonic() + 90,
                                     whole_reader=self.whole,
                                     list_reader=self.current)
        self.assertEqual(outcome, 'SKIP_EXISTING_RECEIPT')
        self.assertEqual(self.reads, [])
        options, stats = mod.candidates([self.entry], self.dest, self.state)
        self.assertEqual(options, [])

    def test_candidates_only_existing_safe_regular_size_matching_non_native(self):
        rows = [self.entry, file('folder/missing.bin', 5),
                file('folder/wrong.bin', 999),
                file('folder/native.pages', 0),
                file('folder/over.bin', mod.pilot.MAX_PILOT_BYTES + 1),
                file('folder/dupe.bin', 4),
                file('folder/dupe.bin', 4)]
        (self.dest / 'folder' / 'wrong.bin').write_bytes(b'abc')
        (self.dest / 'folder' / 'dupe.bin').write_bytes(b'dupe')
        opts, stats = mod.candidates(rows, self.dest, self.state)
        self.assertEqual([e['Path'] for e in opts], [self.entry['Path']])
        self.assertEqual(stats['size_mismatch'], 1)
        self.assertEqual(stats['missing'], 1)
        self.assertEqual(stats['excluded'], 4)

    def test_preview_only_uses_local_complete_inventory(self):
        mod.inventory.save(self.state, [self.entry])
        stdout = io.StringIO()
        with (patch.object(mod.archive, 'STATE', self.state),
              patch.object(mod.archive, 'SOURCES',
                           {'drive': ('icloud:', self.dest)}),
              patch.object(mod.diagnosis, 'backup_busy',
                           side_effect=AssertionError('network forbidden')),
              patch.object(mod.inventory, 'list_one_folder',
                           side_effect=AssertionError('network forbidden')),
              contextlib.redirect_stdout(stdout)):
            self.assertEqual(mod.run(execute=False), 0)
        self.assertIn('1 existing files needing first receipt',
                      stdout.getvalue())
        self.assertIn('no remote calls', stdout.getvalue())
        self.assertEqual(list(self.state.glob('drive-verification-receipts/**/*')), [])

    def test_partial_inventory_blocks_execute_and_preview_without_remote(self):
        mod.inventory.save_progress(self.state, {
            'version': 1, 'source': 'icloud:',
            'finished': [''], 'pending': ['folder'],
            'entries': [{'Path': 'folder', 'IsDir': True}]})
        stdout = io.StringIO()
        with (patch.object(mod.archive, 'STATE', self.state),
              patch.object(mod.diagnosis, 'backup_busy') as busy,
              patch.object(mod.inventory, 'list_one_folder') as listing,
              contextlib.redirect_stdout(stdout)):
            self.assertEqual(mod.run(execute=False), 0)
            self.assertEqual(mod.run(execute=True), 2)
        busy.assert_not_called()
        listing.assert_not_called()
        self.assertIn('complete recent inventory required',
                      stdout.getvalue())

    def test_execute_limited_to_one_existing_file_even_with_multiple(self):
        another = file('folder/another.bin', size=32)
        (self.dest / another['Path']).write_bytes(b'a' * 32)
        mod.inventory.save(self.state, [self.entry, another])
        called = []
        def mock_verify(entry, *_args, **_kwargs):
            called.append(entry['Path'])
            return 'REVERIFIED_RECEIPTED'
        stdout = io.StringIO()
        with (patch.object(mod.archive, 'ROOT', self.root),
              patch.object(mod.archive, 'STATE', self.state),
              patch.object(mod.archive, 'SOURCES',
                           {'drive': ('icloud:', self.dest)}),
              patch.object(mod.archive, 'safe_directory'),
              patch.object(mod.diagnosis, 'backup_busy', return_value=False),
              patch.object(mod, 'verify_one', side_effect=mock_verify),
              contextlib.redirect_stdout(stdout)):
            self.assertEqual(mod.run(execute=True), 0)
        self.assertEqual(called, [self.entry['Path']])
        self.assertIn('REVERIFIED_RECEIPTED', stdout.getvalue())

    def test_unexpected_remote_error_does_not_log_private_filename(self):
        mod.inventory.save(self.state, [self.entry])
        stdout = io.StringIO()
        with (patch.object(mod.archive, 'ROOT', self.root),
              patch.object(mod.archive, 'STATE', self.state),
              patch.object(mod.archive, 'SOURCES',
                           {'drive': ('icloud:', self.dest)}),
              patch.object(mod.archive, 'safe_directory'),
              patch.object(mod.diagnosis, 'backup_busy', return_value=False),
              patch.object(mod, 'verify_one',
                           side_effect=mod.receipts.ReceiptError('secret filename')),
              contextlib.redirect_stdout(stdout)):
            self.assertEqual(mod.run(execute=True), 0)
        self.assertIn('DEFER_VERIFICATION_ERROR', stdout.getvalue())
        self.assertNotIn('secret filename', stdout.getvalue())
        self.assertNotIn(self.entry['Path'], stdout.getvalue())


if __name__ == '__main__':
    unittest.main()

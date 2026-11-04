import contextlib
import importlib.util
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'audit_drive_archive.py'
spec = importlib.util.spec_from_file_location('audit_drive_archive', SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def file(path: str, size: int | None, ident: str = 'id') -> dict:
    return {'Path': path, 'Size': size, 'ID': ident, 'ModTime': 'same',
            'IsDir': False}


class AuditDriveArchiveTests(unittest.TestCase):
    def test_partial_inventory_sizes_present_missing_and_mismatch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'archive'
            archive.mkdir()
            (archive / 'matched.bin').write_bytes(b'abcdef')
            (archive / 'wrong.bin').write_bytes(b'wrong')
            entries = [file('matched.bin', 6), file('wrong.bin', 9),
                       file('absent.bin', 10),
                       {'Path': 'Folder', 'IsDir': True}]
            mod.inventory.save_progress(root, {
                'version': 1, 'source': 'icloud:',
                'finished': [''], 'pending': ['Folder'],
                'entries': entries})
            output = mod.report(state_root=root, destination=archive)
            self.assertIn('PARTIAL_SOURCE_CHECKPOINT', output)
            self.assertIn('Size-matching existing files (NOT independently verified): 1',
                          output)
            self.assertIn('Existing files with incorrect size: 1', output)
            self.assertIn('Destination files missing: 1', output)
            self.assertIn('Bytes of size-matching files: 6', output)
            self.assertNotIn('matched.bin', output)
            self.assertNotIn('wrong.bin', output)

    def test_completed_inventory_still_never_calls_size_match_verified(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'archive'
            dest.mkdir()
            (dest / 'known.bin').write_bytes(b'Z' * 16)
            mod.inventory.save(root, [file('known.bin', 16)])
            output = mod.report(state_root=root, destination=dest)
            self.assertIn('COMPLETE_SOURCE_SNAPSHOT', output)
            self.assertIn('Size alone is not a cryptographic verification receipt.',
                          output)
            self.assertNotIn('PUBLISHED_VERIFIED', output)
            self.assertNotIn('known.bin', output)

    def test_symlink_files_and_parents_are_never_treated_as_backed_up(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'archive'
            dest.mkdir()
            external = root / 'external'
            external.mkdir()
            (external / 'secret.bin').write_bytes(b'xyz')
            (dest / 'link.bin').symlink_to(external / 'secret.bin')
            (dest / 'fake').symlink_to(external, target_is_directory=True)
            output = mod.summarize([
                file('link.bin', 3), file('fake/secret.bin', 3)], dest)
            self.assertEqual(output['size_matching_unverified'] if
                             'size_matching_unverified' in output else 0, 0)
            self.assertEqual(output['unsafe_or_inaccessible'], 2)
            self.assertEqual((external / 'secret.bin').read_bytes(), b'xyz')

    def test_duplicate_path_never_counts_as_a_regular_archived_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'archive'
            dest.mkdir()
            (dest / 'same.bin').write_bytes(b'xx')
            output = mod.summarize([
                file('same.bin', 2, 'a'), file('same.bin', 2, 'b')], dest)
            self.assertEqual(output['ambiguous'], 2)
            self.assertEqual(output.get('size_matching_unverified', 0), 0)

    def test_zero_byte_target_is_size_match_not_verified(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'archive'
            dest.mkdir()
            (dest / 'empty.bin').touch()
            counts = mod.summarize([file('empty.bin', 0)], dest)
            self.assertEqual(counts['size_matching_unverified'], 1)
            self.assertEqual(counts['bytes_size_matching'], 0)

    def test_main_uses_local_mount_check_and_never_network_downloads(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'archive'
            dest.mkdir()
            mod.inventory.save(root, [file('missing.bin', 5)])
            output = io.StringIO()
            with (patch.object(mod.archive, 'STATE', root),
                  patch.object(mod.archive, 'SOURCES',
                               {'drive': ('icloud:', dest)}),
                  patch.object(mod.archive, 'safe_directory') as mount_check,
                  patch.object(mod.inventory, 'list_one_folder',
                               side_effect=AssertionError('network listing forbidden')),
                  patch.object(mod.diagnosis, 'backup_busy',
                               side_effect=AssertionError('remote busy check forbidden')),
                  contextlib.redirect_stdout(output)):
                self.assertEqual(mod.main(), 0)
            mount_check.assert_called_once_with(dest)
            self.assertIn('Destination files missing: 1', output.getvalue())
            self.assertFalse((dest / 'missing.bin').exists())

    def test_no_inventory_returns_cautious_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'archive'
            dest.mkdir()
            text = mod.report(state_root=root, destination=dest)
            self.assertIn('NO_INVENTORY', text)
            self.assertNotIn('COMPLETE_SOURCE_SNAPSHOT', text)

    def test_invalid_root_fails_closed_without_touching_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'not-created'
            mod.inventory.save(root, [file('secret.bin', 4)])
            with self.assertRaisesRegex(mod.AuditError, 'INVALID_ARCHIVE_ROOT'):
                mod.report(state_root=root, destination=dest)


if __name__ == '__main__':
    unittest.main()

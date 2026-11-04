import contextlib
import importlib.util
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / 'scripts' / 'report_drive_coverage.py'
spec = importlib.util.spec_from_file_location('report_drive_coverage', SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def file(path, size, identifier='id'):
    return {'Path': path, 'Size': size, 'ID': identifier,
            'ModTime': 'stable', 'IsDir': False}


class DriveCoverageTests(unittest.TestCase):
    def test_categories_are_correct_and_do_not_double_count_native(self):
        entries = [
            {'Path': 'PrivateFolder', 'IsDir': True},
            file('secret_small.txt', 3),
            file('zero.txt', 0),
            file('private_eligible.bin', 200000),
            file('secret_large.dat', 20 * mod.MIB),
            file('huge.iso', 2 * mod.GIB),
            file('document.pages', -1),
            file('secret_duplicate.dat', 200000, 'id1'),
            file('secret_duplicate.dat', 200000, 'id2')]
        data = mod.summarize(entries)
        self.assertEqual(data['directories'], 1)
        self.assertEqual(data['files'], 8)
        self.assertEqual(data['unknown_size'], 1)
        self.assertEqual(data['apple_native'], 1)
        self.assertEqual(data['ambiguous'], 2)
        self.assertEqual(data['eligible_files'], 1)
        self.assertEqual(data['eligible_bytes'], 200000)
        self.assertEqual(data['under_64k'], 1)
        self.assertEqual(data['empty'], 1)
        self.assertEqual(data['8m_to_64m'], 1)
        self.assertEqual(data['over_1g'], 1)
        self.assertEqual(data['64k_to_8m'], 3)

    def test_partial_checkpoint_status_does_not_contact_apple(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            progress = {
                'version': 1, 'source': 'icloud:',
                'pending': ['Desktop'], 'finished': [''],
                'entries': [{'Path': 'Desktop', 'IsDir': True},
                            file('SecretFile.pdf', 300000)]}
            mod.inventory.save_progress(root, progress)
            with (patch.object(mod.inventory, 'list_one_folder',
                               side_effect=AssertionError('remote listing forbidden')),
                  patch.object(mod.diagnosis.subprocess, 'run',
                               side_effect=AssertionError('systemd forbidden'))):
                output = mod.report(state_root=root)
            self.assertIn('PARTIAL_CHECKPOINT', output)
            self.assertIn('1 complete; 1 pending', output)
            self.assertIn('potential 64 KiB–8 MiB ordinary unique files: 1', output)
            self.assertNotIn('SecretFile', output)

    def test_completed_inventory_status(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mod.inventory.save(root, [
                file('ordinary.txt', 400000),
                file('another.numbers', 99999)])
            output = mod.report(state_root=root)
            self.assertIn('COMPLETE_SNAPSHOT', output)
            self.assertIn('Apple-native records (overlapping category): 1', output)
            self.assertIn('ordinary unique files: 1', output)
            self.assertNotIn('ordinary.txt', output)
            self.assertNotIn('another.numbers', output)

    def test_no_inventory_reports_gracefully(self):
        with TemporaryDirectory() as directory:
            self.assertIn('NO_INVENTORY', mod.report(state_root=Path(directory)))

    def test_malformed_manifest_fails_without_leaking_private_names(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mod.inventory.save(root, [file('secret-passport.pdf', 300000)])
            path = mod.inventory.inventory_path(root)
            path.write_text('malformed json secret-passport')
            output = io.StringIO()
            with (patch.object(mod.archive, 'STATE', root),
                  contextlib.redirect_stderr(output)):
                self.assertEqual(mod.main(), 2)
            self.assertNotIn('secret-passport', output.getvalue())
            self.assertIn('invalid or inaccessible', output.getvalue())

    def test_unknown_size_and_small_native_excluded_from_pilot(self):
        data = mod.summarize([
            file('unknown.bin', None),
            file('native.pages', 200000),
            file('small.txt', 1000),
            file('valid.bin', 300000)])
        self.assertEqual(data['eligible_files'], 1)
        self.assertEqual(data['unknown_size'], 1)
        self.assertEqual(data['apple_native'], 1)


if __name__ == '__main__':
    unittest.main()

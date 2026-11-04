import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / 'scripts' / 'drive_inventory.py'
spec = importlib.util.spec_from_file_location('drive_inventory', SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def file(path='hello.pdf', size=12345, identifier='id-1'):
    return {'Path': path, 'Size': size, 'ID': identifier,
            'ModTime': '2026-11-04T00:00:00Z', 'IsDir': False}


class DriveInventoryTests(unittest.TestCase):
    def test_stores_private_inventory_and_counts_only_known_file_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            entries = [file(), file('native.pages', -1, 'id-2'),
                       {'Path': 'nested', 'IsDir': True}]
            stats = mod.save(root, entries)
            stored, loaded = mod.load(root)
            self.assertEqual(stored, entries)
            self.assertEqual(loaded, stats)
            self.assertEqual(stats['known_bytes'], 12345)
            self.assertEqual(stats['known_files'], 1)
            self.assertEqual(stats['unknown_files'], 1)
            self.assertEqual(stats['directories'], 1)
            self.assertEqual(stats['count'], 3)
            self.assertFalse(mod.inventory_path(root).stat().st_mode & 0o077)

    def test_duplicate_paths_are_preserved_and_counted_not_guessed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            records = [file('same.txt', 10, 'first'),
                       file('same.txt', 20, 'second'),
                       file('unique.mov', 30, 'third')]
            stats = mod.save(root, records)
            self.assertEqual((stats['collision_paths'],
                              stats['collision_records']), (1, 2))
            self.assertEqual(len(mod.load(root)[0]), 3)

    def test_unsafe_paths_are_rejected_before_any_state_written(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ('../out', '/root', 'x//y', 'a\nname', 'x/./y',
                         'x/../y'):
                with self.subTest(path=path):
                    with self.assertRaises(mod.InventoryError):
                        mod.save(root, [file(path)])
            self.assertFalse(root.joinpath(mod.NAME).exists())

    def test_invalid_negative_sizes_and_boolean_sizes_fail(self):
        for size in (True, -2, '1', 4.2):
            with self.subTest(size=size):
                with self.assertRaises(mod.InventoryError):
                    mod.inspect_entries([file(size=size)])
        self.assertEqual(mod.inspect_entries([file(size=None)])
                         ['unknown_files'], 1)

    def test_missing_prior_assets_never_replace_private_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = [file(), file('other.txt', 12, 'second')]
            mod.save(root, baseline, now=1000)
            original = mod.inventory_path(root).read_bytes()
            with self.assertRaisesRegex(mod.InventoryError,
                                        'PREVIOUSLY_KNOWN_RECORDS_MISSING'):
                mod.save(root, baseline[:1], now=1001)
            self.assertEqual(mod.inventory_path(root).read_bytes(), original)

    def test_failed_refresh_keeps_prior_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mod.save(root, [file()])
            before = mod.inventory_path(root).read_bytes()
            with (patch.object(mod, 'list_one_folder',
                               side_effect=mod.InventoryError('FOLDER_LIST_FAILED')),
                  patch.object(mod.diagnosis, 'backup_busy', return_value=False)):
                with self.assertRaisesRegex(mod.InventoryError, 'FOLDER_LIST_FAILED'):
                    mod.refresh(root)
            self.assertEqual(mod.inventory_path(root).read_bytes(), before)

    def test_refresh_saves_metadata_without_calling_any_media_downloader(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            entries = [file(), file('second.pdf', 222, 'id-2')]
            with (patch.object(mod, 'list_one_folder',
                               return_value=entries) as loader,
                  patch.object(mod.diagnosis, 'backup_busy', return_value=False),
                  patch.object(mod.archive, 'stream_range') as downloader):
                complete, stats = mod.refresh(root)
            self.assertTrue(complete)
            loader.assert_called_once()
            downloader.assert_not_called()
            self.assertEqual(stats['known_bytes'], 12567)

    def test_private_file_not_group_readable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mod.save(root, [file()])
            os.chmod(mod.inventory_path(root), 0o644)
            with self.assertRaisesRegex(mod.InventoryError, 'PERMISSIONS'):
                mod.load(root)

    def test_source_mismatch_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mod.save(root, [file()])
            path = mod.inventory_path(root)
            data = json.loads(path.read_text())
            data['source'] = 'other:'
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(mod.InventoryError, 'MISMATCH'):
                mod.load(root)

    def test_preview_uses_only_local_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mod.save(root, [file()])
            output = io.StringIO()
            with (patch.object(mod.archive, 'STATE', root),
                  patch.object(mod.subprocess, 'run',
                               side_effect=AssertionError('network not allowed')),
                  contextlib.redirect_stdout(output)):
                self.assertEqual(mod.run(execute=False), 0)
            self.assertIn('no network call', output.getvalue())
            self.assertNotIn('hello.pdf', output.getvalue())

    def test_refresh_run_obeys_backup_and_apple_lock(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'drive'
            archive.mkdir()
            state = root / 'state'
            result = subprocess.CompletedProcess([], 0, 'icloud:\n', '')
            output = io.StringIO()
            with (patch.object(mod.archive, 'ROOT', root),
                  patch.object(mod.archive, 'STATE', state),
                  patch.object(mod.archive, 'SOURCES',
                               {'drive': ('icloud:', archive)}),
                  patch.object(mod.archive, 'safe_directory'),
                  patch.object(mod.diagnosis, 'backup_busy',
                               return_value=False),
                  patch.object(mod.subprocess, 'run', return_value=result),
                  patch.object(mod, 'refresh',
                               return_value=(True, mod.inspect_entries([file()]))),
                  contextlib.redirect_stdout(output)):
                self.assertEqual(mod.run(execute=True), 0)
            self.assertTrue((root / '.apple-sync.lock').exists())
            self.assertIn('saved privately', output.getvalue())

    def test_folder_walk_checkpoint_resumes_without_relisting_completed_folders(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            visited = []
            def first_pass(folder, timeout):
                visited.append(folder)
                if folder == '':
                    return [
                        {'Path': 'Desktop', 'IsDir': True, 'Size': -1},
                        {'Path': 'Documents', 'IsDir': True, 'Size': -1},
                        file('root.txt', 9, 'root')]
                if folder == 'Desktop':
                    return [file('first.pdf', 100, 'first')]
                raise mod.InventoryDeferred()
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                done, progress = mod.walk_resumable(root, reader=first_pass)
            self.assertFalse(done)
            self.assertEqual(visited, ['', 'Desktop', 'Documents'])
            self.assertEqual(progress['finished'], ['', 'Desktop'])
            self.assertEqual(progress['pending'], ['Documents'])
            self.assertEqual(len(progress['entries']), 4)
            self.assertFalse(mod.inventory_path(root).exists())
            checkpoint = mod.load_progress(root)
            self.assertEqual(checkpoint['pending'], ['Documents'])
            self.assertFalse(mod.progress_path(root).stat().st_mode & 0o077)

            visited.clear()
            def second_pass(folder, timeout):
                visited.append(folder)
                self.assertEqual(folder, 'Documents')
                return [file('second.txt', 50, 'second')]
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                done, stats = mod.walk_resumable(root, reader=second_pass)
            self.assertTrue(done)
            self.assertEqual(visited, ['Documents'])
            self.assertEqual(stats['count'], 5)
            self.assertEqual(stats['known_bytes'], 159)
            self.assertIsNone(mod.load_progress(root))
            paths = [item['Path'] for item in mod.load(root)[0]]
            self.assertIn('Desktop/first.pdf', paths)
            self.assertIn('Documents/second.txt', paths)

    def test_checkpoint_retained_if_folder_listing_fails(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            def reader(folder, timeout):
                if folder == '':
                    return [{'Path': 'Desktop', 'IsDir': True},
                            file('root.txt', 123, 'root')]
                raise mod.InventoryError('FOLDER_LIST_FAILED')
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                with self.assertRaisesRegex(mod.InventoryError, 'FOLDER_LIST_FAILED'):
                    mod.walk_resumable(root, reader=reader)
            snapshot = mod.load_progress(root)
            self.assertEqual(snapshot['finished'], [''])
            self.assertEqual(snapshot['pending'], ['Desktop'])
            self.assertFalse(mod.inventory_path(root).exists())

    def test_bad_folder_result_never_commits_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(mod.diagnosis, 'backup_busy', return_value=False),
                  self.assertRaisesRegex(mod.InventoryError, 'UNSAFE_OR_RECURSIVE')):
                mod.walk_resumable(root, reader=lambda *_a, **_k: [
                    file('../escape.txt', 10)])
            checkpoint = mod.load_progress(root)
            self.assertEqual(checkpoint['finished'], [])
            self.assertEqual(checkpoint['pending'], [''])
            self.assertFalse(mod.inventory_path(root).exists())

    def test_duplicate_file_paths_preserved_but_ambiguous_folders_rejected(self):
        raw, subdirs = mod.rebase_folder('Desktop',
                                         [file('same.pdf', 9, 'one'),
                                          file('same.pdf', 10, 'two')])
        self.assertEqual(len(raw), 2)
        self.assertEqual([x['Path'] for x in raw],
                         ['Desktop/same.pdf', 'Desktop/same.pdf'])
        self.assertEqual(subdirs, [])
        with self.assertRaisesRegex(mod.InventoryError,
                                    'AMBIGUOUS_DIRECTORY_PATH'):
            mod.rebase_folder('',
                              [{'Path': 'Same', 'IsDir': True},
                               {'Path': 'Same', 'IsDir': True}])

    def test_invalid_checkpoint_is_never_used(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mod.save_progress(root, {'version': 1, 'source': mod.SOURCE,
                                     'pending': [''], 'finished': [],
                                     'entries': []})
            path = mod.progress_path(root)
            data = json.loads(path.read_text())
            data['pending'] = ['../escape']
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(mod.InventoryError,
                                        'INVALID_SAVED_PROGRESS'):
                mod.load_progress(root)

    def test_status_missing_complete_snapshot_reports_pending_not_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mod.save_progress(root, {'version': 1, 'source': mod.SOURCE,
                                     'pending': ['Desktop'], 'finished': [''],
                                     'entries': [file('root.txt')]})
            output = io.StringIO()
            with (patch.object(mod.archive, 'STATE', root),
                  patch.object(mod.subprocess, 'run',
                               side_effect=AssertionError('network not allowed')),
                  contextlib.redirect_stdout(output)):
                self.assertEqual(mod.run(execute=False), 0)
            self.assertIn('NO_COMPLETE_SNAPSHOT', output.getvalue())
            self.assertIn('1 folders completed', output.getvalue())
            self.assertIn('1 pending', output.getvalue())
            self.assertNotIn('root.txt', output.getvalue())

    def test_folder_reader_is_nonrecursive_and_preserves_private_names(self):
        with TemporaryDirectory() as directory:
            original = subprocess.Popen
            calls = []
            def fake(command, **kwargs):
                calls.append(command)
                return original([sys.executable, '-c',
                                 'import sys;sys.stdout.write("[]")'],
                                **kwargs)
            with (patch.object(mod.subprocess, 'Popen', side_effect=fake),
                  patch.object(mod.diagnosis, 'backup_busy',
                               return_value=False)):
                self.assertEqual(mod.list_one_folder('Desktop', timeout=10), [])
            self.assertEqual(calls[0][:3], ['rclone', 'lsjson', 'icloud:Desktop'])
            self.assertNotIn('--recursive', calls[0])

    def test_remote_listing_is_recursive_and_size_capped(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = subprocess.Popen
            calls = []
            def pretend_rclone(command, **kwargs):
                calls.append(command)
                return original([sys.executable, '-c',
                                 'import sys;sys.stdout.write("[]")'],
                                **kwargs)
            with (patch.object(mod.subprocess, 'Popen', side_effect=pretend_rclone),
                  patch.object(mod.diagnosis, 'backup_busy', return_value=False)):
                result = mod.fetch_listing_to_file(root, timeout=10)
            self.assertEqual(result, [])
            self.assertEqual(calls[0][:4],
                             ['rclone', 'lsjson', 'icloud:', '--recursive'])
            self.assertEqual(list(root.iterdir()), [])

    def test_remote_oversize_response_aborts_and_cleans_partial(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = subprocess.Popen
            def fake_process(command, **kwargs):
                return original(
                    [sys.executable, '-c',
                     'import sys;sys.stdout.write("a"*1024)'], **kwargs)
            with (patch.object(mod.subprocess, 'Popen',
                               side_effect=fake_process),
                  patch.object(mod.diagnosis, 'backup_busy', return_value=False),
                  patch.object(mod, 'MAX_OUTPUT_BYTES', 100)):
                with self.assertRaisesRegex(mod.InventoryError,
                                            'LISTING_TOO_LARGE'):
                    mod.fetch_listing_to_file(root, timeout=10)
            self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()

"""Offline regression coverage for private All Photos inventory and cache-only batches."""
import importlib.util
import json
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import subprocess

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import photos_inventory as inventory

spec = importlib.util.spec_from_file_location('archive_batch_inventory', SCRIPTS / 'archive_batch.py')
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)

SOURCE = 'icloudphotos:PrimarySync/All Photos'


def entry(name='a.HEIC', size=123):
    return {'Path': name, 'Size': size, 'ModTime': '2026-11-03T00:00:00Z',
            'ID': name, 'IsDir': False}


class PhotosInventoryTests(unittest.TestCase):
    def test_private_atomic_inventory_and_known_media_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'state'
            count, total = inventory.save(root, SOURCE,
                                          [entry('a.HEIC', 123), entry('b.MOV', 456)],
                                          now=1000)
            self.assertEqual((count, total), (2, 579))
            self.assertEqual(inventory.load(root, SOURCE, now=1001)[1], 579)
            self.assertEqual((root / inventory.NAME).stat().st_mode & 0o777, 0o600)

    def test_stale_or_wrong_remote_never_used(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory.save(root, SOURCE, [entry()], now=1000)
            with self.assertRaisesRegex(RuntimeError, 'older than'):
                inventory.load(root, SOURCE, now=1000 + inventory.MAX_AGE + 1)
            with self.assertRaisesRegex(RuntimeError, 'remote'):
                inventory.load(root, 'other:', now=1001)

    def test_incomplete_refresh_cannot_replace_saved_listing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = [entry('a.HEIC'), entry('b.HEIC')]
            inventory.save(root, SOURCE, original, now=1000)
            with self.assertRaisesRegex(RuntimeError, 'omitted previously known'):
                inventory.save(root, SOURCE, [original[0]], now=1001)
            self.assertEqual(inventory.load(root, SOURCE, now=1002)[0], original)

    def test_duplicate_and_invalid_entries_are_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for items in ([entry('duplicate'), entry('duplicate')],
                          [entry('../escape')],
                          [entry('unknown', -1)],
                          []):
                with self.subTest(items=items):
                    with self.assertRaises(RuntimeError):
                        inventory.save(root, SOURCE, items)
            self.assertFalse((root / inventory.NAME).exists())

    def test_refresh_failure_keeps_previous_inventory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory.save(root, SOURCE, [entry()], now=1000)
            runner = lambda *a, **kw: subprocess.CompletedProcess([], 1, '', 'failure')
            with self.assertRaisesRegex(RuntimeError, 'failed'):
                inventory.refresh(root, SOURCE, runner=runner)
            self.assertEqual(len(inventory.load(root, SOURCE, now=1001)[0]), 1)

    def test_successful_metadata_refresh_saves_sizes_not_media(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            listing = [entry('photo.HEIC', 1024), entry('clip.MOV', 1048576)]
            commands = []
            def fake_run(cmd, **kwargs):
                commands.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, json.dumps(listing), '')
            self.assertEqual(inventory.refresh(root, SOURCE, runner=fake_run),
                             (2, 1049600))
            self.assertIn('lsjson', commands[0])
            self.assertNotIn('copy', commands[0])
            self.assertNotIn('cat', commands[0])
            self.assertEqual([x['Path'] for x in inventory.load(root, SOURCE)[0]],
                             ['photo.HEIC', 'clip.MOV'])

    def test_archive_preview_uses_snapshot_without_remote_list(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'archive'
            dest.mkdir()
            state = root / 'state'
            inventory.save(state, SOURCE, [entry('first.HEIC', 4)])
            def no_remote(*args, **kwargs):
                raise AssertionError('Full album must never be remotely listed by batch')
            with patch.object(batch.subprocess, 'run', side_effect=no_remote):
                self.assertEqual(batch.preview('photos', SOURCE, dest, state,
                                               execute=False), 0)

    def test_cache_refresh_mode_is_not_a_media_download(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / '.apple-sync-state'
            with patch.object(batch, 'ROOT', root), patch.object(batch, 'STATE', state), \
                    patch.object(batch, 'safe_directory'), \
                    patch.object(batch.subprocess, 'run',
                                 return_value=subprocess.CompletedProcess(
                                     [], 0, 'icloudphotos:\n', '')), \
                    patch.object(batch.photos_inventory, 'refresh',
                                 return_value=(42, 123456)) as refreshed:
                self.assertEqual(batch.run('photos-inventory', True), 0)
            refreshed.assert_called_once_with(state, SOURCE)


if __name__ == '__main__':
    unittest.main()

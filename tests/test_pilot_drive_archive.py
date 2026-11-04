import hashlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'pilot_drive_archive.py'
spec = importlib.util.spec_from_file_location('pilot_drive_archive', SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class DrivePilotTests(unittest.TestCase):
    def setUp(self):
        self.data = bytes(range(256)) * 512
        self.entry = {'Path': 'small.bin', 'Size': len(self.data),
                      'ID': 'file-id', 'ModTime': 'stable',
                      'IsDir': False}
        self.reads = 0

    def whole(self, _remote, path, _deadline):
        self.reads += 1
        path.write_bytes(self.data)
        return len(self.data), hashlib.sha256(self.data).hexdigest()

    def test_candidates_exclude_apple_packages_and_unsafe_paths(self):
        entries = [
            self.entry,
            {'Path': 'document.pages', 'Size': 80000, 'IsDir': False},
            {'Path': 'document.numbers', 'Size': 80000, 'IsDir': False},
            {'Path': 'document.key', 'Size': 80000, 'IsDir': False},
            {'Path': '../unsafe', 'Size': 80000, 'IsDir': False},
            {'Path': 'bad-size', 'Size': True, 'IsDir': False},
            {'Path': 'directory', 'Size': 80000, 'IsDir': True}]
        self.assertEqual([e['Path'] for e in mod.eligible(entries)],
                         [self.entry['Path']])

    def test_publishes_once_to_proper_path_and_keeps_no_verifier_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest, state = root / 'drive', root / 'state'
            dest.mkdir()
            state.mkdir()
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                result = mod.publish_verified(
                    'icloud:Downloads', self.entry, dest, state,
                    mod.time.monotonic() + 90,
                    whole_reader=self.whole, list_reader=lambda *_a: [self.entry])
            self.assertEqual(result, 'PUBLISHED_VERIFIED')
            self.assertEqual((dest / 'Downloads' / 'small.bin').read_bytes(),
                             self.data)
            self.assertEqual(self.reads, 2)
            self.assertEqual(list(state.iterdir()), [])

    def test_existing_file_is_never_overwritten_or_read(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest, state = root / 'drive', root / 'state'
            (dest / 'Downloads').mkdir(parents=True)
            state.mkdir()
            target = dest / 'Downloads' / 'small.bin'
            target.write_bytes(b'original')
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                result = mod.publish_verified(
                    'icloud:Downloads', self.entry, dest, state,
                    mod.time.monotonic() + 90,
                    whole_reader=self.whole, list_reader=lambda *_a: [self.entry])
            self.assertEqual(result, 'SKIP_EXISTING')
            self.assertEqual(target.read_bytes(), b'original')
            self.assertEqual(self.reads, 0)

    def test_source_identity_mutation_prevents_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest, state = root / 'drive', root / 'state'
            dest.mkdir()
            state.mkdir()
            changed = {**self.entry, 'ModTime': 'changed'}
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                result = mod.publish_verified(
                    'icloud:Downloads', self.entry, dest, state,
                    mod.time.monotonic() + 90,
                    whole_reader=self.whole, list_reader=lambda *_a: [changed])
            self.assertEqual(result, 'DEFER_SOURCE_CHANGED')
            self.assertFalse((dest / 'Downloads' / 'small.bin').exists())
            self.assertEqual(list(state.iterdir()), [])

    def test_listing_duplicate_path_prevents_publication(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest, state = root / 'drive', root / 'state'
            dest.mkdir()
            state.mkdir()
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                result = mod.publish_verified(
                    'icloud:Downloads', self.entry, dest, state,
                    mod.time.monotonic() + 90,
                    whole_reader=self.whole,
                    list_reader=lambda *_a: [self.entry, self.entry])
            self.assertEqual(result, 'DEFER_SOURCE_CHANGED')
            self.assertFalse((dest / 'Downloads' / 'small.bin').exists())

    def test_full_downloads_disagree_then_no_archive_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest, state = root / 'drive', root / 'state'
            dest.mkdir()
            state.mkdir()
            def different(remote, path, deadline):
                length, digest = self.whole(remote, path, deadline)
                if self.reads == 2:
                    path.write_bytes(b'Z' * length)
                    digest = hashlib.sha256(b'Z' * length).hexdigest()
                return length, digest
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                result = mod.publish_verified(
                    'icloud:Downloads', self.entry, dest, state,
                    mod.time.monotonic() + 90,
                    whole_reader=different, list_reader=lambda *_a: [self.entry])
            self.assertEqual(result, 'DEFER_WHOLE_MISMATCH')
            self.assertFalse((dest / 'Downloads' / 'small.bin').exists())

    def test_wrong_file_size_never_publishes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest, state = root / 'drive', root / 'state'
            dest.mkdir()
            state.mkdir()
            listed = {**self.entry, 'Size': self.entry['Size'] + 1}
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                result = mod.publish_verified(
                    'icloud:Downloads', listed, dest, state,
                    mod.time.monotonic() + 90,
                    whole_reader=self.whole, list_reader=lambda *_a: [listed])
            self.assertEqual(result, 'DEFER_SIZE_MISMATCH')
            self.assertFalse((dest / 'Downloads' / 'small.bin').exists())

    def test_staging_fails_closed_on_symlink_target(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest, state = root / 'drive', root / 'state'
            (dest / 'Downloads').mkdir(parents=True)
            state.mkdir()
            (dest / 'Downloads' / 'small.bin').symlink_to(root / 'missing')
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                with self.assertRaises(RuntimeError):
                    mod.publish_verified('icloud:Downloads', self.entry,
                                         dest, state, mod.time.monotonic() + 90,
                                         whole_reader=self.whole,
                                         list_reader=lambda *_a: [self.entry])
            self.assertEqual(self.reads, 0)

    def test_preview_does_not_create_state_or_archive_file(self):
        import subprocess
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'drive'
            dest.mkdir()
            state = root / 'state'
            class Response:
                stdout = 'inactive\n'
                returncode = 0
            with (patch.object(mod.archive, 'STATE', state),
                  patch.object(mod.archive, 'ROOT', root),
                  patch.object(mod.archive, 'SOURCES',
                               {'drive': ('icloud:', dest)}),
                  patch.object(mod.archive, 'safe_directory'),
                  patch.object(mod.diagnosis, 'backup_busy', return_value=False),
                  patch.object(mod, 'listing', return_value=[self.entry]),
                  patch.object(mod, 'publish_verified') as publish):
                self.assertEqual(mod.run(execute=False), 0)
            self.assertFalse(state.exists())
            self.assertEqual(list(dest.iterdir()), [])
            publish.assert_not_called()

    def test_busy_state_defer_before_listing(self):
        with (patch.object(mod.diagnosis, 'backup_busy', return_value=True),
              patch.object(mod, 'listing') as listing):
            self.assertEqual(mod.run(execute=True), 0)
        listing.assert_not_called()

    def test_identity_excludes_ambiguous_paths(self):
        self.assertTrue(mod.still_same(self.entry, [self.entry]))
        self.assertFalse(mod.still_same(self.entry, [self.entry, self.entry]))
        self.assertFalse(mod.still_same(self.entry,
                                       [{**self.entry, 'ID': 'changed'}]))


if __name__ == '__main__':
    unittest.main()

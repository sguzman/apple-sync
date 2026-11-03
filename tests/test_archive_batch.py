import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

SRC = Path(__file__).parents[1] / 'scripts' / 'archive_batch.py'
spec = importlib.util.spec_from_file_location('archive_batch', SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class ChunkTests(unittest.TestCase):
    def test_favorites_uses_independent_album_archive(self):
        remote, destination = mod.SOURCES['photos-favorites']
        self.assertEqual(remote, 'icloudphotos:PrimarySync/Favorites')
        self.assertEqual(destination, mod.ROOT / 'photos' / 'albums' / 'Favorites')
        self.assertNotEqual(destination, mod.SOURCES['photos'][1])
        self.assertNotEqual(
            mod.key_for('photos-favorites', 'sample.HEIC'),
            mod.key_for('photos', 'sample.HEIC'))

    def test_photo_progress_rotates_and_keeps_album_state_isolated(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            state.mkdir()
            selected = []

            def fake_preview(kind, source, destination, state_root, execute):
                self.assertTrue(execute)
                self.assertEqual(state_root, state)
                selected.append((kind, source, destination))
                return 0

            with patch.object(mod, 'STATE', state), \
                    patch.object(mod, 'safe_directory'), \
                    patch.object(mod, 'preview', side_effect=fake_preview):
                for _ in range(len(mod.PROGRESS_ALBUMS) + 1):
                    self.assertEqual(mod.run_progress_locked(True), 0)

            self.assertEqual(len(selected), len(mod.PROGRESS_ALBUMS) + 1)
            self.assertEqual(selected[0], selected[-1])
            self.assertEqual([x[1].split('/')[-1] for x in selected[:-1]],
                             list(mod.PROGRESS_ALBUMS))
            self.assertEqual(json.loads((state / mod.PROGRESS_CURSOR).read_text())['next'], 1)
            self.assertNotIn('Hidden', mod.PROGRESS_ALBUMS)
            self.assertNotIn('Recently Deleted', mod.PROGRESS_ALBUMS)
            self.assertNotIn('All Photos', mod.PROGRESS_ALBUMS)
            self.assertEqual(len({x[2] for x in selected[:-1]}), len(mod.PROGRESS_ALBUMS))

    def test_photo_progress_preview_does_not_write_cursor(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / 'missing'
            with patch.object(mod, 'STATE', state), \
                    patch.object(mod, 'safe_directory'), \
                    patch.object(mod, 'preview', return_value=0) as preview:
                self.assertEqual(mod.run_progress_locked(False), 0)
            self.assertFalse(state.exists())
            preview.assert_called_once()
            self.assertFalse(preview.call_args.kwargs['execute'])

    def test_photo_progress_timeout_moves_cursor_for_next_attempt(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            with patch.object(mod, 'STATE', state), \
                    patch.object(mod, 'safe_directory'), \
                    patch.object(mod, 'preview', side_effect=RuntimeError('listing timed out')):
                with self.assertRaisesRegex(RuntimeError, 'listing timed out'):
                    mod.run_progress_locked(True)
            self.assertEqual(json.loads((state / mod.PROGRESS_CURSOR).read_text())['next'], 1)

    def test_photo_progress_rejects_invalid_cursor(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            (state / mod.PROGRESS_CURSOR).write_text('{"version":1,"next":999}')
            with patch.object(mod, 'STATE', state):
                with self.assertRaisesRegex(RuntimeError, 'Invalid Photos progress cursor'):
                    mod.run_progress_locked(False)

    def test_path_safety(self):
        for bad in ('../evil', '/absolute', 'x//y', 'x/./y', 'x/../y', '', 'a\npath'):
            self.assertIsNone(mod.safe_path(bad))
        self.assertEqual(str(mod.safe_path('a/b.HEIC')), 'a/b.HEIC')

    def test_resume_identity_and_completed_chunk(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            part, meta = mod.state_paths(root, 'photos', 'some.mp4')
            part.parent.mkdir(parents=True)
            entry = {'Path': 'some.mp4', 'Size': 100, 'ModTime': 'stable'}
            s = mod.new_state(entry)
            part.write_bytes(b'')
            mod.write_json_atomic(meta, s)
            chunk = root / 'chunk'
            chunk.write_bytes(b'hello')
            import hashlib
            mod.commit_chunk(part, meta, s, chunk, 0, 5,
                             hashlib.sha256(b'hello').hexdigest())
            reread = mod.read_state(part, meta, entry)
            self.assertEqual(reread['committed'], 5)
            self.assertEqual(part.read_bytes(), b'hello')
            with part.open('ab') as out:
                out.write(b'garbage')
            reread = mod.read_state(part, meta, entry)
            self.assertEqual(reread['committed'], 5)
            self.assertEqual(part.read_bytes(), b'hello')
            with self.assertRaises(RuntimeError):
                mod.read_state(part, meta, {**entry, 'Size': 101})

    def test_drive_execution_blocked_until_ranges_verified(self):
        with self.assertRaisesRegex(RuntimeError, 'range downloads are not verified'):
            mod.run('drive', execute=True)

    def test_dry_run_does_not_truncate_uncommitted_tail(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            part, meta = mod.state_paths(root, 'drive', 'example.bin')
            part.parent.mkdir(parents=True)
            entry = {'Path': 'example.bin', 'Size': 100, 'ModTime': 'stable'}
            state = mod.new_state(entry)
            part.write_bytes(b'uncommitted bytes')
            mod.write_json_atomic(meta, state)
            with self.assertRaises(RuntimeError):
                mod.read_state(part, meta, entry, repair=False)
            self.assertEqual(part.read_bytes(), b'uncommitted bytes')
            self.assertEqual(json.loads(meta.read_text())['committed'], 0)

    def test_candidate_keeps_large_and_prioritizes_partial(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / 'archive'
            destination.mkdir()
            state = root / 'state'
            entry = {'Path': 'z.mp4', 'Size': mod.MAX_BYTES * 30}
            other = {'Path': 'a.jpg', 'Size': 200}
            part, meta = mod.state_paths(state, 'photos', 'z.mp4')
            part.parent.mkdir(parents=True)
            s = mod.new_state(entry)
            part.write_bytes(b'x')
            import hashlib
            s.update(committed=1, last_start=0, last_length=1,
                     last_sha256=hashlib.sha256(b'x').hexdigest())
            mod.write_json_atomic(meta, s)
            todo = mod.candidates([other, entry], destination, state, 'photos')
            self.assertEqual([x['rel'] for x in todo], ['z.mp4', 'a.jpg'])

    def test_atomic_no_overwrite(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'dest'
            dest.mkdir()
            part = root / 'staging.part'
            meta = root / 'staging.json'
            part.write_bytes(b'new')
            meta.write_text('{}')
            (dest / 'test.mp4').write_bytes(b'old')
            mod.finish(part, meta, dest, Path('test.mp4'), 3)
            self.assertEqual((dest / 'test.mp4').read_bytes(), b'old')
            self.assertTrue(part.exists())
            (dest / 'test.mp4').unlink()
            mod.finish(part, meta, dest, Path('test.mp4'), 3)
            self.assertEqual((dest / 'test.mp4').read_bytes(), b'new')
            self.assertFalse(part.exists())


if __name__ == '__main__':
    unittest.main()

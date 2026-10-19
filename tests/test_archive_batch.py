import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

SRC = Path(__file__).parents[1] / 'scripts' / 'archive_batch.py'
spec = importlib.util.spec_from_file_location('archive_batch', SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class ChunkTests(unittest.TestCase):
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

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / 'scripts' / 'verify_drive_staging.py'
spec = importlib.util.spec_from_file_location('verify_drive_staging', SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class DriveStagingTests(unittest.TestCase):
    def setUp(self):
        self.data = bytes(range(256)) * 1024

    def reader(self, _remote, offset, count, path, _remaining):
        payload = self.data[offset:offset + count]
        path.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    def whole(self, _remote, path, _deadline):
        path.write_bytes(self.data)
        return len(self.data), hashlib.sha256(self.data).hexdigest()

    def test_candidates_are_bounded_and_paths_safe(self):
        records = [
            {'Path': '../unsafe', 'Size': 900000, 'IsDir': False},
            {'Path': 'folder', 'Size': 900000, 'IsDir': True},
            {'Path': 'tiny', 'Size': 3, 'IsDir': False},
            {'Path': 'bool', 'Size': True, 'IsDir': False},
            {'Path': 'huge', 'Size': mod.MAX_FILE_BYTES + 1, 'IsDir': False},
            {'Path': 'small.pdf', 'Size': 100000, 'IsDir': False},
            {'Path': 'large.pdf', 'Size': 3000000, 'IsDir': False}]
        self.assertEqual([e['Path'] for e in mod.candidates(records)],
                         ['large.pdf', 'small.pdf'])

    def test_reconstruct_exercises_real_commit_checkpoint_and_publish(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                published = mod.reconstruct('icloud:secret', len(self.data), root,
                                            time.monotonic() + 90, reader=self.reader)
            self.assertEqual(published.read_bytes(), self.data)
            self.assertTrue(published.name == 'sample.bin')
            part, state = mod.archive.state_paths(
                root / 'state', 'drive-test', 'sample.bin')
            self.assertFalse(part.exists())
            self.assertFalse(state.exists())

    def test_full_and_staged_copies_match(self):
        with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
            status = mod.verify('icloud:private', len(self.data),
                                time.monotonic() + 90,
                                reader=self.reader, whole_reader=self.whole)
        self.assertEqual(status, 'STAGING_MATCHES_BOTH_WHOLE_READS')

    def test_mismatched_advertised_size_not_approved(self):
        with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
            status = mod.verify('icloud:private', len(self.data) + 10,
                                time.monotonic() + 90,
                                reader=self.reader, whole_reader=self.whole)
        self.assertEqual(status, 'WHOLE_SIZE_MISMATCH')

    def test_conflicting_full_downloads_not_approved(self):
        calls = 0
        def variable(_remote, path, _deadline):
            nonlocal calls
            calls += 1
            content = self.data if calls == 1 else self.data[:-1] + b'X'
            path.write_bytes(content)
            return len(content), hashlib.sha256(content).hexdigest()
        with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
            status = mod.verify('icloud:private', len(self.data),
                                time.monotonic() + 90,
                                reader=self.reader, whole_reader=variable)
        self.assertEqual(status, 'WHOLE_COPIES_DISAGREE')

    def test_chunk_contents_wrong_even_if_length_and_hash_per_chunk_pass(self):
        def faulty(_remote, offset, count, path, _remaining):
            payload = b'Z' * count
            path.write_bytes(payload)
            return hashlib.sha256(payload).hexdigest()
        with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
            status = mod.verify('icloud:private', len(self.data),
                                time.monotonic() + 90,
                                reader=faulty, whole_reader=self.whole)
        self.assertEqual(status, 'STAGING_DIFFERS_FROM_WHOLE')

    def test_repeated_failed_chunks_never_publish(self):
        def fails(_remote, offset, count, path, _remaining):
            path.write_bytes(b'x')
            raise RuntimeError('short')
        with (TemporaryDirectory() as directory,
              patch.object(mod.diagnosis, 'backup_busy', return_value=False),
              patch.object(mod.archive.time, 'sleep', return_value=None)):
            root = Path(directory)
            with self.assertRaisesRegex(mod.VerificationError,
                                        'RANGE_CHUNK_FAILED'):
                mod.reconstruct('icloud:private', len(self.data), root,
                                time.monotonic() + 90, reader=fails)
            self.assertFalse((root / 'published').exists())

    def test_defers_without_ever_touching_remote_if_backup_active(self):
        output = io.StringIO()
        with (patch.object(mod.diagnosis, 'backup_busy', return_value=True),
              patch.object(mod.diagnosis, 'run_rclone') as remote,
              contextlib.redirect_stdout(output)):
            self.assertEqual(mod.run(), 0)
        remote.assert_not_called()
        self.assertIn('DEFER', output.getvalue())

    def test_private_output_for_success_and_failure(self):
        records = [
            {'Path': 'secret-first.pdf', 'Size': len(self.data), 'IsDir': False},
            {'Path': 'secret-second.pages', 'Size': len(self.data), 'IsDir': False}]
        seen = []
        def verify(remote, size, deadline):
            seen.append(remote)
            return ('STAGING_MATCHES_BOTH_WHOLE_READS'
                    if len(seen) == 1 else 'WHOLE_SIZE_MISMATCH')
        out = io.StringIO()
        with (patch.object(mod.diagnosis, 'backup_busy', return_value=False),
              patch.object(mod.diagnosis, 'SCOPES', ('icloud:Desktop',)),
              patch.object(mod.diagnosis, 'run_rclone',
                           return_value=json.dumps(records).encode()),
              patch.object(mod, 'verify', side_effect=verify),
              contextlib.redirect_stdout(out)):
            self.assertEqual(mod.run(), 0)
        self.assertEqual(len(seen), 2)
        self.assertIn('1 reconstructed', out.getvalue())
        self.assertNotIn('secret-first', out.getvalue())
        self.assertNotIn('secret-second', out.getvalue())
        self.assertIn('archive remains BLOCKED', out.getvalue())

    def test_whole_read_accepts_explicit_higher_limit_but_still_bounds_output(self):
        import hashlib
        import subprocess
        import sys
        original_popen = subprocess.Popen
        body = b'a' * 3072
        def fake_popen(_cmd, **kwargs):
            return original_popen(
                [sys.executable, '-c',
                 "import sys;sys.stdout.buffer.write(b'a'*3072)"],
                **kwargs)
        with (TemporaryDirectory() as directory,
              patch.object(mod.diagnosis, 'backup_busy', return_value=False),
              patch.object(mod.subprocess, 'Popen', side_effect=fake_popen),
              patch.object(mod, 'MAX_FILE_BYTES', 1024)):
            root = Path(directory)
            with self.assertRaisesRegex(mod.VerificationError, 'WHOLE_OVERSIZE'):
                mod.whole_file('icloud:private', root / 'too_large',
                               time.monotonic() + 20)
            size, digest = mod.whole_file(
                'icloud:private', root / 'allowed',
                time.monotonic() + 20, max_file_bytes=4096)
            self.assertEqual((size, digest),
                             (3072, hashlib.sha256(body).hexdigest()))
            with self.assertRaisesRegex(mod.VerificationError, 'WHOLE_OVERSIZE'):
                mod.whole_file('icloud:private', root / 'still_too_large',
                               time.monotonic() + 20, max_file_bytes=2048)

    def test_invalid_whole_read_limit_fails_before_remote_access(self):
        with patch.object(mod.diagnosis, 'backup_busy') as busy:
            with self.assertRaisesRegex(mod.VerificationError,
                                        'INVALID_WHOLE_FILE_LIMIT'):
                mod.whole_file('icloud:private', Path('/ignored'),
                               time.monotonic() + 30, max_file_bytes=0)
            busy.assert_not_called()

    def test_whole_read_enforces_maximum_source_bytes(self):
        import subprocess
        import sys
        real_popen = subprocess.Popen
        def fake_popen(_cmd, **kwargs):
            return real_popen([sys.executable, '-c',
                               "import sys;sys.stdout.buffer.write(b'a'*8192)"],
                              **kwargs)
        with (TemporaryDirectory() as directory,
              patch.object(mod.diagnosis, 'backup_busy', return_value=False),
              patch.object(mod.subprocess, 'Popen', side_effect=fake_popen),
              patch.object(mod, 'MAX_FILE_BYTES', 1024)):
            with self.assertRaisesRegex(mod.VerificationError, 'WHOLE_OVERSIZE'):
                mod.whole_file('icloud:private', Path(directory) / 'whole',
                               time.monotonic() + 20)


if __name__ == '__main__':
    unittest.main()

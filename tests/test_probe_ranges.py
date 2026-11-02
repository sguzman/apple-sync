import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / 'scripts' / 'probe_ranges.py'
spec = importlib.util.spec_from_file_location('probe_ranges', SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class ProbeTests(unittest.TestCase):
    def test_rejects_untrusted_paths_and_size_values(self):
        entries = [
            {'Path': '../bad', 'Size': 10},
            {'Path': 'bad//path', 'Size': 100},
            {'Path': 'fine.bin', 'Size': True},
            {'Path': 'huge.mov', 'Size': mod.MAX_FILE_BYTES + 1},
            {'Path': 'a.bin', 'Size': 7},
            {'Path': 'b.bin', 'Size': 3},
        ]
        self.assertEqual([e['Path'] for e in mod.candidates(entries)],
                         ['b.bin', 'a.bin'])
        for value in ('/absolute', 'x/../y', 'x//y', 'a\nfile'):
            self.assertFalse(mod.valid_path(value))

    def test_probe_checks_overlap_without_leaking_name(self):
        file_data = b'abcdefghi'
        listing = b'[{"Path":"private-filename.jpg","Size":9}]'
        calls = []

        def fake_run(args, seconds):
            calls.append((args, seconds))
            if args[0] == 'lsjson':
                return subprocess.CompletedProcess(args, 0, listing, b'')
            if args[0] == 'cat':
                offset = int(args[args.index('--offset') + 1])
                count = int(args[args.index('--count') + 1])
                return subprocess.CompletedProcess(
                    args, 0, file_data[offset:offset + count], b'')
            self.fail('unexpected subprocess')

        with patch.object(mod, 'run_rclone', fake_run):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = mod.probe('photos')
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 3)
        self.assertNotIn('private-filename', output.getvalue())
        self.assertIn('PASS', output.getvalue())

    def test_probe_rejects_ignored_offset(self):
        listing = b'[{"Path":"asset.jpg","Size":9}]'

        def fake_run(args, seconds):
            if args[0] == 'lsjson':
                return subprocess.CompletedProcess(args, 0, listing, b'')
            # Simulate a backend that ignores --offset=1.
            return subprocess.CompletedProcess(args, 0, b'abcdefgh', b'')

        with patch.object(mod, 'run_rclone', fake_run):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = mod.probe('photos')
        self.assertEqual(code, 2)
        self.assertIn('FAIL', out.getvalue())

    def test_no_suitable_source_is_inconclusive(self):
        def fake_run(args, seconds):
            return subprocess.CompletedProcess(args, 0, b'[]', b'')

        with patch.object(mod, 'run_rclone', fake_run):
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.redirect_stderr(io.StringIO()):
                    code = mod.probe('drive')
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()

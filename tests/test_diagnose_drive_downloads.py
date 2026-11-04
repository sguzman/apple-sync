import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / 'scripts' / 'diagnose_drive_downloads.py'
spec = importlib.util.spec_from_file_location('diagnose_drive_downloads', SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class DriveDownloadDiagnosisTests(unittest.TestCase):
    def test_ordinary_whole_and_ranges_agree(self):
        content = bytes(range(256)) * 20
        n = mod.SAMPLE_BYTES
        self.assertEqual(mod.inspect(content, content, content[:n],
                                     content[1:n + 1], content[:n],
                                     len(content)),
                         ('WHOLE_STABLE_MATCHES_SIZE', 'RANGE_AGREES_WITH_WHOLE'))

    def test_range_offset_ignored(self):
        content = bytes(range(256)) * 20
        part = content[:mod.SAMPLE_BYTES]
        self.assertEqual(mod.inspect(content, content, part, part, part,
                                     len(content))[1],
                         'RANGE_OFFSET_IGNORED_OR_STALE')

    def test_overlap_mismatch_with_stable_whole(self):
        content = bytes(range(256)) * 20
        n = mod.SAMPLE_BYTES
        bad = bytes(reversed(content[1:n + 1]))
        self.assertEqual(mod.inspect(content, content, content[:n], bad,
                                     content[:n], len(content))[1],
                         'RANGE_OVERLAP_MISMATCH')

    def test_repeated_whole_downloads_detect_change(self):
        first = bytes(range(256)) * 20
        changed = first[:-1] + b'Q'
        n = mod.SAMPLE_BYTES
        whole, ranges = mod.inspect(first, changed, first[:n],
                                    first[1:n + 1], first[:n], len(first))
        self.assertEqual(whole, 'WHOLE_DOWNLOADS_DISAGREE')
        self.assertEqual(ranges, 'RANGE_AGREES_WITH_WHOLE')

    def test_whole_size_mismatch_not_approved(self):
        content = bytes(range(256)) * 20
        n = mod.SAMPLE_BYTES
        self.assertEqual(mod.inspect(content, content, content[:n],
                                     content[1:n + 1], content[:n],
                                     len(content) + 20)[0],
                         'WHOLE_SIZE_DIFFERS_FROM_LISTING')

    def test_wrong_length_and_unstable_offsets(self):
        content = bytes(range(256)) * 20
        n = mod.SAMPLE_BYTES
        self.assertEqual(mod.inspect(content, content, content[:n],
                                     content[1:n], content[:n],
                                     len(content))[1], 'RANGE_WRONG_LENGTH')
        self.assertEqual(mod.inspect(content, content, content[:n],
                                     content[1:n + 1], b'X' * n,
                                     len(content))[1],
                         'RANGE_SAME_OFFSET_UNSTABLE')

    def test_reject_unsafe_paths_and_bad_sizes(self):
        entries = [
            {'Path': '../private', 'Size': 5000, 'IsDir': False},
            {'Path': 'link', 'Size': 5000, 'IsDir': True},
            {'Path': 'boolean', 'Size': True, 'IsDir': False},
            {'Path': 'tiny', 'Size': 2, 'IsDir': False},
            {'Path': 'large', 'Size': mod.MAX_FILE_BYTES + 1, 'IsDir': False},
            {'Path': 'normal.pdf', 'Size': 5000, 'IsDir': False},
            {'Path': 'native.pages', 'Size': 6000, 'IsDir': False}]
        self.assertEqual([x['Path'] for x in mod.select_files(entries)],
                         ['normal.pdf', 'native.pages'])

    def test_subprocess_stdout_is_bounded_even_if_backend_sends_too_much(self):
        import subprocess
        import sys
        actual_popen = subprocess.Popen
        def fake_spawn(_arguments, **kwargs):
            return actual_popen([sys.executable, '-c',
                                 "import sys; sys.stdout.buffer.write(b'x' * 1024)"],
                                **kwargs)
        with (patch.object(mod.subprocess, 'Popen', side_effect=fake_spawn),
              patch.object(mod, 'MAX_DOWNLOAD_OUTPUT', 16)):
            with self.assertRaisesRegex(mod.ReadFailure, 'OUTPUT_TOO_LARGE'):
                mod.run_rclone(['cat', 'icloud:some-private-name'], 20)

    def test_read_error_does_not_expose_remote_name(self):
        import time
        with patch.object(mod, 'run_rclone',
                          side_effect=mod.ReadFailure('RCLONE_READ_ERROR')):
            self.assertEqual(
                mod.examine('icloud:Desktop/private-financial-document.pdf',
                            10000, time.monotonic() + 90),
                ('INCONCLUSIVE_READ_ERROR', 'RCLONE_READ_ERROR'))

    def test_examine_reads_two_full_files_and_three_overlapping_ranges(self):
        content = bytes(range(256)) * 20
        calls = []
        def reader(args, seconds):
            calls.append(args)
            if '--offset' not in args:
                return content
            offset = int(args[args.index('--offset') + 1])
            count = int(args[args.index('--count') + 1])
            return content[offset:offset + count]
        with patch.object(mod, 'run_rclone', side_effect=reader):
            whole, ranges = mod.examine('icloud:Desktop/private.pdf',
                                         len(content), mod.time.monotonic() + 100)
        self.assertEqual(whole, 'WHOLE_STABLE_MATCHES_SIZE')
        self.assertEqual(ranges, 'RANGE_AGREES_WITH_WHOLE')
        self.assertEqual(len(calls), 5)
        self.assertTrue(all('--offset' not in args for args in calls[:2]))
        self.assertEqual([int(args[args.index('--offset') + 1])
                          for args in calls[2:]], [0, 1, 0])

    def test_diagnosis_output_does_not_leak_private_names_or_bytes(self):
        source = [{'Path': 'secret-document.pdf', 'Size': 5120, 'IsDir': False}]
        content = bytes(range(256)) * 20
        def fake_rclone(args, _seconds):
            if args[0] == 'lsjson':
                return json.dumps(source).encode('utf-8')
            offset = int(args[args.index('--offset') + 1]) if '--offset' in args else 0
            count = int(args[args.index('--count') + 1]) if '--count' in args else len(content)
            return content[offset:offset + count]
        output = io.StringIO()
        with (patch.object(mod, 'SCOPES', ('icloud:Desktop', 'icloud:Downloads')),
              patch.object(mod, 'run_rclone', side_effect=fake_rclone),
              contextlib.redirect_stdout(output)):
            result = mod.diagnose()
        self.assertEqual(result, 0)
        self.assertIn('2 fully consistent', output.getvalue())
        self.assertIn('Drive archive remains BLOCKED', output.getvalue())
        self.assertNotIn('secret-document', output.getvalue())


if __name__ == '__main__':
    unittest.main()

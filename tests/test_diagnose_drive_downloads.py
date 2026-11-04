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

    def test_extended_uses_16k_ranges_and_checks_middle(self):
        content = bytes(range(256)) * 96
        calls = []
        def reader(args, seconds):
            calls.append(args)
            if '--offset' not in args:
                return content
            offset = int(args[args.index('--offset') + 1])
            count = int(args[args.index('--count') + 1])
            return content[offset:offset + count]
        with patch.object(mod, 'run_rclone', side_effect=reader):
            result = mod.examine('icloud:Desktop/private.bin', len(content),
                                 mod.time.monotonic() + 100, extended=True)
        self.assertEqual(result, ('WHOLE_STABLE_MATCHES_SIZE',
                                  'RANGE_AGREES_WITH_WHOLE'))
        self.assertEqual(len(calls), 6)
        self.assertEqual([int(args[args.index('--count') + 1])
                          for args in calls[2:5]], [16384, 16384, 16384])
        self.assertGreater(int(calls[5][calls[5].index('--offset') + 1]), 1)

    def test_extended_detects_wrong_middle_even_when_prefix_works(self):
        content = bytes(range(256)) * 96
        def reader(args, seconds):
            if '--offset' not in args:
                return content
            offset = int(args[args.index('--offset') + 1])
            count = int(args[args.index('--count') + 1])
            if offset > 1:
                return b'X' * count
            return content[offset:offset + count]
        with patch.object(mod, 'run_rclone', side_effect=reader):
            result = mod.examine('icloud:Desktop/private.bin', len(content),
                                 mod.time.monotonic() + 100, extended=True)
        self.assertEqual(result, ('WHOLE_STABLE_MATCHES_SIZE',
                                  'RANGE_MIDDLE_DISAGREES_WITH_WHOLE'))

    def test_varied_samples_include_small_large_and_iwork(self):
        entries = [
            {'Path': 'small.pdf', 'Size': 5000},
            {'Path': 'medium.pdf', 'Size': 15000},
            {'Path': 'large.pdf', 'Size': 500000},
            {'Path': 'document.pages', 'Size': 10000},
            {'Path': 'other.pdf', 'Size': 9000}]
        selected = mod.varied_files(entries, 4)
        names = [item['Path'] for item in selected]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn('small.pdf', names)
        self.assertIn('large.pdf', names)
        self.assertIn('document.pages', names)

    def test_busy_checker_includes_all_photo_workers(self):
        import subprocess
        names = []
        def status(cmd, **_kwargs):
            names.append(cmd[4])
            return subprocess.CompletedProcess(cmd, 0, 'inactive\\n', '')
        with patch.object(mod.subprocess, 'run', side_effect=status):
            self.assertFalse(mod.backup_busy())
        self.assertEqual(tuple(names), mod.BUSY_UNITS)
        self.assertIn('apple-sync-photos-progress.service', names)
        self.assertIn('apple-sync-photos-inventory.service', names)

    def test_wait_idle_resumes_when_all_services_become_idle(self):
        output = io.StringIO()
        with (patch.object(mod, 'backup_busy', side_effect=[True, True, False]),
              patch.object(mod.time, 'sleep') as sleeper,
              contextlib.redirect_stdout(output)):
            self.assertTrue(mod.wait_for_idle(seconds=45))
        self.assertEqual(sleeper.call_count, 2)
        self.assertIn('waiting for a safe idle', output.getvalue())

    def test_wait_idle_expires_without_any_drive_reads(self):
        output = io.StringIO()
        with (patch.object(mod, 'backup_busy', return_value=True),
              patch.object(mod.time, 'monotonic', side_effect=[0, 3]),
              patch.object(mod.time, 'sleep') as sleeper,
              contextlib.redirect_stdout(output)):
            self.assertFalse(mod.wait_for_idle(seconds=2))
        sleeper.assert_not_called()
        self.assertIn('no idle window', output.getvalue())

    def test_main_wait_idle_deferral_does_not_run_diagnosis(self):
        with (patch.object(mod, 'wait_for_idle', return_value=False),
              patch.object(mod, 'diagnose') as diagnostic,
              patch.object(mod, 'argparse') as parser):
            # Exercise the CLI with the real parser while mocking only work.
            parser.ArgumentParser.return_value.parse_args.return_value.extended = True
            parser.ArgumentParser.return_value.parse_args.return_value.wait_idle = True
            self.assertEqual(mod.main(), 0)
        diagnostic.assert_not_called()

    def test_extended_defers_before_any_remote_reads_if_backup_busy(self):
        output = io.StringIO()
        with (patch.object(mod, 'backup_busy', return_value=True),
              patch.object(mod, 'run_rclone') as reader,
              contextlib.redirect_stdout(output)):
            result = mod.diagnose(extended=True)
        self.assertEqual(result, 0)
        reader.assert_not_called()
        self.assertIn('DEFER', output.getvalue())

    def test_extended_diagnostic_handles_multiple_samples_privately(self):
        content = bytes(range(256)) * 96
        entries = [{'Path': 'private-a.pdf', 'Size': len(content), 'IsDir': False},
                   {'Path': 'private-b.pdf', 'Size': len(content), 'IsDir': False}]
        def reader(args, seconds):
            if args[0] == 'lsjson':
                return json.dumps(entries).encode()
            offset = int(args[args.index('--offset') + 1]) if '--offset' in args else 0
            count = int(args[args.index('--count') + 1]) if '--count' in args else len(content)
            return content[offset:offset + count]
        output = io.StringIO()
        with (patch.object(mod, 'SCOPES', ('icloud:Desktop',)),
              patch.object(mod, 'backup_busy', return_value=False),
              patch.object(mod, 'run_rclone', side_effect=reader),
              contextlib.redirect_stdout(output)):
            self.assertEqual(mod.diagnose(extended=True), 0)
        self.assertIn('2 fully consistent', output.getvalue())
        self.assertNotIn('private-a', output.getvalue())
        self.assertNotIn('private-b', output.getvalue())

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

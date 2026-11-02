import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).parents[1] / 'scripts' / 'diagnose_drive_ranges.py'
spec = importlib.util.spec_from_file_location('diagnose_drive_ranges', SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class DiagnosisTests(unittest.TestCase):
    def test_stable_overlapping_ranges_pass(self):
        first = b'abcdefghijklmnopqrstuvwxyz'
        shifted = b'bcdefghijklmnopqrstuvwxyz!'
        self.assertEqual(mod.classify(first, shifted, first), 'PASS')

    def test_detects_ignored_offsets(self):
        first = b'abcdefgh'
        self.assertEqual(mod.classify(first, first, first),
                         'OFFSET_IGNORED_OR_STALE_RESPONSE')

    def test_detects_inconsistent_reads(self):
        self.assertEqual(mod.classify(b'abcd', b'bcde', b'abce'),
                         'INCONSISTENT_SAME_OFFSET')

    def test_detects_unrelated_shifted_content(self):
        self.assertEqual(mod.classify(b'abcdefgh', b'zzzzzzzz', b'abcdefgh'),
                         'OFFSET_CONTENT_MISMATCH')

    def test_short_result_rejected(self):
        self.assertEqual(mod.classify(b'abcdef', b'bcde', b'abcdef'),
                         'WRONG_LENGTH')

    def test_filters_untrusted_paths_and_sizes(self):
        source = [
            {'Path': '../escape', 'Size': 5000},
            {'Path': 'normal', 'Size': 5000},
            {'Path': 'small', 'Size': 100},
            {'Path': 'boolean', 'Size': True},
            {'Path': 'oversize', 'Size': mod.MAX_FILE_BYTES + 1},
        ]
        self.assertEqual([item['Path'] for item in mod.select_files(source)],
                         ['normal', 'small'])


if __name__ == '__main__':
    unittest.main()

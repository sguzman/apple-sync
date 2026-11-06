import contextlib
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import drive_inventory as inventory
import drive_inventory_profile as profiler


def metadata(path, *, directory=False):
    return {'Path': path, 'Name': path.rsplit('/', 1)[-1],
            'IsDir': directory, 'Size': -1 if directory else 123,
            'ID': 'secret-document-id',
            'ModTime': '2026-11-06T01:02:03Z'}


class InventoryProfileTests(unittest.TestCase):
    def sample(self):
        return {'version': 1, 'source': inventory.SOURCE,
                'finished': ['', 'Desktop', 'Desktop/Nested', 'Empty'],
                'pending': ['Pending'],
                'entries': [
                    metadata('Desktop', directory=True),
                    metadata('Empty', directory=True),
                    metadata('Pending', directory=True),
                    metadata('secret-file.docx'),
                    metadata('Desktop/first.txt'),
                    metadata('Desktop/second.txt'),
                    metadata('Desktop/Nested', directory=True),
                    metadata('Desktop/Nested/one.txt'),
                ]}

    def test_counts_group_by_direct_parent_not_full_path(self):
        folders = profiler.count_folders(self.sample())
        self.assertEqual([v['records'] for v in folders], [4, 3, 1, 0])
        self.assertEqual([v['dirs'] for v in folders], [3, 1, 0, 0])
        self.assertEqual([v['depth'] for v in folders], [0, 1, 2, 1])

    def test_profile_window_metrics_identify_increased_record_density(self):
        groups = profiler.profile(self.sample(), [0, 2, 4])
        self.assertEqual(groups[0]['records'], 7)
        self.assertEqual(groups[1]['records'], 1)
        self.assertEqual(groups[0]['records_per_folder'], 3.5)
        self.assertEqual(groups[1]['records_per_folder'], 0.5)
        self.assertEqual(groups[1]['empty_folders'], 1)
        self.assertGreater(groups[0]['local_record_json_bytes'], 0)

    def test_privacy_summary_excludes_all_metadata_names(self):
        reports = profiler.profile(self.sample(), [0, 2, 4])
        rendered = json.dumps(reports)
        for secret in ('Desktop', 'Pending', 'secret-file', 'secret-document-id',
                       'first.txt', 'one.txt'):
            self.assertNotIn(secret, rendered)

    def test_rejects_records_not_under_finished_folder(self):
        progress = self.sample()
        progress['entries'].append(metadata('Pending/private.dat'))
        with self.assertRaisesRegex(
                inventory.InventoryError,
                'CHECKPOINT_ENTRY_PARENT_NOT_FINISHED'):
            profiler.count_folders(progress)

    def test_rejects_out_of_range_or_unsorted_cuts(self):
        progress = self.sample()
        for cuts in ([0, 5], [2, 2], [3, 2], [3], [-1, 2]):
            with self.subTest(cuts=cuts):
                with self.assertRaises(ValueError):
                    profiler.profile(progress, cuts)

    def test_read_only_entrypoint_never_calls_apple_or_writes_checkpoint(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            progress = self.sample()
            inventory.save_progress(root, progress)
            original = inventory.progress_path(root).read_bytes()
            output = io.StringIO()
            with (patch.object(profiler.archive, 'STATE', root),
                  patch.object(profiler.inventory.subprocess, 'Popen',
                               side_effect=AssertionError('network forbidden')),
                  patch('sys.argv', ['profile', '--cuts', '0', '2', '4']),
                  contextlib.redirect_stdout(output)):
                self.assertEqual(profiler.main(), 0)
            self.assertEqual(inventory.progress_path(root).read_bytes(),
                             original)
            self.assertIn('NO APPLE NETWORK', output.getvalue())
            self.assertNotIn('secret-file', output.getvalue())
            self.assertEqual(output.getvalue().count('LOCAL PROFILE {'), 2)


if __name__ == '__main__':
    unittest.main()

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import drive_inventory as inventory
import drive_inventory_rc as rc


def entry(path='Desktop/file.txt', *, size=123, directory=False):
    return {'Name': path.rsplit('/', 1)[-1], 'Path': path,
            'Size': size, 'IsDir': directory, 'ID': 'id-1',
            'ModTime': '2026-01-02T03:04:05Z'}


class RcloneRCInventoryTests(unittest.TestCase):
    def test_rebases_rc_root_paths_and_preserves_metadata(self):
        files = [entry(), entry('Desktop/Nested', size=-1, directory=True)]
        result = rc.normalize_folder_listing('Desktop', {'list': files})
        self.assertEqual([v['Path'] for v in result],
                         ['file.txt', 'Nested'])
        self.assertEqual(result[0]['ID'], 'id-1')
        self.assertEqual(result[0]['Size'], 123)
        self.assertEqual(result[0]['ModTime'], '2026-01-02T03:04:05Z')
        output, directories = inventory.rebase_folder('Desktop', result)
        self.assertEqual([v['Path'] for v in output],
                         ['Desktop/file.txt', 'Desktop/Nested'])
        self.assertEqual(directories, ['Desktop/Nested'])

    def test_root_listing_does_not_strip_names(self):
        items = [entry('Desktop', directory=True, size=-1),
                 entry('hello.txt')]
        result = rc.normalize_folder_listing('', {'list': items})
        self.assertEqual(result[0]['Path'], 'Desktop')
        self.assertEqual(result[1]['Path'], 'hello.txt')

    def test_empty_folder_is_valid_and_can_complete(self):
        self.assertEqual(rc.normalize_folder_listing('Desktop', {'list': []}), [])

    def test_outside_folder_and_recursive_items_are_rejected(self):
        for folder, path in (
            ('Desktop', 'Other/file.txt'),
            ('Desktop', 'Desktop'),
            ('Desktop', 'Desktop/nested/file.txt'),
            ('', 'nested/hello.txt'),
            ('Desktop', 'Desktop/../unsafe'),
            ('Desktop', 'Desktop/.'),
        ):
            with self.subTest(folder=folder, path=path):
                with self.assertRaises(rc.RcloneRCError):
                    rc.normalize_folder_listing(folder, {'list': [entry(path)]})

    def test_invalid_response_shapes_are_rejected(self):
        for value in (None, {}, {'list': None}, {'list': {}},
                      {'list': [1]}, {'list': [{'Path': 12}]}):
            with self.subTest(value=value):
                with self.assertRaises(rc.RcloneRCError):
                    rc.normalize_folder_listing('Desktop', value)

    def test_duplicate_file_records_preserved(self):
        a, b = entry(), entry()
        b['ID'] = 'id-2'
        output = rc.normalize_folder_listing('Desktop', {'list': [a, b]})
        self.assertEqual(len(output), 2)
        self.assertEqual([v['ID'] for v in output], ['id-1', 'id-2'])

    def test_reader_uses_nonrecursive_rc_metadata_only(self):
        response = {'list': [entry()]}
        seen = []
        real_popen = subprocess.Popen
        def fake_popen(command, **kwargs):
            seen.append(command)
            return real_popen([sys.executable, '-c',
                               'import sys;sys.stdout.write(' +
                               repr(json.dumps(response)) + ')'],
                              **kwargs)
        reader = rc.RcloneRCReader(Path('/ignored'))
        reader.socket = Path('/tmp/not-used-by-mock.sock')
        reader.daemon = Mock()
        reader.daemon.poll.return_value = None
        with (patch.object(rc.subprocess, 'Popen', side_effect=fake_popen),
              patch.object(rc.diagnosis, 'backup_busy', return_value=False)):
            records = reader('Desktop', timeout=5)
        self.assertEqual(records[0]['Path'], 'file.txt')
        self.assertEqual(seen[0][:5],
                         ['rclone', 'rc', '--unix-socket',
                          '/tmp/not-used-by-mock.sock', 'operations/list'])
        request = json.loads(seen[0][6])
        self.assertEqual(request['fs'], 'icloud:')
        self.assertEqual(request['remote'], 'Desktop')
        self.assertEqual(request['opt'], {'recurse': False})
        self.assertNotIn('cat', seen[0])
        self.assertNotIn('copy', seen[0])

    def test_oversize_rc_output_is_refused_before_checkpoint(self):
        reader = rc.RcloneRCReader(Path('/ignored'), max_output=24)
        reader.socket = Path('/tmp/not-used-by-mock.sock')
        reader.daemon = Mock()
        reader.daemon.poll.return_value = None
        real_popen = subprocess.Popen
        def fake_popen(_command, **kwargs):
            return real_popen(
                [sys.executable, '-c', 'print("X"*1000)'], **kwargs)
        with (patch.object(rc.subprocess, 'Popen', side_effect=fake_popen),
              patch.object(rc.diagnosis, 'backup_busy', return_value=False),
              self.assertRaisesRegex(rc.RcloneRCError, 'RC_RESPONSE_TOO_LARGE')):
            reader('', timeout=5)

    def test_busy_backup_defers_without_contacting_rclone(self):
        reader = rc.RcloneRCReader(Path('/ignored'))
        reader.socket = Path('/tmp/not-used-by-mock.sock')
        reader.daemon = Mock()
        reader.daemon.poll.return_value = None
        with (patch.object(rc.subprocess, 'Popen') as process,
              patch.object(rc.diagnosis, 'backup_busy', return_value=True),
              self.assertRaises(rc.RcloneRCDeferred)):
            reader('Desktop', timeout=5)
        process.assert_not_called()

    def test_refresh_default_does_not_start_rc(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(inventory, 'walk_resumable',
                               return_value=(False, {})) as walk,
                  patch.object(inventory.rc_reader, 'RcloneRCReader') as daemon):
                inventory.refresh(Path(directory))
        walk.assert_called_once()
        daemon.assert_not_called()

    def test_refresh_persistent_reader_commits_only_complete_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            class Reader:
                def __enter__(self):
                    return self
                def __exit__(self, *_a):
                    pass
                def __call__(self, folder, timeout):
                    if folder == '':
                        return [entry('Desktop', directory=True, size=-1)]
                    raise rc.RcloneRCDeferred()
            with (patch.object(inventory.rc_reader, 'RcloneRCReader',
                               return_value=Reader()),
                  patch.object(inventory.diagnosis, 'backup_busy',
                               return_value=False)):
                complete, partial = inventory.refresh(root, persistent_rc=True)
            self.assertFalse(complete)
            self.assertEqual(partial['finished'], [''])
            self.assertEqual(partial['pending'], ['Desktop'])
            self.assertEqual([v['Path'] for v in partial['entries']],
                             ['Desktop'])
            self.assertFalse(inventory.inventory_path(root).exists())

    def test_failed_rc_listing_keeps_checkpoint_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory.save_progress(root, {
                'version': 1, 'source': inventory.SOURCE,
                'pending': ['Desktop'], 'finished': [''],
                'entries': [entry('root.txt')],
            })
            before = inventory.progress_path(root).read_bytes()
            class Reader:
                def __enter__(self):
                    return self
                def __exit__(self, *_a):
                    pass
                def __call__(self, folder, timeout):
                    raise rc.RcloneRCError('RC_LISTING_FAILED')
            with (patch.object(inventory.rc_reader, 'RcloneRCReader',
                               return_value=Reader()),
                  patch.object(inventory.diagnosis, 'backup_busy',
                               return_value=False),
                  self.assertRaisesRegex(inventory.InventoryError,
                                         'RC_LISTING_FAILED')):
                inventory.refresh(root, persistent_rc=True)
            self.assertEqual(inventory.progress_path(root).read_bytes(),
                             before)

    def test_private_rc_socket_directory_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_dir = []
            class FakeDaemon:
                def poll(self):
                    return None
                def terminate(self):
                    pass
                def wait(self, timeout):
                    pass
            def fake_popen(command, **_kw):
                self.assertEqual(command[:2], ['rclone', 'rcd'])
                address = command[command.index('--rc-addr') + 1]
                self.assertTrue(address.startswith('unix://'))
                sock_path = Path(address[len('unix://'):])
                socket_dir.append(sock_path.parent)
                sock_path.touch()
                return FakeDaemon()
            with (patch.object(rc.subprocess, 'Popen', side_effect=fake_popen),
                  patch.object(Path, 'is_socket', return_value=True)):
                with rc.RcloneRCReader(root) as reader:
                    self.assertEqual(reader.socket.parent, socket_dir[0])
                    self.assertEqual(reader.socket.parent.stat().st_mode & 0o777,
                                     0o700)
                self.assertFalse(socket_dir[0].exists())


if __name__ == '__main__':
    unittest.main()

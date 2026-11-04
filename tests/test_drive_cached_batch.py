import contextlib
import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parents[1] / 'scripts' / 'drive_cached_batch.py'
spec = importlib.util.spec_from_file_location('drive_cached_batch', SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def file(path='folder/ordinary.bin', size=128 * 1024, identifier='id-1'):
    return {'Path': path, 'Size': size, 'IsDir': False, 'ID': identifier,
            'ModTime': 'stable'}


class CachedBatchTests(unittest.TestCase):
    def test_filters_ambiguous_native_unknown_oversize_and_existing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'folder').mkdir()
            (root / 'folder' / 'existing.bin').write_bytes(b'kept')
            rows = [file(), file('folder/same.bin', identifier='a'),
                    file('folder/same.bin', identifier='b'),
                    file('folder/huge.bin', mod.MAX_FILE_SIZE + 1),
                    file('folder/native.pages'),
                    file('folder/unknown.bin', -1),
                    file('folder/existing.bin'),
                    {'Path': 'folder', 'IsDir': True, 'Size': -1}]
            chosen, stats = mod.candidates(rows, root)
            self.assertEqual([row['Path'] for row in chosen],
                             ['folder/ordinary.bin'])
            self.assertEqual(stats['ambiguous'], 2)
            self.assertEqual(stats['existing'], 1)
            self.assertEqual(stats['excluded'], 3)
            self.assertEqual((root / 'folder' / 'existing.bin').read_bytes(),
                             b'kept')

    def test_tiny_nonempty_files_are_eligible_but_zero_length_is_excluded(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [file('one-byte.txt', 1),
                    file('two-bytes.txt', 2),
                    file('four-kib.txt', 4096),
                    file('zero.txt', 0),
                    file('empty.pages', 0)]
            chosen, stats = mod.candidates(rows, root)
            self.assertEqual([record['Path'] for record in chosen],
                             ['one-byte.txt', 'two-bytes.txt', 'four-kib.txt'])
            self.assertEqual(stats['excluded'], 2)

    def test_one_byte_whole_file_archive_preserves_exact_content(self):
        import hashlib
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'drive'
            state = root / 'state'
            dest.mkdir()
            state.mkdir()
            entry = file('tiny.bin', 1)
            reads = []
            def whole(remote, path, _deadline):
                reads.append(remote)
                path.write_bytes(b'Z')
                return 1, hashlib.sha256(b'Z').hexdigest()
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                result = mod.pilot.publish_verified(
                    'icloud:', entry, dest, state,
                    time.monotonic() + 90, whole_reader=whole,
                    list_reader=lambda *_args: [entry])
            self.assertEqual(result, 'PUBLISHED_VERIFIED')
            self.assertEqual(reads, ['icloud:tiny.bin', 'icloud:tiny.bin'])
            self.assertEqual((dest / 'tiny.bin').read_bytes(), b'Z')
            self.assertEqual(list(state.iterdir()), [])

    def test_changed_tiny_file_rejects_publication(self):
        import hashlib
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'drive'
            state = root / 'state'
            dest.mkdir()
            state.mkdir()
            entry = file('tiny.bin', 1)
            reads = 0
            def whole(remote, path, _deadline):
                nonlocal reads
                reads += 1
                body = b'A' if reads == 1 else b'B'
                path.write_bytes(body)
                return 1, hashlib.sha256(body).hexdigest()
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                result = mod.pilot.publish_verified(
                    'icloud:', entry, dest, state,
                    time.monotonic() + 90, whole_reader=whole,
                    list_reader=lambda *_args: [entry])
            self.assertEqual(result, 'DEFER_WHOLE_MISMATCH')
            self.assertFalse((dest / 'tiny.bin').exists())

    def test_large_profile_keeps_only_8_to_64_mib_ordinary_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [
                file('small.bin', mod.MAX_FILE_SIZE),
                file('large.bin', mod.MAX_FILE_SIZE + 1),
                file('huge.bin', mod.MAX_LARGE_FILE_SIZE + 1),
                file('maximum.bin', mod.MAX_LARGE_FILE_SIZE),
                file('native.pages', 16 * mod.MIB),
                file('ambiguous.bin', 12 * mod.MIB, 'a'),
                file('ambiguous.bin', 12 * mod.MIB, 'b'),
            ]
            candidates, stats = mod.candidates(
                rows, root,
                min_file_size=mod.MAX_FILE_SIZE + 1,
                max_file_size=mod.MAX_LARGE_FILE_SIZE)
            self.assertEqual([item['Path'] for item in candidates],
                             ['large.bin', 'maximum.bin'])
            self.assertEqual(stats['ambiguous'], 2)
            self.assertEqual(stats['excluded'], 3)

    def test_large_preview_never_uses_remote_or_archive_publisher(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive_root = root / 'drive'
            archive_root.mkdir()
            mod.inventory.save(root, [
                file('small.bin', 4096),
                file('large.bin', 12 * mod.MIB)])
            output = io.StringIO()
            with (patch.object(mod.archive, 'STATE', root),
                  patch.object(mod.archive, 'SOURCES',
                               {'drive': ('icloud:', archive_root)}),
                  patch.object(mod.pilot, 'publish_verified') as publish,
                  patch.object(mod.inventory, 'list_one_folder') as listing,
                  patch.object(mod.diagnosis, 'backup_busy') as busy,
                  contextlib.redirect_stdout(output)):
                self.assertEqual(mod.run(execute=False, large=True), 0)
            publish.assert_not_called()
            listing.assert_not_called()
            busy.assert_not_called()
            self.assertIn('(large)', output.getvalue())
            self.assertIn('1 eligible missing', output.getvalue())

    def test_large_pass_reserves_twice_size_and_at_most_one_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            dest = root / 'drive'
            dest.mkdir()
            mod.inventory.save(state, [
                file('large-one.bin', 9 * mod.MIB, 'first'),
                file('large-two.bin', 10 * mod.MIB, 'second'),
                file('large-three.bin', 11 * mod.MIB, 'third')])
            invoked = []
            def fake_publish(scope, entry, *_args, **kwargs):
                self.assertIsNotNone(kwargs.get('whole_reader'))
                invoked.append((scope, entry['Path'], entry['Size'],
                                kwargs['whole_reader']))
                return 'PUBLISHED_VERIFIED'
            output = io.StringIO()
            with (patch.object(mod.archive, 'ROOT', root),
                  patch.object(mod.archive, 'STATE', state),
                  patch.object(mod.archive, 'SOURCES',
                               {'drive': ('icloud:', dest)}),
                  patch.object(mod.archive, 'safe_directory'),
                  patch.object(mod.diagnosis, 'backup_busy', return_value=False),
                  patch.object(mod.pilot, 'publish_verified',
                               side_effect=fake_publish),
                  contextlib.redirect_stdout(output)):
                self.assertEqual(mod.run(execute=True, large=True), 0)
            self.assertEqual(len(invoked), 1)
            self.assertEqual(invoked[0][:3],
                             ('icloud:', 'large-one.bin', 9 * mod.MIB))
            with patch.object(mod.pilot.verification, 'whole_file',
                              return_value=(9 * mod.MIB, 'hash')) as reader:
                self.assertEqual(invoked[0][3]('remote', root / 'temp', 90),
                                 (9 * mod.MIB, 'hash'))
            reader.assert_called_once_with(
                'remote', root / 'temp', 90,
                max_file_bytes=mod.MAX_LARGE_FILE_SIZE)
            self.assertIn('1 verified files published', output.getvalue())
            self.assertIn('18874368 bytes', output.getvalue())

    def test_large_pass_denied_if_inventory_only_partially_checkpointed(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mod.inventory.save_progress(
                root, {'version': 1, 'source': 'icloud:',
                       'pending': [''], 'finished': [], 'entries': []})
            with (patch.object(mod.archive, 'STATE', root),
                  patch.object(mod.pilot, 'publish_verified') as publisher,
                  patch.object(mod.inventory, 'list_one_folder') as listing,
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(mod.run(execute=False, large=True), 0)
                self.assertEqual(mod.run(execute=True, large=True), 2)
            publisher.assert_not_called()
            listing.assert_not_called()

    def test_partial_inventory_fails_closed_without_any_remote_calls(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mod.inventory.save_progress(
                root, {'version': 1, 'source': 'icloud:',
                       'pending': [''], 'finished': [], 'entries': []})
            with (patch.object(mod.archive, 'STATE', root),
                  patch.object(mod.diagnosis, 'backup_busy') as remote_check,
                  patch.object(mod.pilot, 'publish_verified') as publisher,
                  patch.object(mod.inventory, 'list_one_folder') as listing,
                  contextlib.redirect_stdout(io.StringIO()) as output):
                self.assertEqual(mod.run(execute=False), 0)
                self.assertEqual(mod.run(execute=True), 2)
            remote_check.assert_not_called()
            publisher.assert_not_called()
            listing.assert_not_called()
            self.assertIn('complete and recent inventory', output.getvalue())

    def test_preview_local_only_and_cannot_download_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive_root = root / 'drive'
            archive_root.mkdir()
            mod.inventory.save(root, [file()])
            output = io.StringIO()
            with (patch.object(mod.archive, 'STATE', root),
                  patch.object(mod.archive, 'SOURCES',
                               {'drive': ('icloud:', archive_root)}),
                  patch.object(mod.pilot, 'publish_verified') as publish,
                  patch.object(mod.diagnosis, 'backup_busy') as busy,
                  contextlib.redirect_stdout(output)):
                self.assertEqual(mod.run(execute=False), 0)
            publish.assert_not_called()
            busy.assert_not_called()
            self.assertIn('1 eligible missing', output.getvalue())
            self.assertIn('no remote calls', output.getvalue())

    def test_folder_parts_prepare_root_and_nested_paths(self):
        root_scope, root_entry, folder = mod.folder_parts(file('root.bin'))
        self.assertEqual(root_scope, 'icloud:')
        self.assertEqual(root_entry['Path'], 'root.bin')
        self.assertEqual(folder, '')
        scope, entry, folder = mod.folder_parts(file('a/b/c.bin'))
        self.assertEqual((scope, entry['Path'], folder),
                         ('icloud:a/b', 'c.bin', 'a/b'))

    def test_shallow_recheck_never_recurses_or_enumerates_root(self):
        with patch.object(mod.inventory, 'list_one_folder',
                          return_value=[file('a.bin')]) as caller:
            self.assertEqual(mod.safe_recheck('icloud:Desktop',
                                               time.monotonic() + 90,
                                               'Desktop'),
                             [file('a.bin')])
        caller.assert_called_once()
        self.assertEqual(caller.call_args[0][0], 'Desktop')

    def test_execute_publishes_only_bounded_candidates_from_cache(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'drive'
            dest.mkdir()
            state = root / 'state'
            entries = [file('a.bin', 100000, 'a'), file('b.bin', 200000, 'b'),
                       file('c.bin', 300000, 'c'),
                       file('d.bin', 400000, 'd')]
            mod.inventory.save(state, entries)
            called = []
            def publish(scope, entry, *_a, **_kw):
                called.append((scope, entry['Path']))
                return 'PUBLISHED_VERIFIED'
            output = io.StringIO()
            with (patch.object(mod.archive, 'ROOT', root),
                  patch.object(mod.archive, 'STATE', state),
                  patch.object(mod.archive, 'SOURCES',
                               {'drive': ('icloud:', dest)}),
                  patch.object(mod.archive, 'safe_directory'),
                  patch.object(mod.diagnosis, 'backup_busy',
                               return_value=False),
                  patch.object(mod.pilot, 'publish_verified',
                               side_effect=publish),
                  contextlib.redirect_stdout(output)):
                self.assertEqual(mod.run(execute=True), 0)
            self.assertEqual(called, [('icloud:', 'a.bin'),
                                      ('icloud:', 'b.bin')])
            self.assertIn('2 verified files published', output.getvalue())

    def test_retry_limit_and_per_file_error_does_not_leak_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            dest = root / 'drive'
            dest.mkdir()
            entries = [file('secret-one.pdf', 100000, 'one'),
                       file('secret-two.pdf', 110000, 'two'),
                       file('secret-three.pdf', 120000, 'three'),
                       file('secret-four.pdf', 130000, 'four')]
            mod.inventory.save(state, entries)
            def fail(*_a, **_kw):
                raise mod.pilot.PilotError('TEST')
            output = io.StringIO()
            with (patch.object(mod.archive, 'ROOT', root),
                  patch.object(mod.archive, 'STATE', state),
                  patch.object(mod.archive, 'SOURCES',
                               {'drive': ('icloud:', dest)}),
                  patch.object(mod.archive, 'safe_directory'),
                  patch.object(mod.diagnosis, 'backup_busy',
                               return_value=False),
                  patch.object(mod.pilot, 'publish_verified',
                               side_effect=fail),
                  contextlib.redirect_stdout(output)):
                self.assertEqual(mod.run(execute=True), 0)
            self.assertIn('3 attempted', output.getvalue())
            self.assertNotIn('secret-one', output.getvalue())
            self.assertIn('0 verified files', output.getvalue())

    def test_malformed_private_snapshot_is_not_accepted(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            mod.inventory.save(root, [file()])
            path = mod.inventory.inventory_path(root)
            content = json.loads(path.read_text())
            content['stats']['count'] = 999
            path.write_text(json.dumps(content))
            output = io.StringIO()
            with (patch.object(mod.archive, 'STATE', root),
                  patch.object(mod.pilot, 'publish_verified') as publisher,
                  contextlib.redirect_stdout(output)):
                self.assertEqual(mod.run(execute=True), 2)
            publisher.assert_not_called()
            self.assertIn('validation failed', output.getvalue())

    def test_root_file_uri_uses_icloud_root_correctly(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'drive'
            state = root / 'state'
            dest.mkdir()
            state.mkdir()
            original = file('root-file.bin', 128 * 1024)
            seen = []
            def copy(remote, path, _deadline):
                seen.append(remote)
                body = b'x' * original['Size']
                path.write_bytes(body)
                import hashlib
                return len(body), hashlib.sha256(body).hexdigest()
            with patch.object(mod.diagnosis, 'backup_busy', return_value=False):
                result = mod.pilot.publish_verified(
                    'icloud:', original, dest, state, time.monotonic() + 90,
                    whole_reader=copy, list_reader=lambda *_: [original])
            self.assertEqual(result, 'PUBLISHED_VERIFIED')
            self.assertEqual(seen, ['icloud:root-file.bin', 'icloud:root-file.bin'])
            self.assertTrue((dest / 'root-file.bin').is_file())


if __name__ == '__main__':
    unittest.main()

"""Offline safety tests for the pinned experimental rclone build launcher."""
from __future__ import annotations

from pathlib import Path
import contextlib
import io
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_rclone_batch as builder


class ExperimentalBuildTests(unittest.TestCase):
    def test_preview_does_not_run_commands(self):
        with patch.object(sys, 'argv', ['build_rclone_batch.py']), \
                patch.object(builder, 'run') as runner, \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(builder.main(), 0)
        runner.assert_not_called()
        self.assertIn(builder.RCLONE_COMMIT, stdout.getvalue())
        self.assertIn('No Apple requests', stdout.getvalue())

    def test_pin_is_explicit_commit(self):
        self.assertEqual(builder.RCLONE_TAG, 'v1.75.2')
        self.assertEqual(builder.RCLONE_COMMIT,
                         'be8d6044eacb843b74d335db0c5fbe09222031fe')
        self.assertEqual(builder.BUILD_TAG, 'apple_sync_experimental')

    def test_existing_checkout_requires_exact_origin_and_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / '.git').mkdir()
            with patch.object(builder, 'run', side_effect=[
                    builder.RCLONE_REPO, builder.RCLONE_COMMIT, '']):
                builder.verify_checkout(path)
            with patch.object(builder, 'run', side_effect=[
                    'https://example.com/fake.git', builder.RCLONE_COMMIT, '']):
                with self.assertRaisesRegex(builder.BuildError, 'REMOTE'):
                    builder.verify_checkout(path)
            with patch.object(builder, 'run', side_effect=[
                    builder.RCLONE_REPO, 'a' * 40, '']):
                with self.assertRaisesRegex(builder.BuildError, 'COMMIT'):
                    builder.verify_checkout(path)

    def test_rejects_symlinked_or_file_build_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            regular = root / 'regular'
            regular.write_text('not a directory')
            alias = root / 'alias'
            alias.symlink_to(root, target_is_directory=True)
            for path in (regular, alias):
                with self.subTest(path=path), self.assertRaises(builder.BuildError):
                    builder.checked_directory(path)

    def test_dirty_tracked_source_blocks_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            (checkout / '.git').mkdir()
            with patch.object(builder, 'run', side_effect=[
                    builder.RCLONE_REPO, builder.RCLONE_COMMIT,
                    ' M backend/iclouddrive/iclouddrive.go']):
                with self.assertRaisesRegex(builder.BuildError, 'TRACKED_CHANGES'):
                    builder.verify_checkout(checkout)

    def test_existing_foreign_directory_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(builder.BuildError, 'UNTRUSTED'):
                builder.verify_checkout(Path(tmp))


    def test_second_build_cannot_enter_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'locks'
            with builder.exclusive_build_lock(directory):
                with self.assertRaisesRegex(builder.BuildError, 'BUILD_ALREADY_RUNNING'):
                    with builder.exclusive_build_lock(directory):
                        self.fail('second build entered exclusive section')
            with builder.exclusive_build_lock(directory):
                pass

    def test_lock_file_symlink_rejected_without_target_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'locks'
            directory.mkdir()
            target = Path(tmp) / 'protected'
            target.write_text('original')
            (directory / '.experimental-build.lock').symlink_to(target)
            with self.assertRaisesRegex(builder.BuildError, 'BUILD_LOCK_UNAVAILABLE'):
                with builder.exclusive_build_lock(directory):
                    pass
            self.assertEqual(target.read_text(), 'original')

    def test_lock_bad_mode_rejected_without_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            lock = directory / '.experimental-build.lock'
            lock.touch(mode=0o666)
            lock.chmod(0o644)
            with self.assertRaisesRegex(builder.BuildError, 'UNSAFE_BUILD_LOCK_FILE'):
                with builder.exclusive_build_lock(directory):
                    pass
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o644)

    def test_timeout_is_actionable_without_raw_arguments(self):
        with self.assertRaisesRegex(builder.BuildError, 'MOCK_TASK_TIMED_OUT') as caught:
            builder.run([sys.executable, '-c', 'import time; time.sleep(3)'],
                        timeout=0.2, stage='MOCK_TASK')
        self.assertNotIn('time.sleep', str(caught.exception))

    def test_missing_command_is_actionable(self):
        with self.assertRaisesRegex(builder.BuildError, 'TEST_COMMAND_START_FAILED'):
            builder.run(['/nonexistent/example-builder-tool'],
                        stage='TEST_COMMAND')

    def test_previously_formatted_addons_are_reusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            addon_root = root / 'repo' / 'rclone-addon' / 'iclouddrive'
            addon_root.mkdir(parents=True)
            for name in builder.ADDON_FILES:
                (addon_root / name).write_text('package iclouddrive\n\nfunc test(){println("ok")}\n')
            checkout = root / 'rclone'
            (checkout / 'backend' / 'iclouddrive').mkdir(parents=True)
            with patch.object(builder, 'REPO_ROOT', root / 'repo'):
                builder.stage_addons(checkout)
                prior = [(checkout / 'backend' / 'iclouddrive' / name).read_bytes()
                         for name in builder.ADDON_FILES]
                builder.stage_addons(checkout)
                again = [(checkout / 'backend' / 'iclouddrive' / name).read_bytes()
                         for name in builder.ADDON_FILES]
                self.assertEqual(prior, again)
                self.assertNotEqual(prior[0], (addon_root / builder.ADDON_FILES[0]).read_bytes())
                self.assertIn(b'func test() {', prior[0])

    def test_modified_cached_addon_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            addon_root = root / 'repo' / 'rclone-addon' / 'iclouddrive'
            addon_root.mkdir(parents=True)
            for name in builder.ADDON_FILES:
                (addon_root / name).write_text('package iclouddrive\n')
            checkout = root / 'rclone'
            target = checkout / 'backend' / 'iclouddrive'
            target.mkdir(parents=True)
            (target / builder.ADDON_FILES[0]).write_text('modified code\n')
            with patch.object(builder, 'REPO_ROOT', root / 'repo'):
                with self.assertRaisesRegex(builder.BuildError, 'ALREADY_MODIFIED'):
                    builder.stage_addons(checkout)
            self.assertEqual((target / builder.ADDON_FILES[0]).read_text(), 'modified code\n')

    def test_existing_binary_is_verified_without_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / 'rclone-batch'
            binary.write_text('stand-in for a verified executable')
            binary.chmod(0o700)
            with patch.object(builder, 'run', side_effect=[
                    self._rclone_version(), self._go_metadata()]) as mock:
                self.assertTrue(builder.existing_binary_verified(binary))
                self.assertEqual(mock.call_count, 2)
            self.assertEqual(binary.read_text(), 'stand-in for a verified executable')
            with patch.object(builder, 'run', side_effect=[
                    self._rclone_version(), self._go_metadata(tag='none')]):
                with self.assertRaisesRegex(builder.BuildError, 'UNVERIFIED'):
                    builder.existing_binary_verified(binary)

    @staticmethod
    def _rclone_version():
        # rclone's go/tags output is allowed to say "none" even when
        # the Go executable was compiled with -tags.
        return 'rclone v1.75.2\n- go/tags: none'

    @staticmethod
    def _go_metadata(module='github.com/rclone/rclone',
                     tag='apple_sync_experimental'):
        return ('/private/rclone: go1.27.2\n'
                '\tpath\t' + module + '\n'
                '\tbuild\t-tags=' + tag)

    def test_accepts_tag_in_go_build_metadata_not_rclone_display(self):
        with patch.object(builder, 'run', side_effect=[
                self._rclone_version(), self._go_metadata()]):
            self.assertIn('rclone v1.75.2',
                          builder.verify_binary_identity(Path('/private/rclone')))

    def test_rejects_wrong_binary_module_and_missing_tag(self):
        invalid_cases = [
            (self._go_metadata(module='github.com/other/rclone'), 'MODULE_MISMATCH'),
            (self._go_metadata(tag='none'), 'TAG_MISSING'),
            (self._go_metadata(tag='not_apple_sync_experimental'), 'TAG_MISSING'),
            (self._go_metadata(tag=''), 'TAG_MISSING'),
            ('go1.27.2\n\tpath\tgithub.com/rclone/rclone', 'TAG_MISSING'),
        ]
        for metadata, error in invalid_cases:
            with self.subTest(error=error, metadata=metadata):
                with patch.object(builder, 'run', side_effect=[
                        self._rclone_version(), metadata]):
                    with self.assertRaisesRegex(builder.BuildError, error):
                        builder.verify_binary_identity(Path('/private/rclone'))

    def test_rejects_wrong_rclone_version_without_go_read(self):
        with patch.object(builder, 'run', return_value='rclone v1.75.1\n- go/tags: none') as mock:
            with self.assertRaisesRegex(builder.BuildError, 'RCLONE_VERSION_MISMATCH'):
                builder.verify_binary_identity(Path('/private/rclone'))
        mock.assert_called_once()

    def test_accepts_tag_in_comma_separated_go_build_tags(self):
        with patch.object(builder, 'run', side_effect=[
                self._rclone_version(),
                self._go_metadata(tag='cmount,apple_sync_experimental,foo')]):
            builder.verify_binary_identity(Path('/private/rclone'))

    def test_rejects_duplicate_metadata_tag_keys(self):
        with patch.object(builder, 'run', side_effect=[
                self._rclone_version(),
                self._go_metadata() + '\n\tbuild\t-tags=apple_sync_experimental']):
            with self.assertRaisesRegex(builder.BuildError, 'TAG_MISSING'):
                builder.verify_binary_identity(Path('/private/rclone'))

    def test_existing_binary_symlink_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / 'rclone-batch'
            binary.symlink_to(Path(tmp) / 'none')
            with self.assertRaisesRegex(builder.BuildError, 'UNSAFE_EXPERIMENTAL_BINARY'):
                builder.existing_binary_verified(binary)


if __name__ == '__main__':
    unittest.main()

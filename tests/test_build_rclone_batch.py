"""Offline safety tests for the pinned experimental rclone build launcher."""
from __future__ import annotations

from pathlib import Path
import contextlib
import io
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


if __name__ == '__main__':
    unittest.main()

"""No-Apple, no-systemd tests for fd-relative replay receipt-channel leases."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_channel as channel
import drive_batch_replay_systemd_plan as plan


class ChannelTests(unittest.TestCase):
    def setUp(self):
        if os.getuid() == 0:
            self.skipTest('fd creation test uses an unprivileged user account')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / 'user-runtime'
        self.runtime.mkdir(mode=0o700)
        self.runtime.chmod(0o700)
        self.uid = os.getuid()
        self.gid = os.getgid()

    def allocate(self):
        fd = os.open(self.runtime, channel.O_DIR)
        try:
            result = channel._create_at_fd(
                fd, uid=self.uid, gid=self.gid,
                runtime_path=self.runtime)
        finally:
            os.close(fd)
        self.addCleanup(result.close)
        return result

    def test_private_channel_has_exact_owner_and_permissions(self):
        lease = self.allocate()
        self.assertTrue(lease.path.is_dir())
        info = lease.path.lstat()
        self.assertEqual(info.st_uid, self.uid)
        self.assertEqual(info.st_gid, self.gid)
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        self.assertTrue(plan.CHANNEL_NAME.fullmatch(lease.name))
        self.assertEqual(lease.path.parent, self.runtime)

    def test_new_channel_names_are_independent_and_private(self):
        first, second = self.allocate(), self.allocate()
        self.assertNotEqual(first.name, second.name)
        self.assertNotEqual(first.inode, second.inode)
        self.assertTrue(first.path.exists())
        self.assertTrue(second.path.exists())

    def test_cleanup_empty_channel_after_verified_stop(self):
        lease = self.allocate()
        path = lease.path
        lease.cleanup_after_stop(stopped_verified=True)
        self.assertFalse(path.exists())
        self.assertTrue(lease.closed)

    def test_cleanup_removes_only_single_trusted_receipt(self):
        lease = self.allocate()
        file = lease.path / channel.RESULT_NAME
        file.write_bytes(b'{"kind":"observation"}')
        file.chmod(0o600)
        lease.cleanup_after_stop(stopped_verified=True)
        self.assertFalse(lease.path.exists())
        self.assertTrue(lease.closed)

    def test_unconfirmed_stop_leaves_channel_intact(self):
        lease = self.allocate()
        with self.assertRaisesRegex(channel.ReplayChannelError,
                                    'REPLAY_STOP_UNCONFIRMED'):
            lease.cleanup_after_stop(stopped_verified=False)
        self.assertTrue(lease.path.exists())
        self.assertFalse(lease.closed)

    def test_truthy_nonbool_is_not_accepted_as_stop_proof(self):
        lease = self.allocate()
        for value in (1, 'true', [], None):
            with self.subTest(value=value), self.assertRaisesRegex(
                    channel.ReplayChannelError, 'REPLAY_STOP_UNCONFIRMED'):
                lease.cleanup_after_stop(stopped_verified=value)
        self.assertTrue(lease.path.exists())

    def test_an_unexpected_child_prevents_cleanup(self):
        lease = self.allocate()
        private = lease.path / 'private-not-a-receipt'
        private.write_bytes(b'keep')
        with self.assertRaisesRegex(channel.ReplayChannelError,
                                    'REPLAY_CHANNEL_UNEXPECTED_CONTENTS'):
            lease.cleanup_after_stop(stopped_verified=True)
        self.assertEqual(private.read_bytes(), b'keep')

    def test_more_than_one_child_prevents_cleanup(self):
        lease = self.allocate()
        (lease.path / 'other').write_bytes(b'keep')
        (lease.path / channel.RESULT_NAME).write_bytes(b'keep')
        with self.assertRaisesRegex(channel.ReplayChannelError,
                                    'REPLAY_CHANNEL_UNEXPECTED_CONTENTS'):
            lease.cleanup_after_stop(stopped_verified=True)
        self.assertTrue(lease.path.exists())

    def test_symlink_receipt_is_rejected_without_following(self):
        lease = self.allocate()
        target = self.runtime / 'untouched'
        target.write_bytes(b'private')
        (lease.path / channel.RESULT_NAME).symlink_to(target)
        with self.assertRaisesRegex(channel.ReplayChannelError,
                                    'REPLAY_RECEIPT_UNTRUSTED'):
            lease.cleanup_after_stop(stopped_verified=True)
        self.assertEqual(target.read_bytes(), b'private')

    def test_world_readable_receipt_blocks_cleanup(self):
        lease = self.allocate()
        receipt = lease.path / channel.RESULT_NAME
        receipt.write_bytes(b'payload')
        receipt.chmod(0o644)
        with self.assertRaisesRegex(channel.ReplayChannelError,
                                    'REPLAY_RECEIPT_UNTRUSTED'):
            lease.cleanup_after_stop(stopped_verified=True)
        self.assertTrue(receipt.exists())

    def test_hardlinked_receipt_is_refused(self):
        lease = self.allocate()
        original = self.runtime / 'original'
        original.write_bytes(b'private')
        original.chmod(0o600)
        os.link(original, lease.path / channel.RESULT_NAME)
        with self.assertRaisesRegex(channel.ReplayChannelError,
                                    'REPLAY_RECEIPT_UNTRUSTED'):
            lease.cleanup_after_stop(stopped_verified=True)
        self.assertEqual(original.read_bytes(), b'private')

    def test_replacement_symlink_directory_is_not_removed(self):
        lease = self.allocate()
        old = lease.path.with_name(lease.name + '-moved')
        lease.path.rename(old)
        target = self.runtime / 'other-directory'
        target.mkdir()
        lease.path.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(channel.ReplayChannelError,
                                    'REPLAY_CHANNEL_CHANGED'):
            lease.cleanup_after_stop(stopped_verified=True)
        self.assertTrue(target.exists())
        self.assertTrue(lease.path.is_symlink())

    def test_replacement_directory_inode_is_not_removed(self):
        lease = self.allocate()
        previous = lease.path.with_name(lease.name + '-previous')
        lease.path.rename(previous)
        lease.path.mkdir(mode=0o700)
        with self.assertRaisesRegex(channel.ReplayChannelError,
                                    'REPLAY_CHANNEL_CHANGED'):
            lease.cleanup_after_stop(stopped_verified=True)
        self.assertTrue(lease.path.exists())
        self.assertTrue(previous.exists())

    def test_changed_directory_mode_refuses_cleanup(self):
        lease = self.allocate()
        lease.path.chmod(0o755)
        with self.assertRaisesRegex(channel.ReplayChannelError,
                                    'REPLAY_CHANNEL_CHANGED'):
            lease.cleanup_after_stop(stopped_verified=True)
        self.assertTrue(lease.path.exists())

    def test_double_cleanup_is_refused(self):
        lease = self.allocate()
        lease.cleanup_after_stop(stopped_verified=True)
        with self.assertRaisesRegex(channel.ReplayChannelError,
                                    'REPLAY_CHANNEL_CLOSED'):
            lease.cleanup_after_stop(stopped_verified=True)

    def test_close_releases_fds_but_does_not_delete(self):
        lease = self.allocate()
        lease.close()
        self.assertTrue(lease.closed)
        self.assertTrue(lease.path.exists())
        lease.close()

    def test_untrusted_runtime_mode_refused_before_mkdir(self):
        self.runtime.chmod(0o755)
        with self.assertRaisesRegex(channel.ReplayChannelError,
                                    'REPLAY_RUNTIME_DIRECTORY_UNTRUSTED'):
            self.allocate()
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_wrong_runtime_owner_refused_before_creation(self):
        fd = os.open(self.runtime, channel.O_DIR)
        try:
            with self.assertRaisesRegex(channel.ReplayChannelError,
                                        'REPLAY_RUNTIME_DIRECTORY_UNTRUSTED'):
                channel._create_at_fd(
                    fd, uid=self.uid + 1, gid=self.gid,
                    runtime_path=self.runtime)
        finally:
            os.close(fd)
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_invalid_user_or_group_ids_are_rejected(self):
        fd = os.open(self.runtime, channel.O_DIR)
        try:
            for uid, gid in ((0, self.gid), (True, self.gid),
                             ('user', self.gid), (self.uid, -1),
                             (self.uid, False)):
                with self.subTest(uid=uid, gid=gid), self.assertRaisesRegex(
                        channel.ReplayChannelError, 'REPLAY_RUNTIME_ID_INVALID'):
                    channel._create_at_fd(
                        fd, uid=uid, gid=gid, runtime_path=self.runtime)
        finally:
            os.close(fd)

    def test_relative_runtime_path_is_rejected(self):
        fd = os.open(self.runtime, channel.O_DIR)
        try:
            with self.assertRaisesRegex(channel.ReplayChannelError,
                                        'REPLAY_RUNTIME_ID_INVALID'):
                channel._create_at_fd(
                    fd, uid=self.uid, gid=self.gid,
                    runtime_path=Path('relative'))
        finally:
            os.close(fd)

    def test_public_allocator_refuses_nonroot_without_touching_runtime(self):
        with patch.object(channel.os, 'geteuid', return_value=self.uid):
            with self.assertRaisesRegex(channel.ReplayChannelError,
                                        'REPLAY_ROOT_SERVICE_REQUIRED'):
                channel.allocate_root_channel()

    def test_module_has_no_service_execution_or_auto_cleanup(self):
        source = Path(channel.__file__).read_text(encoding='utf-8')
        self.assertNotIn('import subprocess', source)
        self.assertNotIn('subprocess.run(', source)
        self.assertNotIn("if __name__ == '__main__'", source)
        lease = self.allocate()
        self.assertTrue(lease.path.exists())
        lease.close()
        self.assertTrue(lease.path.exists())


if __name__ == '__main__':
    unittest.main()

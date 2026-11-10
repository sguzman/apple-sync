"""Offline, unprivileged QA for dormant root-owned replay code verification."""
from __future__ import annotations

from pathlib import Path
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_root_trust as trust


class RootTrustTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'root-code-sample'
        self.folder.mkdir(mode=0o700)
        self.folder.chmod(0o700)
        self.uid = os.getuid()
        for name in trust.ROOT_REPLAY_MODULES:
            file = self.folder / name
            file.write_bytes(b'# static root-only example\n')
            file.chmod(0o644)

    def verify(self, modules=None, owner=None):
        fd = os.open(self.folder, trust.DIR_FLAGS)
        try:
            trust._verify_modules_in_fd(
                fd, owner=self.uid if owner is None else owner,
                modules=(trust.ROOT_REPLAY_MODULES if modules is None
                         else modules))
        finally:
            os.close(fd)

    def test_complete_owned_module_manifest_passes(self):
        self.verify()

    def test_each_module_is_present_and_named_once(self):
        names = trust.ROOT_REPLAY_MODULES
        self.assertEqual(len(names), 10)
        self.assertEqual(len(set(names)), len(names))
        self.assertIn('drive_meter_budget.py', names)
        self.assertIn('drive_batch_replay_lifecycle.py', names)
        self.assertIn('drive_batch_replay_root_trust.py', names)

    def test_user_writable_checkout_is_not_the_install_target(self):
        self.assertEqual(
            trust.LIBEXEC,
            Path('/usr/local/libexec/apple-sync-drive-metered'))
        self.assertNotIn('/home/', str(trust.LIBEXEC))

    def test_module_permission_mode_0644_is_valid(self):
        self.verify()
        self.assertEqual(stat.S_IMODE(
            (self.folder / 'drive_meter_budget.py').stat().st_mode), 0o644)

    def test_missing_any_required_file_fails_closed(self):
        for name in trust.ROOT_REPLAY_MODULES:
            with self.subTest(name=name):
                path = self.folder / name
                path.unlink()
                with self.assertRaisesRegex(
                        trust.ReplayRootTrustError, 'REPLAY_ROOT_MODULE_MISSING'):
                    self.verify()
                path.write_bytes(b'# restored\n')
                path.chmod(0o644)

    def test_module_symlink_not_followed(self):
        name = trust.ROOT_REPLAY_MODULES[0]
        path = self.folder / name
        target = self.folder / 'private-target'
        target.write_bytes(b'# trusted?\n')
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MODULE_MISSING'):
            self.verify()
        self.assertEqual(target.read_bytes(), b'# trusted?\n')

    def test_module_directory_cannot_be_imported_as_file(self):
        path = self.folder / trust.ROOT_REPLAY_MODULES[0]
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MODULE_UNTRUSTED'):
            self.verify()

    def test_file_mode_0666_is_rejected(self):
        path = self.folder / trust.ROOT_REPLAY_MODULES[0]
        path.chmod(0o666)
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MODULE_UNTRUSTED'):
            self.verify()

    def test_file_mode_0664_is_rejected(self):
        path = self.folder / trust.ROOT_REPLAY_MODULES[0]
        path.chmod(0o664)
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MODULE_UNTRUSTED'):
            self.verify()

    def test_file_mode_0646_is_rejected(self):
        path = self.folder / trust.ROOT_REPLAY_MODULES[0]
        path.chmod(0o646)
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MODULE_UNTRUSTED'):
            self.verify()

    def test_file_with_multiple_hardlinks_is_rejected(self):
        path = self.folder / trust.ROOT_REPLAY_MODULES[0]
        os.link(path, self.folder / 'second-link')
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MODULE_UNTRUSTED'):
            self.verify()

    def test_empty_module_refused(self):
        path = self.folder / trust.ROOT_REPLAY_MODULES[0]
        path.write_bytes(b'')
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MODULE_UNTRUSTED'):
            self.verify()

    def test_wrong_owner_refused(self):
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_DIRECTORY_UNTRUSTED'):
            self.verify(owner=self.uid + 1)

    def test_world_writable_directory_refused(self):
        self.folder.chmod(0o777)
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_DIRECTORY_UNTRUSTED'):
            self.verify()

    def test_group_writable_directory_refused(self):
        self.folder.chmod(0o775)
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_DIRECTORY_UNTRUSTED'):
            self.verify()

    def test_manifest_duplicate_module_refused(self):
        modules = (trust.ROOT_REPLAY_MODULES[0],) * 2
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MANIFEST_INVALID'):
            self.verify(modules=modules)

    def test_manifest_empty_refused(self):
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MANIFEST_INVALID'):
            self.verify(modules=())

    def test_non_tuple_module_list_refused(self):
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MANIFEST_INVALID'):
            self.verify(modules=['drive_meter_budget.py'])

    def test_path_traversal_and_shell_tokens_refused(self):
        values = ('../evil.py', '/tmp/evil.py', 'foo/bar.py', 'bad;ls.py',
                  '.py', '..py', 'hello world.py', r'..\evil.py')
        for name in values:
            with self.subTest(name=name), self.assertRaisesRegex(
                    trust.ReplayRootTrustError, 'REPLAY_ROOT_MANIFEST_INVALID'):
                self.verify(modules=(name,))

    def test_non_ascii_or_non_string_manifest_denied(self):
        for value in ('évil.py', None, 1234, Path('module.py')):
            with self.subTest(value=str(value)), self.assertRaisesRegex(
                    trust.ReplayRootTrustError, 'REPLAY_ROOT_MANIFEST_INVALID'):
                self.verify(modules=(value,))

    def test_public_check_refuses_unprivileged_user(self):
        with patch.object(trust.os, 'geteuid', return_value=1000):
            with self.assertRaisesRegex(
                    trust.ReplayRootTrustError, 'REPLAY_ROOT_REQUIRED'):
                trust.verify_root_installation()

    def test_public_check_refuses_patched_non_install_root(self):
        with (patch.object(trust.os, 'geteuid', return_value=0),
              patch.object(trust, 'LIBEXEC', Path('/tmp/test-root-code'))):
            with self.assertRaisesRegex(
                    trust.ReplayRootTrustError,
                    'REPLAY_ROOT_INSTALL_PATH_INVALID'):
                trust.verify_root_installation()

    def test_module_verification_is_read_only(self):
        before = {name: (self.folder / name).read_bytes()
                  for name in trust.ROOT_REPLAY_MODULES}
        self.verify()
        after = {name: (self.folder / name).read_bytes()
                 for name in trust.ROOT_REPLAY_MODULES}
        self.assertEqual(before, after)

    def test_nofollow_directory_and_file_fds(self):
        self.assertTrue(trust.DIR_FLAGS & os.O_NOFOLLOW)
        self.assertTrue(trust.DIR_FLAGS & os.O_DIRECTORY)
        self.assertTrue(trust.FILE_FLAGS & os.O_NOFOLLOW)
        self.assertTrue(trust.FILE_FLAGS & os.O_CLOEXEC)

    def test_verifier_contains_no_service_or_live_apple_execution(self):
        source = Path(trust.__file__).read_text(encoding='utf-8')
        for forbidden in ('import subprocess', 'subprocess.run(',
                          'RcloneRCReader(', 'replay_once(',
                          'BudgetLedger(', 'os.system(',
                          "if __name__ == '__main__'"):
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main()

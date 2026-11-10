"""Offline security tests for the future root-owned replay worker code path.

No systemd execution, Apple network, installer, ledger, timer or checkpoint IO.
The worker CLI remains dormant. Temporary files are owned by test user.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_root_trust as trust
import drive_batch_replay_systemd_plan as plan
import drive_batch_replay_worker as worker
import install_metered_launcher as installer

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
UNIT = 'apple-sync-drive-metered-replay-1234.service'
CHANNEL = Path('/run/user/1000/.apple-sync-replay-aBcDE123')


class ProtectedWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.modules = Path(self.temp.name) / 'root-code-fixture'
        self.modules.mkdir(mode=0o700)
        self.modules.chmod(0o700)
        self.uid = os.getuid()
        for name in trust.ROOT_WORKER_MODULES:
            path = self.modules / name
            path.write_bytes(b'# unprivileged immutable-code simulation\n')
            path.chmod(0o644)

    def verify(self, names=None, uid=None):
        fd = os.open(self.modules, trust.DIR_FLAGS)
        try:
            trust._verify_modules_in_fd(
                fd,
                owner=self.uid if uid is None else uid,
                modules=trust.ROOT_WORKER_MODULES if names is None else names,
            )
        finally:
            os.close(fd)

    def test_fixed_worker_module_manifest_has_21_distinct_entries(self):
        self.assertEqual(len(trust.ROOT_WORKER_MODULES), 21)
        self.assertEqual(len(set(trust.ROOT_WORKER_MODULES)), 21)

    def test_root_and_worker_manifests_are_disjoint_and_complete(self):
        self.assertTrue(set(trust.ROOT_REPLAY_MODULES).isdisjoint(
            trust.ROOT_WORKER_MODULES))
        self.assertEqual(len(trust.ROOT_REPLAY_MODULES), 11)
        self.assertEqual(len(set(trust.ROOT_REPLAY_MODULES)
                             | set(trust.ROOT_WORKER_MODULES)), 32)

    def test_key_worker_and_preflight_modules_are_in_manifest(self):
        for name in ('drive_batch_replay_worker.py',
                     'drive_batch_replay_worker_entry.py',
                     'drive_batch_replay_worker_preflight.py',
                     'drive_batch_replay_worker_gate.py',
                     'drive_inventory_rc.py',
                     'archive_batch.py'):
            with self.subTest(name=name):
                self.assertIn(name, trust.ROOT_WORKER_MODULES)

    def test_transitive_local_imports_in_approved_tree_are_closed(self):
        available = set(trust.ROOT_REPLAY_MODULES) | set(
            trust.ROOT_WORKER_MODULES)
        for filename in sorted(available):
            source = (SCRIPTS / filename).read_text(encoding='utf-8')
            tree = ast.parse(source, filename=filename)
            modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(alias.name.split('.')[0]
                                   for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    if node.module:
                        modules.add(node.module.split('.')[0])
            for module in modules:
                local = module + '.py'
                if (SCRIPTS / local).is_file():
                    with self.subTest(importer=filename, dependency=local):
                        self.assertIn(local, available)

    def test_future_worker_argv_points_only_to_root_libexec(self):
        command = plan.worker_start_argv(
            UNIT, uid=1000, receipt_dir=CHANNEL)
        self.assertEqual(plan.WORKER_ROOT, trust.LIBEXEC)
        self.assertEqual(plan.WORKER,
                         trust.LIBEXEC / 'drive_batch_replay_worker.py')
        self.assertEqual(command[-2], str(plan.WORKER))

    def test_python_flags_ignore_user_paths_and_bytecode(self):
        command = plan.worker_start_argv(
            UNIT, uid=1000, receipt_dir=CHANNEL)
        self.assertEqual(command[-6:-2],
                         ('/usr/bin/python3', '-E', '-s', '-B'))

    def test_working_directory_is_never_the_user_checkout(self):
        command = plan.worker_start_argv(
            UNIT, uid=1000, receipt_dir=CHANNEL)
        self.assertIn(
            '--property=WorkingDirectory=' + str(trust.LIBEXEC),
            command)
        self.assertFalse(any('Code/Sync/apple-sync' in arg for arg in command))

    def test_worker_still_unprivileged_with_ip_accounting(self):
        command = plan.worker_start_argv(
            UNIT, uid=1000, receipt_dir=CHANNEL)
        self.assertIn('--property=User=sguzman', command)
        self.assertIn('--property=IPAccounting=yes', command)
        self.assertIn('--property=NoNewPrivileges=yes', command)
        self.assertIn('--property=Restart=no', command)
        self.assertNotIn('--on-calendar', command)

    def test_worker_path_is_fixed_not_a_caller_parameter(self):
        import inspect
        signature = inspect.signature(plan.worker_start_argv)
        self.assertEqual(list(signature.parameters),
                         ['unit', 'uid', 'receipt_dir'])
        self.assertNotIn('worker', signature.parameters)

    def test_original_worker_command_remains_dormant(self):
        source = (SCRIPTS / plan.WORKER.name).read_text(encoding='utf-8')
        self.assertIn('REPLAY_WORKER_DORMANT', source)
        self.assertIn("if __name__ == '__main__':", source)
        self.assertNotIn('guarded_once()', source)
        self.assertEqual(worker.__file__, str(SCRIPTS / plan.WORKER.name))

    def test_unprivileged_fixture_worker_modules_pass_verification(self):
        self.verify()

    def test_missing_worker_dependency_refused(self):
        (self.modules / 'drive_inventory_rc.py').unlink()
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MODULE_MISSING'):
            self.verify()

    def test_symlink_worker_module_never_followed(self):
        suspect = self.modules / 'drive_batch_replay_worker.py'
        target = self.modules / 'private-target'
        target.write_bytes(b'untouched')
        suspect.unlink()
        suspect.symlink_to(target)
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MODULE_MISSING'):
            self.verify()
        self.assertEqual(target.read_bytes(), b'untouched')

    def test_group_writable_worker_module_refused(self):
        suspect = self.modules / 'drive_batch_replay_worker_entry.py'
        suspect.chmod(0o664)
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MODULE_UNTRUSTED'):
            self.verify()

    def test_hardlinked_worker_module_refused(self):
        suspect = self.modules / 'drive_batch_replay_protocol.py'
        os.link(suspect, self.modules / 'duplicate-link')
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MODULE_UNTRUSTED'):
            self.verify()

    def test_untrusted_worker_directory_owner_refused(self):
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_DIRECTORY_UNTRUSTED'):
            self.verify(uid=self.uid + 1)

    def test_duplicate_module_manifest_refused(self):
        duplicate = (trust.ROOT_WORKER_MODULES[0],) * 2
        with self.assertRaisesRegex(
                trust.ReplayRootTrustError, 'REPLAY_ROOT_MANIFEST_INVALID'):
            self.verify(names=duplicate)

    def test_existing_installer_does_not_stage_or_start_replay(self):
        destinations = tuple(str(dst) for _src, dst in installer.COPIES)
        sources = tuple(str(src) for src, _dst in installer.COPIES)
        self.assertFalse(any('drive_batch_replay_worker.py' in name
                             for name in sources + destinations))
        self.assertFalse(any('drive_batch_replay_root_bootstrap.py' in name
                             for name in sources + destinations))
        self.assertTrue(all('apple-sync-drive-metered-replay-' not in name
                            for name in destinations))


if __name__ == '__main__':
    unittest.main()

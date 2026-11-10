"""Offline tests for diagnostic-only root replay bootstrap (never Apple traffic)."""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_replay_root_bootstrap as boot
import drive_batch_replay_root_trust as trust

FLAGS = SimpleNamespace(
    isolated=1, no_site=1, no_user_site=1, dont_write_bytecode=1)


def invocation(**replace):
    args = dict(argv=('doctor',), uid=0, interpreter='/usr/bin/python3',
                script=str(boot.BOOTSTRAP), flags=FLAGS,
                prefix='/usr', base_prefix='/usr')
    args.update(replace)
    return args


class RootBootstrapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / 'example-root-code'
        self.directory.mkdir(mode=0o700)
        self.directory.chmod(0o700)
        self.uid = os.getuid()
        for name in (boot.BOOTSTRAP_NAME, boot.TRUST_NAME):
            file = self.directory / name
            file.write_bytes(b'# test-only example\n')
            file.chmod(0o644)

    def check_seed(self, owner=None):
        fd = os.open(self.directory, boot.DIR_FLAGS)
        try:
            boot._verify_seed_at_fd(
                fd, owner=self.uid if owner is None else owner)
        finally:
            os.close(fd)

    def diagnostic(self, *, fail=None, where=None):
        calls = []
        def validate(**_kwargs):
            calls.append('validate')
            if where == 'validate':
                raise boot.ReplayBootstrapError('private')
        def seed():
            calls.append('seed')
            if where == 'seed':
                raise boot.ReplayBootstrapError('private')
        def trust_modules():
            calls.append('trust')
            if where == 'trust':
                raise RuntimeError('/private/credential')
        out, err = StringIO(), StringIO()
        with (patch.object(boot, 'validate_invocation',
                           side_effect=validate),
              patch.object(boot, 'verify_installed_seed', side_effect=seed),
              patch.object(boot, '_verify_installed_modules',
                           side_effect=trust_modules),
              redirect_stdout(out), redirect_stderr(err)):
            result = boot.main(('doctor',))
        return result, out.getvalue(), err.getvalue(), calls

    def test_pinned_doctor_invocation_is_valid(self):
        self.assertIsNone(boot.validate_invocation(**invocation()))

    def test_any_other_action_refused(self):
        for args in ((), ('run',), ('--execute',), ('doctor', '--execute'),
                     ('start',), ('install',), ('status',), ('doctor', 'run'),
                     ['doctor']):
            with self.subTest(args=args), self.assertRaisesRegex(
                    boot.ReplayBootstrapError,
                    'REPLAY_BOOTSTRAP_ACTION_DISABLED'):
                boot.validate_invocation(**invocation(argv=args))

    def test_root_only_uid_validation(self):
        for uid in (-1, 1000, True, False, None, '0'):
            with self.subTest(uid=uid), self.assertRaisesRegex(
                    boot.ReplayBootstrapError,
                    'REPLAY_BOOTSTRAP_ROOT_REQUIRED'):
                boot.validate_invocation(**invocation(uid=uid))

    def test_interpreter_exact_path_required(self):
        for binary in ('/bin/python3', 'python3', '/usr/bin/python3.15',
                       '/home/user/.venv/bin/python3', None):
            with self.subTest(binary=binary), self.assertRaisesRegex(
                    boot.ReplayBootstrapError,
                    'REPLAY_BOOTSTRAP_INTERPRETER_UNTRUSTED'):
                boot.validate_invocation(**invocation(interpreter=binary))

    def test_script_must_not_be_user_checkout(self):
        for script in ('/home/sguzman/Code/Sync/apple-sync/scripts/'
                       'drive_batch_replay_root_bootstrap.py',
                       '/tmp/drive_batch_replay_root_bootstrap.py',
                       str(boot.BOOTSTRAP) + '.bak', None):
            with self.subTest(script=script), self.assertRaisesRegex(
                    boot.ReplayBootstrapError,
                    'REPLAY_BOOTSTRAP_INTERPRETER_UNTRUSTED'):
                boot.validate_invocation(**invocation(script=script))

    def test_each_missing_isolation_flag_refused(self):
        for name in vars(FLAGS):
            invalid = vars(FLAGS).copy()
            invalid[name] = 0
            with self.subTest(flag=name), self.assertRaisesRegex(
                    boot.ReplayBootstrapError,
                    'REPLAY_BOOTSTRAP_INTERPRETER_UNTRUSTED'):
                boot.validate_invocation(**invocation(
                    flags=SimpleNamespace(**invalid)))

    def test_venv_and_missing_flags_refused(self):
        for overrides in ({'prefix': '/venv'},
                          {'flags': object()}, {'base_prefix': '/venv'}):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(
                    boot.ReplayBootstrapError,
                    'REPLAY_BOOTSTRAP_INTERPRETER_UNTRUSTED'):
                boot.validate_invocation(**invocation(**overrides))

    def test_bootstrap_manifest_includes_11_root_modules(self):
        self.assertEqual(len(trust.ROOT_REPLAY_MODULES), 11)
        self.assertIn(boot.BOOTSTRAP_NAME, trust.ROOT_REPLAY_MODULES)
        self.assertIn(boot.TRUST_NAME, trust.ROOT_REPLAY_MODULES)

    def test_two_trusted_seed_files_pass(self):
        self.check_seed()

    def test_missing_seed_files_refused(self):
        for name in (boot.TRUST_NAME, boot.BOOTSTRAP_NAME):
            path = self.directory / name
            path.unlink()
            with self.subTest(name=name), self.assertRaisesRegex(
                    boot.ReplayBootstrapError,
                    'REPLAY_BOOTSTRAP_CODE_MISSING'):
                self.check_seed()
            path.write_bytes(b'# restored test\n')
            path.chmod(0o644)

    def test_symlink_to_private_seed_is_not_followed(self):
        private = self.directory / 'private'
        private.write_bytes(b'keep')
        suspect = self.directory / boot.TRUST_NAME
        suspect.unlink()
        suspect.symlink_to(private)
        with self.assertRaisesRegex(
                boot.ReplayBootstrapError, 'REPLAY_BOOTSTRAP_CODE_MISSING'):
            self.check_seed()
        self.assertEqual(private.read_bytes(), b'keep')

    def test_directory_disguised_as_module_refused(self):
        suspect = self.directory / boot.TRUST_NAME
        suspect.unlink()
        suspect.mkdir()
        with self.assertRaisesRegex(
                boot.ReplayBootstrapError, 'REPLAY_BOOTSTRAP_CODE_UNTRUSTED'):
            self.check_seed()

    def test_group_world_writable_seed_files_refused(self):
        suspect = self.directory / boot.BOOTSTRAP_NAME
        for mode in (0o666, 0o664, 0o646):
            suspect.chmod(mode)
            with self.subTest(mode=mode), self.assertRaisesRegex(
                    boot.ReplayBootstrapError,
                    'REPLAY_BOOTSTRAP_CODE_UNTRUSTED'):
                self.check_seed()

    def test_empty_seed_file_refused(self):
        (self.directory / boot.TRUST_NAME).write_bytes(b'')
        with self.assertRaisesRegex(
                boot.ReplayBootstrapError, 'REPLAY_BOOTSTRAP_CODE_UNTRUSTED'):
            self.check_seed()

    def test_hardlink_seed_file_refused(self):
        target = self.directory / boot.BOOTSTRAP_NAME
        os.link(target, self.directory / 'second-link')
        with self.assertRaisesRegex(
                boot.ReplayBootstrapError, 'REPLAY_BOOTSTRAP_CODE_UNTRUSTED'):
            self.check_seed()

    def test_writable_seed_directory_refused(self):
        for mode in (0o775, 0o777):
            self.directory.chmod(mode)
            with self.subTest(mode=mode), self.assertRaisesRegex(
                    boot.ReplayBootstrapError,
                    'REPLAY_BOOTSTRAP_DIRECTORY_UNTRUSTED'):
                self.check_seed()

    def test_wrong_directory_owner_refused(self):
        with self.assertRaisesRegex(
                boot.ReplayBootstrapError,
                'REPLAY_BOOTSTRAP_DIRECTORY_UNTRUSTED'):
            self.check_seed(owner=self.uid + 1)

    def test_seed_checks_are_read_only(self):
        files = (boot.TRUST_NAME, boot.BOOTSTRAP_NAME)
        before = [((self.directory / f).read_bytes()) for f in files]
        self.check_seed()
        after = [((self.directory / f).read_bytes()) for f in files]
        self.assertEqual(before, after)

    def test_public_install_verification_refuses_nonroot_without_io(self):
        with (patch.object(boot.os, 'geteuid', return_value=1000),
              patch.object(boot.os, 'open') as opener):
            with self.assertRaisesRegex(
                    boot.ReplayBootstrapError,
                    'REPLAY_BOOTSTRAP_ROOT_REQUIRED'):
                boot.verify_installed_seed()
            opener.assert_not_called()

    def test_main_doctor_runs_checks_in_order(self):
        code, stdout, stderr, calls = self.diagnostic()
        self.assertEqual(code, 0)
        self.assertEqual(stderr, '')
        self.assertEqual(calls, ['validate', 'seed', 'trust'])
        self.assertIn('REPLAY_ROOT_BOOTSTRAP_DORMANT:', stdout)
        self.assertIn('NO_APPLE_ACCESS', stdout)
        self.assertIn('NO_SERVICE_START', stdout)

    def test_main_refuses_execute_without_attempting_trust_import(self):
        stdout, stderr = StringIO(), StringIO()
        with (patch.object(boot, '_verify_installed_modules') as loader,
              patch.object(boot, 'verify_installed_seed') as seed,
              redirect_stdout(stdout), redirect_stderr(stderr)):
            result = boot.main(('--execute',))
        self.assertEqual(result, 2)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(),
                         'REPLAY_ROOT_BOOTSTRAP_REFUSED\n')
        loader.assert_not_called()
        seed.assert_not_called()

    def test_any_failed_precheck_refuses_and_hides_private_message(self):
        for where in ('validate', 'seed', 'trust'):
            with self.subTest(where=where):
                code, out, err, calls = self.diagnostic(where=where)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertEqual(err, 'REPLAY_ROOT_BOOTSTRAP_REFUSED\n')
                self.assertNotIn('private', err)
                self.assertEqual(calls, {
                    'validate': ['validate'],
                    'seed': ['validate', 'seed'],
                    'trust': ['validate', 'seed', 'trust'],
                }[where])

    def test_missing_module_loader_rejected(self):
        with patch.object(boot.importlib.util, 'spec_from_file_location',
                          return_value=None):
            with self.assertRaisesRegex(
                    boot.ReplayBootstrapError,
                    'REPLAY_BOOTSTRAP_TRUST_IMPORT_FAILED'):
                boot._verify_installed_modules()

    def test_imports_trust_only_from_fixed_libexec(self):
        actions = []
        module = SimpleNamespace(
            verify_root_installation=lambda: actions.append('verify-all'))
        spec = SimpleNamespace(
            loader=SimpleNamespace(
                exec_module=lambda _: actions.append('load')))
        with (patch.object(boot.importlib.util, 'spec_from_file_location',
                           return_value=spec) as spec_call,
              patch.object(boot.importlib.util, 'module_from_spec',
                           return_value=module)):
            boot._verify_installed_modules()
        spec_call.assert_called_once_with(
            'drive_batch_replay_root_trust',
            '/usr/local/libexec/apple-sync-drive-metered/'
            'drive_batch_replay_root_trust.py')
        self.assertEqual(actions, ['load', 'verify-all'])

    def test_trust_import_exception_sanitized(self):
        def fail(_module):
            raise RuntimeError('/private/credential')
        spec = SimpleNamespace(loader=SimpleNamespace(exec_module=fail))
        with (patch.object(boot.importlib.util, 'spec_from_file_location',
                           return_value=spec),
              patch.object(boot.importlib.util, 'module_from_spec',
                           return_value=object())):
            with self.assertRaisesRegex(
                    boot.ReplayBootstrapError,
                    'REPLAY_BOOTSTRAP_TRUST_UNVERIFIED') as caught:
                boot._verify_installed_modules()
        self.assertNotIn('private', str(caught.exception))

    def test_no_live_apple_service_or_ledger_execution(self):
        source = Path(boot.__file__).read_text(encoding='utf-8')
        for forbidden in ('import subprocess', 'subprocess.run(',
                          'RcloneRCReader(', 'replay_once(',
                          'run_lifecycle(', 'BudgetLedger(',
                          'systemd-run', 'os.system('):
            self.assertNotIn(forbidden, source)

    def test_bootstrap_does_not_use_writable_checkout_in_sys_path(self):
        source = Path(boot.__file__).read_text(encoding='utf-8')
        self.assertNotIn('sys.path.insert(', source)
        self.assertNotIn('sys.path.append(', source)
        self.assertIn("argv != ('doctor',)", source)


if __name__ == '__main__':
    unittest.main()

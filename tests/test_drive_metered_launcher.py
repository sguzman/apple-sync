import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import drive_metered as launcher
import install_metered_launcher as installer
import drive_meter_budget as ledger


class MeteredLauncherTests(unittest.TestCase):
    def test_service_is_root_owned_static_manual_only(self):
        service = (ROOT / 'packaging' /
                   'apple-sync-drive-metered-manual.service').read_text()
        self.assertIn('Type=oneshot', service)
        self.assertIn('/usr/local/libexec/apple-sync-drive-metered/'
                      'metered_drive_inventory_trial.py', service)
        self.assertIn('--persistent-rc --wait-idle', service)
        self.assertNotIn('[Install]', service)
        self.assertNotIn('OnCalendar=', service)
        self.assertNotIn('Restart=always', service)

    def test_policy_is_exact_user_start_only(self):
        rule = (ROOT / 'packaging' /
                '49-apple-sync-drive-metered.rules').read_text()
        self.assertIn('action.lookup("unit") === '
                      '"apple-sync-drive-metered-manual.service"', rule)
        self.assertIn('action.lookup("verb") === "start"', rule)
        self.assertIn('subject.user === "sguzman"', rule)
        self.assertIn('subject.local && subject.active', rule)
        self.assertNotIn('subject.isInGroup("wheel")', rule)
        self.assertNotIn('action.lookup("verb") === "restart"', rule)

    def test_installer_source_and_destinations_are_fixed(self):
        self.assertEqual(installer.SOURCE, Path('/home/sguzman/Code/Sync/apple-sync'))
        copied = {target.name for _, target in installer.COPIES}
        self.assertEqual(copied, {
            'metered_drive_inventory_trial.py', 'drive_meter_budget.py',
            'apple-sync-drive-metered-manual.service',
            '49-apple-sync-drive-metered.rules',
        })
        for _, destination in installer.COPIES:
            self.assertTrue(str(destination).startswith(('/usr/local/',
                                                         '/etc/')))

    def test_installer_refuses_execution_without_admin_authorization(self):
        with (patch.object(installer.os, 'geteuid', return_value=1000),
              self.assertRaisesRegex(installer.InstallationError,
                                     'ADMIN_AUTHORIZATION_REQUIRED')):
            installer.install()

    def test_installer_refuses_other_originating_accounts(self):
        user = Mock(pw_uid=1000)
        with (patch.object(installer.os, 'geteuid', return_value=0),
              patch.object(installer.pwd, 'getpwnam', return_value=user),
              patch.dict(os.environ, {'PKEXEC_UID': '2222'}),
              self.assertRaisesRegex(installer.InstallationError,
                                     'PKEXEC_MUST_BE_INVOKED_BY_ACCOUNT_OWNER')):
            installer.install()

    def test_root_installer_rejects_symlink_destination(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'source.py'
            source.write_text('sample')
            target = root / 'important'
            target.write_text('keep')
            link = root / 'destination.py'
            link.symlink_to(target)
            with (patch.object(installer, 'check_trusted_directory'),
                  self.assertRaisesRegex(installer.InstallationError,
                                         'UNSAFE_EXISTING_INSTALL_FILE')):
                installer.atomic_install(source, link)
            self.assertEqual(target.read_text(), 'keep')

    def test_root_installer_rejects_symlink_source(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'real.py'
            source.write_text('sample')
            link = root / 'symlink.py'
            link.symlink_to(source)
            with self.assertRaisesRegex(
                    installer.InstallationError,
                    'MISSING_OR_UNSAFE_INSTALL_SOURCE'):
                installer.atomic_install(link, root / 'target.py')

    def test_atomic_file_install_has_read_only_root_owned_intent(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            source = root / 'source.py'
            source.write_text('print("safe")')
            destination = root / 'destination.py'
            with patch.object(installer, 'check_trusted_directory'):
                installer.atomic_install(source, destination)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o644)

    def test_regular_user_cannot_use_root_helper(self):
        with (patch.object(launcher.os, 'geteuid', return_value=0),
              patch('sys.argv', ['metered', 'status']),
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(launcher.main(), 2)

    def test_polkit_doctor_never_attempts_administrator_authorization(self):
        out = io.StringIO()
        with (patch.object(launcher.os, 'geteuid', return_value=1000),
              patch.object(launcher.polkit, 'doctor', return_value=True),
              patch.object(launcher, 'service_state',
                           return_value={'LoadState': 'not-found'}),
              patch.object(launcher.polkit, 'authenticate_installer') as auth,
              patch('sys.argv', ['metered', 'doctor']),
              contextlib.redirect_stdout(out)):
            self.assertEqual(launcher.main(), 0)
        auth.assert_not_called()
        self.assertIn('MANUAL SERVICE: not-found', out.getvalue())

    def test_installer_preflight_failure_never_requests_password(self):
        with (patch.object(launcher.os, 'geteuid', return_value=1000),
              patch.object(launcher.polkit, 'doctor', return_value=False),
              patch.object(launcher.polkit, 'authenticate_installer') as auth,
              patch('sys.argv', ['metered', 'install']),
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(launcher.main(), 2)
        auth.assert_not_called()

    def test_installer_uses_one_external_agent_auth_attempt(self):
        with (patch.object(launcher.os, 'geteuid', return_value=1000),
              patch.object(launcher.polkit, 'doctor', return_value=True),
              patch.object(launcher.polkit, 'authenticate_installer',
                           return_value=0) as auth,
              patch('sys.argv', ['metered', 'install']),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(launcher.main(), 0)
        auth.assert_called_once_with(launcher.INSTALLER)

    def test_start_refuses_missing_unit(self):
        with (patch.object(launcher, 'service_state',
                           return_value={'LoadState': 'not-found'}),
              patch.object(launcher.subprocess, 'run') as worker,
              self.assertRaisesRegex(launcher.LauncherError,
                                     'MANUAL_SERVICE_NOT_INSTALLED')):
            launcher.request_start()
        worker.assert_not_called()

    def test_start_only_calls_fixed_systemctl_unit(self):
        result = subprocess.CompletedProcess([], 0, '', '')
        with (patch.object(launcher, 'service_state',
                           return_value={'LoadState': 'loaded',
                                         'ActiveState': 'inactive'}),
              patch.object(launcher.subprocess, 'run',
                           return_value=result) as runner,
              contextlib.redirect_stdout(io.StringIO())):
            launcher.request_start()
        self.assertEqual(runner.call_args.args[0], [
            'systemctl', '--system', '--no-block', 'start', launcher.UNIT,
        ])

    def test_busy_service_cannot_be_started_again(self):
        with (patch.object(launcher, 'service_state',
                           return_value={'LoadState': 'loaded',
                                         'ActiveState': 'activating'}),
              patch.object(launcher.subprocess, 'run') as worker,
              self.assertRaisesRegex(launcher.LauncherError,
                                     'MANUAL_SERVICE_ALREADY_RUNNING')):
            launcher.request_start()
        worker.assert_not_called()

    def test_public_status_missing_is_not_private_ledger_access(self):
        with TemporaryDirectory() as td:
            self.assertIsNone(launcher.public_budget(Path(td) / 'missing.json'))

    def test_public_status_refuses_symlink(self):
        with TemporaryDirectory() as td:
            target = Path(td) / 'real.json'
            target.write_text('{}')
            link = Path(td) / 'status.json'
            link.symlink_to(target)
            with self.assertRaisesRegex(
                    launcher.LauncherError, 'UNTRUSTED_PUBLIC_BUDGET_STATUS'):
                launcher.public_budget(link)

    def test_public_projection_never_contains_private_paths(self):
        data = ledger._initial()
        data['pending'] = {'unit': 'apple-sync-drive-metered-abc.service',
                           'started': 101}
        # Test the serialized fields contract without writing /var/lib.
        expected_public_keys = set(launcher.REQUIRED)
        self.assertNotIn('unit', expected_public_keys)
        self.assertNotIn('started', expected_public_keys)
        self.assertNotIn('entries', expected_public_keys)
        self.assertIn('pending', expected_public_keys)

    def test_status_reads_checkpoint_with_no_apple_calls(self):
        result = subprocess.CompletedProcess([], 0, '', '')
        out = io.StringIO()
        with (patch.object(launcher, 'service_state',
                           return_value={'ActiveState': 'inactive',
                                         'Result': 'success'}),
              patch.object(launcher, 'public_budget', return_value=None),
              patch.object(launcher.subprocess, 'run',
                           return_value=result) as command,
              contextlib.redirect_stdout(out)):
            launcher.show_state()
        args = command.call_args.args[0]
        self.assertEqual(Path(args[1]).name, 'drive_inventory.py')
        self.assertIn('PUBLIC BUDGET', out.getvalue())


if __name__ == '__main__':
    unittest.main()

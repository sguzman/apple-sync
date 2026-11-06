import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import polkit_authorization as polkit


class PolkitAuthorizationTests(unittest.TestCase):
    def test_process_start_time_parses_proc_stat_with_spaces_in_comm(self):
        with TemporaryDirectory() as td:
            path = Path(td) / 'stat'
            # Fields 3 through 52, including field 22 (start time).
            items = ['S'] + [str(n) for n in range(4, 53)]
            path.write_text('1234 (process name with spaces) ' +
                            ' '.join(items))
            self.assertEqual(polkit.process_start_time(path), 22)

    def test_process_start_time_rejects_bad_input(self):
        with TemporaryDirectory() as td:
            path = Path(td) / 'stat'
            path.write_text('not a proc stat')
            with self.assertRaises(polkit.AuthorizationError):
                polkit.process_start_time(path)

    def test_doctor_is_read_only_and_prints_version(self):
        output = io.StringIO()
        with (patch.object(polkit.os, 'isatty', return_value=True),
              patch.object(Path, 'is_file', return_value=True),
              patch.object(polkit.subprocess, 'run',
                           return_value=subprocess.CompletedProcess(
                               [], 0, 'polkit 127-2\\n', '')) as run,
              patch.object(polkit.subprocess, 'Popen') as auth,
              contextlib.redirect_stdout(output)):
            self.assertTrue(polkit.doctor())
        run.assert_called_once()
        auth.assert_not_called()
        self.assertIn('POLKIT PACKAGE: polkit 127-2', output.getvalue())

    def test_doctor_refuses_missing_agent_without_prompt(self):
        output = io.StringIO()
        with (patch.object(polkit.os, 'isatty', return_value=True),
              patch.object(Path, 'is_file',
                           side_effect=[True, False]),
              patch.object(polkit.subprocess, 'Popen') as auth,
              contextlib.redirect_stdout(output)):
            self.assertFalse(polkit.doctor())
        auth.assert_not_called()
        self.assertIn('installation not ready', output.getvalue())

    def test_no_interactive_terminal_refuses_before_any_authentication(self):
        with (patch.object(polkit.os, 'isatty', return_value=False),
              patch.object(polkit.subprocess, 'Popen') as agent,
              patch.object(polkit.subprocess, 'run') as auth,
              self.assertRaisesRegex(polkit.AuthorizationError,
                                     'INTERACTIVE_TERMINAL_REQUIRED')):
            polkit.authenticate_installer(Path('/any/installer'))
        agent.assert_not_called()
        auth.assert_not_called()

    def test_missing_polkit_tools_refuses_without_prompt(self):
        with (patch.object(polkit.os, 'isatty', return_value=True),
              patch.object(Path, 'is_file', return_value=False),
              patch.object(polkit.subprocess, 'Popen') as agent,
              self.assertRaisesRegex(polkit.AuthorizationError,
                                     'POLKIT_TOOLS_UNAVAILABLE')):
            polkit.authenticate_installer(Path('/any/installer'))
        agent.assert_not_called()

    def test_registered_agent_executes_exactly_one_pkexec_without_internal(self):
        agent = Mock()
        agent.poll.return_value = None
        installer = Path('/example/installer.py')
        with (patch.object(polkit.os, 'isatty', return_value=True),
              patch.object(Path, 'is_file', return_value=True),
              patch.object(polkit, 'process_start_time', return_value=4242),
              patch.object(polkit.subprocess, 'Popen',
                           return_value=agent) as create,
              patch.object(polkit.subprocess, 'run',
                           return_value=subprocess.CompletedProcess(
                               [], 0)) as auth,
              contextlib.redirect_stdout(io.StringIO())):
            result = polkit.authenticate_installer(installer)
        self.assertEqual(result, 0)
        self.assertEqual(auth.call_count, 1)
        self.assertEqual(auth.call_args.args[0],
                         ['/usr/bin/pkexec', '--disable-internal-agent',
                          '/usr/bin/python3', str(installer)])
        command = create.call_args.args[0]
        self.assertEqual(command[:3],
                         ['/usr/bin/pkttyagent', '--process',
                          str(os.getpid()) + ',4242'])
        self.assertIn('--fallback', command)
        self.assertIn('--notify-fd', command)
        agent.terminate.assert_called_once()
        agent.wait.assert_called_once()

    def test_authentication_denied_does_not_retry(self):
        agent = Mock()
        agent.poll.return_value = 127
        with (patch.object(polkit.os, 'isatty', return_value=True),
              patch.object(Path, 'is_file', return_value=True),
              patch.object(polkit, 'process_start_time', return_value=4242),
              patch.object(polkit.subprocess, 'Popen',
                           return_value=agent),
              patch.object(polkit.subprocess, 'run',
                           return_value=subprocess.CompletedProcess(
                               [], 127)) as auth,
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(polkit.authenticate_installer(
                Path('/example/installer.py')), 127)
        auth.assert_called_once()
        agent.terminate.assert_not_called()


if __name__ == '__main__':
    unittest.main()

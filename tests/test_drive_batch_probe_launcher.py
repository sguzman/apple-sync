"""Static-only launch checks; no systemctl start or cloud operations."""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import drive_batch_probe_launcher as launcher


def state(active='inactive', loaded='loaded'):
    return {'ActiveState': active, 'LoadState': loaded, 'Result': 'success'}


class LauncherTests(unittest.TestCase):
    def test_preview_is_network_free(self):
        with (patch.object(sys, 'argv', ['probe']),
              patch.object(launcher.subprocess, 'run') as client,
              patch.object(launcher.metered, 'service_state') as check,
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(launcher.main(), 0)
        client.assert_not_called()
        check.assert_not_called()
        self.assertIn('no checkpoint writes', output.getvalue())

    def test_ready_requires_inactive_probe_and_both_scanners(self):
        calls = []
        with (patch.object(launcher.metered, 'service_state',
                           side_effect=lambda name:
                           (calls.append(name), state())[1]),
              patch.object(launcher.metered, 'public_budget',
                           return_value={'pending': False,
                                         'remaining_bytes': 40_000_000})):
            launcher.ready()
        self.assertEqual(calls, [launcher.UNIT, launcher.metered.UNIT,
                                 launcher.metered.SINGLE_UNIT])

    def test_missing_or_active_service_prevents_probe(self):
        for reply in (state(loaded='not-found'), state(active='active'),
                      state(active='activating')):
            with self.subTest(reply=reply):
                with (patch.object(launcher.metered, 'service_state',
                                   return_value=reply),
                      self.assertRaises(launcher.ProbeLaunchError)):
                    launcher.ready()

    def test_pending_ledger_disables_run(self):
        with (patch.object(launcher.metered, 'service_state',
                           return_value=state()),
              patch.object(launcher.metered, 'public_budget',
                           return_value={'pending': True,
                                         'remaining_bytes': 48_000_000}),
              self.assertRaisesRegex(launcher.ProbeLaunchError, 'BUDGET')):
            launcher.ready()

    def test_run_uses_only_static_start_verb(self):
        call = []
        with (patch.object(sys, 'argv', ['probe', 'run', '--detach']),
              patch.object(launcher, 'ready'),
              patch.object(launcher.subprocess, 'run',
                           side_effect=lambda args, **kwargs:
                           (call.append(args), Mock(returncode=0))[1]),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(launcher.main(), 0)
        self.assertEqual(call, [['systemctl', '--system', '--no-block',
                                 'start', launcher.UNIT]])

    def test_installation_manifest_contains_probe_without_timers(self):
        import install_metered_launcher as installer
        destinations = dict(installer.COPIES)
        self.assertIn('scripts/metered_drive_batch_probe.py', destinations)
        self.assertIn('packaging/apple-sync-drive-batch-probe.service', destinations)
        self.assertEqual(destinations['packaging/apple-sync-drive-batch-probe.service'],
                         installer.PROBE_UNIT)
        self.assertTrue(installer.PROBE_UNIT.name.endswith('.service'))
        package = Path(__file__).resolve().parents[1] / 'packaging'
        contents = (package / 'apple-sync-drive-batch-probe.service').read_text()
        self.assertNotIn('OnCalendar', contents)
        self.assertNotIn('WantedBy=', contents)
        self.assertIn('Type=oneshot', contents)
        self.assertNotIn('drive_inventory.py', contents)


if __name__ == '__main__':
    unittest.main()

from pathlib import Path
import unittest

ROOT = Path(__file__).parents[1]
SERVICE = ROOT / 'systemd' / 'user' / 'apple-sync-drive-inventory.service'
TIMER = ROOT / 'systemd' / 'user' / 'apple-sync-drive-inventory.timer'


class DriveInventoryTimerTests(unittest.TestCase):
    def test_service_only_resumes_inventory_without_downloads(self):
        service = SERVICE.read_text()
        self.assertIn('ConditionPathIsMountPoint=/drive', service)
        self.assertIn('ExecStart=/usr/bin/python3 %h/.local/bin/apple-sync-drive-inventory --execute --resume-only', service)
        self.assertNotIn('apple-sync-batch drive', service)
        self.assertNotIn('drive_cached_batch', service)
        self.assertNotIn('--wait-idle', service)

    def test_bounded_priority_and_timeout(self):
        service = SERVICE.read_text()
        self.assertIn('Type=oneshot', service)
        self.assertIn('TimeoutStartSec=16min', service)
        self.assertIn('Nice=15', service)
        self.assertIn('IOSchedulingClass=idle', service)

    def test_timer_is_hourly_outside_photos_main_windows(self):
        timer = TIMER.read_text()
        self.assertIn('OnCalendar=*-*-* *:05:00', timer)
        self.assertIn('Persistent=false', timer)
        self.assertIn('Unit=apple-sync-drive-inventory.service', timer)

    def test_installer_links_new_units_but_does_not_enable_them(self):
        installer = (ROOT / 'scripts' / 'install.sh').read_text()
        self.assertIn('apple-sync-drive-inventory', installer)
        self.assertIn('photos-inventory drive-inventory', installer)
        self.assertIn('systemctl --user daemon-reload', installer)
        self.assertNotIn('systemctl --user enable --now', installer.split("printf")[0])

    def test_unrestricted_drive_archive_remains_blocked(self):
        archive = (ROOT / 'scripts' / 'archive_batch.py').read_text()
        self.assertIn("kind == 'drive' and execute", archive)


if __name__ == '__main__':
    unittest.main()

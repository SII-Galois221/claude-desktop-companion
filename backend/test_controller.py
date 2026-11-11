import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, Mock
import controller as c
import subprocess

class StatusTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.raw = {'at': self.now.isoformat(), 'controller_pid': 31,
                    'monitor_mode': 'apply', 'control_generation': 'a',
                    'paused': False, 'state': 'healthy', 'healthy_verified': True}
        self.control = {'schema': 1, 'paused': False, 'generation': 'a'}

    def status(self, **kwargs):
        return c.compose_status(kwargs.get('installed', True), kwargs.get('pid', 31),
                                kwargs.get('raw', self.raw), kwargs.get('control', self.control), self.now)

    def test_verified_current_apply_can_be_green(self):
        self.assertTrue(self.status()['healthy_verified'])

    def test_old_or_different_process_never_green(self):
        for pid in (None, 32):
            self.assertFalse(self.status(pid=pid)['healthy_verified'])
        self.raw['at'] = (self.now - timedelta(seconds=13)).isoformat()
        self.assertEqual(self.status()['state'], 'stale')

    def test_future_or_corrupt_timestamp_never_green(self):
        for stamp in ((self.now + timedelta(seconds=1)).isoformat(), 'bad', None):
            self.raw['at'] = stamp
            self.assertFalse(self.status()['healthy_verified'])

    def test_pending_pause_ack_is_not_paused_or_green(self):
        self.control.update(paused=True, generation='b')
        status = self.status()
        self.assertEqual(status['state'], 'pausing')
        self.assertFalse(status['healthy_verified'])

    def test_resume_cannot_reuse_old_healthy(self):
        self.control['generation'] = 'b'
        self.assertEqual(self.status()['state'], 'resuming')
        self.assertFalse(self.status()['healthy_verified'])

    def test_unknown_mode_control_and_installation_fail_closed_for_display(self):
        self.raw['monitor_mode'] = 'dry-run'
        self.assertFalse(self.status()['healthy_verified'])
        self.raw['monitor_mode'] = 'apply'
        self.assertFalse(self.status(control={})['healthy_verified'])
        self.assertFalse(self.status(installed=False)['healthy_verified'])

class ActivationTests(unittest.TestCase):
    def test_service_file_is_not_a_login_item(self):
        self.assertEqual(c.PLIST.parent, c.RUNTIME)
        self.assertNotEqual(c.PLIST.parent, c.LEGACY_PLIST.parent)

    @patch.object(c, 'remove_legacy_login_item')
    @patch('watchdog_core.get_process', return_value=None)
    @patch.object(c, 'read_json', return_value={})
    @patch.object(c, 'set_paused')
    @patch.object(c, 'PLIST')
    @patch.object(c, 'service_pid', return_value=None)
    @patch.object(c, 'run', side_effect=subprocess.TimeoutExpired('bootout', 8))
    def test_stop_timeout_still_verifies_actual_shutdown(self, run, pid, plist, pause, read, proc, legacy):
        self.assertTrue(c.stop_monitor()['ok'])
        plist.unlink.assert_called_once_with(missing_ok=True)

    @patch.object(c, 'claude_running', return_value=True)
    @patch.object(c, 'configuration_applied', return_value=False)
    @patch.object(c, 'run')
    def test_first_start_requests_restart_before_any_mutation(self, run, *_):
        self.assertTrue(c.start()['needs_configuration'])
        run.assert_not_called()

    @patch.object(c, 'remove_legacy_login_item')
    @patch.object(c, 'gui_owner', return_value=123)
    @patch.object(c, 'write_launchagent')
    @patch.object(c, 'set_paused')
    @patch.object(c, 'copy_runtime')
    @patch.object(c, 'run', return_value=Mock(returncode=0))
    @patch.object(c, 'service_pid', return_value=None)
    @patch.object(c, 'configuration_applied', return_value=True)
    def test_recovers_partial_install_without_reapplying_existing_config(self, _, pid, run, copy, pause, plist, owner, legacy):
        self.assertTrue(c.start()['ok'])
        self.assertFalse(any('apply_setup.py' in str(call) for call in run.call_args_list))
        copy.assert_called_once()
        pause.assert_called_once_with(False)

    @patch.object(c, 'gui_owner', return_value=123)
    @patch.object(c, 'copy_runtime')
    @patch.object(c, 'run', return_value=Mock(returncode=0))
    @patch.object(c, 'service_pid', side_effect=[None, 81])
    @patch.object(c, 'configuration_applied', return_value=True)
    def test_does_not_replace_code_if_old_monitor_remains(self, _, pid, run, copy, owner):
        self.assertFalse(c.start()['ok'])
        copy.assert_not_called()

if __name__ == '__main__':
    unittest.main()

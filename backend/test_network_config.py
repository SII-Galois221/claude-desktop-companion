import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import network_config as n
import controller
from daemon import Config
from flows import FlowInspector


class NetworkConfigTests(unittest.TestCase):
    def test_loopback_only_no_credentials_no_paths(self):
        for value in ('https://127.0.0.1:9999', 'http://example.com:80',
                      'http://192.0.2.1:80', 'http://user:pass@127.0.0.1:80',
                      'http://127.0.0.1:80/path', 'http://127.0.0.1:80#f',
                      'http://127.0.0.1:0', 'http://127.0.0.1', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                n.proxy_endpoint(value)
        self.assertEqual(n.proxy_endpoint('http://127.0.0.1:9999'), ('127.0.0.1', 9999))
        self.assertEqual(n.proxy_endpoint('http://[::1]:9999'), ('::1', 9999))

    def test_initializer_exclusively_writes_manifest_and_detects_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            trusted = base/'proxy-config.yaml'
            trusted.write_bytes(b'# synthetic test configuration\n')
            original = trusted.read_bytes()
            dest = base/'private/network.json'
            args = argparse.Namespace(proxy_url='http://127.0.0.1:9999',
                controller_socket='/tmp/example-test.sock', expected_node='Example node',
                expected_ip='192.0.2.1', system_proxy_url=None, watch_file=[str(trusted)])
            self.assertTrue(n.initialize(args, dest)['ok'])
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
            self.assertEqual(dest.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(trusted.read_bytes(), original)
            config = Config(dest)
            self.assertTrue(config.check_hashes())
            self.assertIsNone(config.system_proxy)
            with self.assertRaises(FileExistsError):
                n.initialize(args, dest)
            trusted.write_bytes(original + b'# changed\n')
            self.assertFalse(config.check_hashes())

    def test_no_config_start_never_touches_processes(self):
        with patch.object(controller, 'configuration_applied', return_value=False), \
             patch.object(controller, 'gui_owner') as owner, \
             patch.object(controller, 'run') as commands:
            result = controller.start()
            self.assertTrue(result['ok'])
            self.assertTrue(result['needs_configuration'])
            owner.assert_not_called()
            commands.assert_not_called()

    def test_watched_symlink_repoint_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            original, changed, link = base/'first.yaml', base/'second.yaml', base/'active.yaml'
            original.write_bytes(b'original trusted config')
            changed.write_bytes(b'different config')
            link.symlink_to(original)
            dest = base/'private/network.json'
            args = argparse.Namespace(proxy_url='http://127.0.0.1:9999',
                controller_socket='/tmp/example-test.sock', expected_node='Example node',
                expected_ip='192.0.2.1', system_proxy_url=None, watch_file=[str(link)])
            n.initialize(args, dest)
            config = Config(dest)
            self.assertEqual(config.files[0]['path'], str(link.absolute()))
            self.assertTrue(config.check_hashes())
            link.unlink()
            link.symlink_to(changed)
            self.assertFalse(config.check_hashes())
            self.assertEqual(original.read_bytes(), b'original trusted config')

    def test_unconfigured_inspection_does_not_read_controller(self):
        with patch('flows.read_connections') as reader:
            data = FlowInspector().inspect(processes=[], socket_reader=lambda pids: ([], True))
            reader.assert_not_called()
            self.assertFalse(data['controller_available'])

    def test_old_egress_cache_cannot_validate_new_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base/'manifest.json').write_text('{}')
            (base/'audit').mkdir()
            (base/'audit/egress.json').write_text(json.dumps({
                'status': 'verified', 'checked_at': datetime.now(timezone.utc).isoformat(),
                'config_fingerprint': 'old-configuration'}))
            with patch.object(controller, 'ROOT', base), \
                 patch.object(controller, 'RUNTIME', base), \
                 patch.object(controller, 'STATE', base/'state'), \
                 patch.object(controller, 'is_installed', return_value=True), \
                 patch.object(controller, 'load_network', return_value={
                     'proxy_url': 'http://127.0.0.1:9999', 'expected_ip': '192.0.2.1'}), \
                 patch.object(controller, 'run', return_value=Mock(stdout='', returncode=1)) as probe:
                result = controller.inspect_egress()
                self.assertEqual(result['status'], 'unverified')
                probe.assert_called_once()

    def test_trust_manifest_cannot_be_empty_or_use_direct(self):
        base = {'schema': 1, 'proxy_url': 'http://127.0.0.1:9999',
                'controller_socket': '/tmp/example-test.sock', 'expected_node': 'Example node',
                'expected_ip': '192.0.2.1', 'expected_files': [
                    {'path': '/tmp/example-test.yaml', 'sha256': '0' * 64}]}
        n.validate(base)
        for changed in ({'expected_files': []}, {'expected_node': 'DIRECT'},
                        {'expected_ip': 'example.com'}, {'controller_socket': 'relative.sock'},
                        {'expected_files': base['expected_files'] * 2}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                n.validate({**base, **changed})


if __name__ == '__main__':
    unittest.main()

import contextlib
import hashlib
import io
import json
from concurrent.futures import Future
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from daemon import (Config, Control, OwnerGuard, OWNER_SUFFIX, PersistentSignaller, ProbeWork, Shutdown, State,
                    fast_check, probe_round, read_control, run, schedule_after_result, status_payload)
from watchdog_core import Process, ROOT, CODE_BASE, request_sigterm


def running_state():
    s = State()
    s.control(Control(False, 1, 'initial'))
    return s


class OwnerTests(unittest.TestCase):
    def owner_process(self, started=(10, 20), uid=501, path=None):
        return Process(76543, 1, uid, started, path or '/Applications' + OWNER_SUFFIX)

    def test_optional_owner_does_not_read_any_process(self):
        with patch('daemon.get_process') as read:
            self.assertTrue(OwnerGuard(None, read_process=read, uid=501).alive())
            read.assert_not_called()

    def test_owner_lifetime_and_pid_reuse(self):
        current = [self.owner_process()]
        owner = OwnerGuard(76543, read_process=lambda _: current[0], uid=501)
        self.assertTrue(owner.alive())
        current[0] = None
        self.assertFalse(owner.alive())
        current[0] = self.owner_process(started=(11, 20))
        self.assertFalse(owner.alive())  # Same PID is not the same GUI instance.

    def test_owner_rejects_wrong_executable_or_user(self):
        for process in [self.owner_process(uid=502),
                        self.owner_process(path='/usr/bin/python3'),
                        self.owner_process(path='/Applications/Fake.app/Contents/MacOS/ClaudeResidentialGuard')]:
            owner = OwnerGuard(process.pid, read_process=lambda _: process, uid=501)
            self.assertFalse(owner.alive())

    def test_owner_leaving_blocks_signal_without_touching_claude(self):
        current = [self.owner_process()]
        owner = OwnerGuard(76543, read_process=lambda _: current[0], uid=501)
        claude = Process(12345, 1, 501, (1, 2), ROOT)
        with patch('daemon.OneShotSignaller') as ctor:
            shutdown = Shutdown(True)
            current[0] = None
            shutdown.step([claude], 0, allowed=owner.alive)
            ctor.return_value.request.assert_not_called()

    def test_dead_owner_returns_success_and_writes_stopped_without_monitoring(self):
        dead = OwnerGuard(76543, read_process=lambda _: None, uid=501)
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()), \
             patch('daemon.OwnerGuard', return_value=dead), \
             patch('daemon.signal.signal'), patch('daemon.snapshot') as inventory, \
             patch('daemon.Shutdown.step') as shutdown, patch('daemon.probe_round') as probe:
            self.assertEqual(run(None, True, Path(tmp), owner_pid=76543), 0)
            status = json.loads((Path(tmp) / 'status.json').read_text())
            self.assertEqual(status['state'], 'stopped')
            self.assertFalse(status['healthy_verified'])
            self.assertEqual(status['owner_pid'], 76543)
            inventory.assert_not_called()
            shutdown.assert_not_called()
            probe.assert_not_called()

    def test_status_exposes_exact_owner_birth_identity(self):
        process = self.owner_process()
        owner = OwnerGuard(process.pid, read_process=lambda _: process, uid=501)
        status = status_payload(running_state(), [], False, True, 'start', owner=owner)
        self.assertEqual(status['owner_pid'], process.pid)
        self.assertEqual(status['owner_started'], [10, 20])


class StateTests(unittest.TestCase):
    def test_probe_scheduling_is_start_to_start(self):
        self.assertEqual(schedule_after_result(10, 10, True, 0), 10)
        self.assertEqual(schedule_after_result(10, 2, True, 0), 10)
        self.assertEqual(schedule_after_result(10, 6, False, 0), 36)
        self.assertEqual(schedule_after_result(10, 6, False, 1), 16)

    def test_two_failures_then_two_successes(self):
        s = running_state()
        s.result(False, 1)
        self.assertIsNone(s.fault)
        s.result(False, 6)
        self.assertEqual(s.fault, 'egress_unavailable')
        s.result(True, 11)
        self.assertIsNotNone(s.fault)
        s.result(True, 16)
        self.assertIsNone(s.fault)

    def test_local_fault_cannot_be_cleared_by_probe(self):
        s = running_state()
        s.local('configuration_changed')
        s.local('configuration_changed')
        s.result(True, 1)
        s.result(True, 6)
        self.assertEqual(s.fault, 'configuration_changed')
        s.local(None)
        s.result(True, 11)
        self.assertIsNotNone(s.fault)
        s.result(True, 16)
        self.assertIsNone(s.fault)

    def test_wake_rechecks_without_inventing_fault(self):
        s = running_state()
        s.result(True, 0)
        s.result(False, 1)
        s.wake()
        self.assertIsNone(s.last_result)
        self.assertEqual(s.failures, 0)
        self.assertEqual(s.recovery_successes, 0)
        self.assertIsNone(s.fault)
        s.result(False, 5)
        self.assertIsNone(s.fault)
        s.result(False, 10)
        self.assertEqual(s.fault, 'egress_unavailable')

    def test_wake_preserves_existing_real_fault(self):
        s = running_state()
        s.local('configuration_changed')
        s.local('configuration_changed')
        s.wake()
        self.assertEqual(s.fault, 'configuration_changed')
        self.assertEqual(s.local_fault, 'configuration_changed')
        self.assertIsNone(s.last_result)

    def test_pause_resume_and_generation_invalidate_old_result(self):
        s = running_state()
        s.local(None, 0)
        old_epoch = s.epoch
        s.result(True, 0, epoch=old_epoch)
        self.assertTrue(s.healthy_verified(True, 0))
        s.control(Control(True, 2, 'pause'))
        self.assertEqual(s.name(True, now=0), 'paused')
        self.assertFalse(s.result(True, 1, epoch=old_epoch))
        self.assertFalse(s.healthy_verified(True, 1))
        s.control(Control(False, 3, 'resume'))
        self.assertEqual(s.control_generation, 3)
        self.assertEqual(s.name(True, now=1), 'checking')
        self.assertFalse(s.result(True, 1, epoch=old_epoch))
        s.result(True, 2, epoch=s.epoch)
        self.assertFalse(s.healthy_verified(True, 2))  # Local validation still required.
        s.local(None, 2)
        self.assertTrue(s.healthy_verified(True, 2))

    def test_wake_invalidates_pending_epoch(self):
        s = running_state()
        old = s.epoch
        s.wake()
        self.assertFalse(s.result(True, 1, epoch=old))
        self.assertIsNone(s.last_probe_at)

    def test_first_failed_probe_cannot_be_healthy(self):
        s = running_state()
        s.local(None, 0)
        s.result(False, 0)
        self.assertEqual(s.name(True, now=0), 'probe_retry')
        self.assertFalse(s.healthy_verified(True, 0))

    def test_status_contract_idle_stale_stopped_never_green(self):
        s = running_state()
        s.local(None, 0)
        s.result(True, 0, at='2026-11-11T00:00:00+00:00')
        payload = status_payload(s, [1], False, True, 'start', now=0)
        self.assertTrue(payload['healthy_verified'])
        for key in ['monitor_mode', 'apply', 'paused', 'control_generation', 'last_probe_at',
                    'controller_pid', 'started_at', 'at']:
            self.assertIn(key, payload)
        self.assertFalse(status_payload(s, [], False, True, 'start', now=0)['healthy_verified'])
        self.assertEqual(status_payload(s, [], False, True, 'start', now=0)['state'], 'idle')
        self.assertFalse(s.healthy_verified(True, 31))
        s.inventory_ok = False
        self.assertFalse(s.healthy_verified(True, 0))
        s.inventory_ok = True
        s.stopped = True
        self.assertEqual(s.name(True, now=0), 'stopped')
        self.assertFalse(s.healthy_verified(True, 0))


class ProbeTests(unittest.TestCase):
    def test_success_uses_only_one_head(self):
        calls = []
        def probe(_proxy, url, **_):
            calls.append(url)
            return True
        self.assertTrue(probe_round('test', threading.Event(), probe=probe))
        self.assertEqual(len(calls), 1)
        self.assertIn('gstatic', calls[0])

    def test_fallback_only_after_failure_and_total_budget_ten(self):
        calls, clock = [], [0]
        def probe(_proxy, url, timeout, **_):
            calls.append((url, timeout))
            clock[0] += timeout
            return False
        self.assertFalse(probe_round('test', threading.Event(), probe=probe, clock=lambda: clock[0]))
        self.assertEqual(len(calls), 2)
        self.assertIn('apple', calls[1][0])
        self.assertEqual(clock[0], 10)

    def test_pause_cancellation_prevents_fallback(self):
        stop, calls = threading.Event(), []
        def probe(*_, **__):
            calls.append(1)
            stop.set()
            return False
        self.assertIsNone(probe_round('test', stop, probe=probe))
        self.assertEqual(calls, [1])

    def test_old_future_success_is_discarded(self):
        work = ProbeWork()
        work.epoch = 1
        work.cancel_event = threading.Event()
        work.future = Future()
        work.future.set_result(True)
        self.assertIsNone(work.take(2))
        self.assertIsNone(work.future)

    def test_cancelled_future_cannot_turn_healthy(self):
        work = ProbeWork()
        work.epoch = 1
        work.cancel_event = threading.Event()
        work.future = Future()
        work.future.set_result(True)
        work.cancel()
        self.assertIsNone(work.take(1))


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def config(self):
        trusted = self.base / 'trusted.txt'
        trusted.write_text('pinned')
        manifest = self.base / 'manifest.json'
        manifest.write_text(json.dumps({'schema': 1, 'proxy_url': 'http://127.0.0.1:17897',
            'controller_socket': '/tmp/test-only.sock',
            'system_proxy_url': 'http://127.0.0.1:7897',
            'expected_node': 'Example residential node', 'expected_ip': '192.0.2.10', 'expected_files': [
            {'path': str(trusted), 'sha256': hashlib.sha256(b'pinned').hexdigest()}]}))
        return Config(manifest), trusted

    def test_config_and_manifest_mutation_not_trusted(self):
        config, trusted = self.config()
        self.assertTrue(config.check_hashes())
        trusted.write_text('different')
        self.assertFalse(config.check_hashes())
        trusted.write_text('pinned')
        config.path.write_text(config.path.read_text() + '\n')
        self.assertFalse(config.check_hashes())

    def test_hash_mismatch_does_not_probe_socket(self):
        config, trusted = self.config()
        trusted.write_text('changed')
        with patch('daemon.socket.create_connection') as net:
            self.assertEqual(fast_check(config), 'configuration_changed')
            net.assert_not_called()

    def test_system_proxy_off_does_not_continue(self):
        config, _ = self.config()
        with patch('daemon.subprocess.run') as command, patch('daemon.socket.create_connection') as net:
            command.return_value.stdout = '<dictionary> {\n HTTPEnable : 0\n}'
            self.assertEqual(fast_check(config), 'system_proxy_changed')
            net.assert_not_called()

    def test_term_only_once_even_after_monitor_restart(self):
        root = Process(12345, 1, 501, (1, 2), ROOT)
        calls = []
        ledger = self.base / 'signals.json'
        for _ in range(2):
            signaler = PersistentSignaller(ledger)
            signaler.request([root], apply=True, read_process=lambda _: root,
                             send=lambda *args: calls.append(args))
        self.assertEqual(len(calls), 1)  # Fake signal callback only.

    def test_shutdown_root_first_children_after_three_seconds(self):
        root = Process(12345, 1, 501, (1, 2), ROOT)
        child = Process(12346, 12345, 501, (1, 3),
            CODE_BASE + '/2.1.321/build/claude.app/Contents/MacOS/claude')
        calls = []
        class FakeSignaller:
            def __init__(self): self.sent = set()
            def request(self, targets, **_):
                new = [p for p in targets if p.identity not in self.sent]
                calls.extend(p.pid for p in new)
                self.sent.update(p.identity for p in new)
                return len(new), 0
        guard = Shutdown(False, FakeSignaller())
        with contextlib.redirect_stdout(io.StringIO()):
            guard.step([root, child], 0)
            self.assertEqual(calls, [12345])
            guard.step([root, child], 2)
            self.assertEqual(calls, [12345])
            guard.step([root, child], 3)
            self.assertEqual(calls, [12345, 12346])
            guard.step([root, child], 20)
            self.assertEqual(calls, [12345, 12346])

    def test_control_missing_invalid_pause_safely(self):
        path = self.base / 'control.json'
        self.assertTrue(read_control(path).paused)
        path.write_text('{')
        self.assertEqual(read_control(path).error, 'control_invalid')
        path.write_text(json.dumps({'schema': 1, 'paused': False, 'generation': 'ack-2'}))
        control = read_control(path)
        self.assertFalse(control.paused)
        self.assertEqual(control.generation, 'ack-2')

    def test_paused_shutdown_never_calls_signaller(self):
        root = Process(12345, 1, 501, (1, 2), ROOT)
        with patch('daemon.OneShotSignaller') as ctor:
            shutdown = Shutdown(True)
            shutdown.step([root], 0, allowed=lambda: False)
            ctor.return_value.request.assert_not_called()

    def test_permission_revoked_immediately_before_signal(self):
        root = Process(12345, 1, 501, (1, 2), ROOT)
        checks, sent = iter([True, False]), []
        request_sigterm([root], apply=True, allowed=lambda: next(checks),
                        read_process=lambda _: root, send=lambda *args: sent.append(args))
        self.assertEqual(sent, [])

    def test_cancelled_signal_identity_remains_eligible_after_resume(self):
        root = Process(12345, 1, 501, (1, 2), ROOT)
        checks, sent = iter([True, False]), []
        signer = PersistentSignaller(self.base / 'signals.json')
        signer.request([root], apply=True, allowed=lambda: next(checks),
                       read_process=lambda _: root, send=lambda *args: sent.append(args))
        self.assertNotIn(root.identity, signer.sent)
        self.assertEqual(sent, [])
        signer.request([root], apply=True, allowed=lambda: True,
                       read_process=lambda _: root, send=lambda *args: sent.append(args))
        self.assertEqual(len(sent), 1)

    def test_pause_during_intent_save_rolls_back_unsent_identity(self):
        root = Process(12345, 1, 501, (1, 2), ROOT)
        signer = PersistentSignaller(self.base / 'signals.json')
        sent, permit = [], [True]
        original_save = signer.save
        def pause_after_save():
            original_save()
            permit[0] = False
        with patch.object(signer, 'save', pause_after_save):
            signer.request([root], apply=True, allowed=lambda: permit[0],
                           read_process=lambda _: root, send=lambda *args: sent.append(args))
        self.assertEqual(sent, [])
        self.assertNotIn(root.identity, signer.sent)
        self.assertNotIn(root.identity, PersistentSignaller(signer.path).sent)


if __name__ == '__main__':
    unittest.main()

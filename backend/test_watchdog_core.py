import unittest
import subprocess
import threading
from unittest.mock import patch
from watchdog_core import GuardState, Process, Targets, ROOT, CODE_BASE, request_sigterm, OneShotSignaller
from watchdog_core import probe_pinned_listener


class ProbeCancellationTests(unittest.TestCase):
    def test_pause_terminates_only_owned_curl(self):
        cancelled = threading.Event()
        class FakeCurl:
            returncode = None
            terminated = False
            def communicate(self, timeout):
                cancelled.set()
                raise subprocess.TimeoutExpired('curl', timeout)
            def poll(self): return self.returncode
            def terminate(self):
                self.terminated = True
                self.returncode = -15
            def wait(self, timeout): return self.returncode
        child = FakeCurl()
        with patch('watchdog_core.subprocess.Popen', return_value=child) as launch:
            self.assertFalse(probe_pinned_listener(cancel_event=cancelled))
        self.assertTrue(child.terminated)
        args = launch.call_args.args[0]
        self.assertEqual(args[:2], ['/usr/bin/curl', '--disable'])
        self.assertIn('--head', args)
        self.assertNotIn('--insecure', args)

    def test_already_paused_starts_no_curl(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch('watchdog_core.subprocess.Popen') as launch:
            self.assertFalse(probe_pinned_listener(cancel_event=cancelled))
            launch.assert_not_called()


class TestGuard(unittest.TestCase):
    def test_start_unverified_and_two_failures_latch(self):
        s = GuardState()
        self.assertFalse(s.verified)
        s.probe_result(False, 0)
        self.assertIsNone(s.fault)
        s.probe_result(False, 5)
        self.assertEqual(s.fault, 'egress_probe_failed')
        s.probe_result(True, 10)
        self.assertFalse(s.verified)  # No automatic unlatch/relaunch.

    def test_success_resets_consecutive_failures(self):
        s = GuardState()
        s.probe_result(False, 0)
        s.probe_result(True, 5)
        s.probe_result(False, 10)
        self.assertIsNone(s.fault)
        self.assertTrue(s.verified)

    def test_fast_fault_and_stale_health(self):
        s = GuardState()
        s.probe_result(True, 0)
        s.stale_check(17)
        self.assertEqual(s.fault, 'health_stale')
        s = GuardState()
        s.fast_result(False)
        self.assertEqual(s.fault, 'local_guard_failed')


class TestOwnership(unittest.TestCase):
    def test_pause_cancellation_does_not_consume_one_shot(self):
        old = self.fixture()[0]
        checks, calls = iter([True, False]), []
        helper = OneShotSignaller()
        helper.request([old], apply=True, allowed=lambda: next(checks),
                       read_process=lambda _: old, send=lambda *args: calls.append(args))
        self.assertNotIn(old.identity, helper.sent)
        helper.request([old], apply=True, allowed=lambda: True,
                       read_process=lambda _: old, send=lambda *args: calls.append(args))
        self.assertEqual(len(calls), 1)

    def fixture(self):
        return [
            Process(1, 0, 501, (1, 0), ROOT),
            Process(2, 1, 501, (1, 0), '/Applications/Claude.app/Contents/Helpers/disclaimer'),
            Process(3, 2, 501, (1, 0), CODE_BASE + '/2.1.321/build/claude.app/Contents/MacOS/claude'),
            Process(4, 1, 501, (1, 0), '/bin/bash'),
            Process(5, 0, 501, (1, 0), '/opt/homebrew/bin/claude'),
            Process(6, 99, 501, (1, 0), '/Applications/Claude.app/Contents/Helpers/chrome-native-host'),
            Process(7, 0, 502, (1, 0), ROOT),
        ]

    def test_descendants_only_and_no_unrelated_cli(self):
        self.assertEqual({p.pid for p in Targets(501).update(self.fixture())}, {1, 2, 3})

    def test_tracked_orphan_remains_but_pid_reuse_does_not(self):
        t = Targets(501)
        t.update(self.fixture())
        old = self.fixture()[2]
        orphan = Process(old.pid, 0, old.uid, old.started, old.path)
        self.assertEqual([p.pid for p in t.update([orphan])], [3])
        reused = Process(old.pid, 0, old.uid, (9, 0), old.path)
        self.assertEqual(t.update([reused]), [])

    def test_dry_run_and_identity_recheck(self):
        old = self.fixture()[0]
        calls = []
        fake_send = lambda *args: calls.append(args)
        request_sigterm([old], read_process=lambda _: old, send=fake_send)
        self.assertEqual(calls, [])
        reused = Process(old.pid, old.ppid, old.uid, (2, 0), old.path)
        request_sigterm([old], apply=True, read_process=lambda _: reused, send=fake_send)
        self.assertEqual(calls, [])
        request_sigterm([old], apply=True, read_process=lambda _: old, send=fake_send)
        self.assertEqual(len(calls), 1)  # Stub only, no signal sent.

    def test_one_shot_prevents_second_term_abrupt_exit(self):
        old = self.fixture()[0]
        calls = []
        helper = OneShotSignaller()
        for _ in range(3):
            helper.request([old], apply=True, read_process=lambda _: old,
                           send=lambda *args: calls.append(args))
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Local Claude residential-proxy monitor. Dry-run unless --apply is explicit.

The fixed proxy listener is the no-fallback boundary; this monitor requests
application exit after detection. It is not a firewall or immediate kill switch.
No shell commands, user messages, credentials, IP addresses, or argv are logged.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time

from watchdog_core import (
    CODE_PATTERN, ROOT, OneShotSignaller, Targets, probe_pinned_listener, snapshot,
    request_sigterm, get_process,
)

PROBE_URLS = ('https://www.gstatic.com/generate_204',
              'https://www.apple.com/library/test/success.html')
from network_config import OWNER_SUFFIX, proxy_endpoint, validate


class OwnerGuard:
    """Bind this monitor to one same-user menu-bar app process instance."""
    def __init__(self, pid=None, read_process=get_process, uid=None):
        self.pid = pid
        self.read_process = read_process
        self.uid = os.getuid() if uid is None else uid
        self.identity = None
        if pid is not None and type(pid) is int and pid > 0:
            process = self._read()
            if self._eligible(process):
                self.identity = process.identity

    def _read(self):
        try:
            return self.read_process(self.pid)
        except (OSError, ValueError):
            return None

    def _eligible(self, process):
        return bool(process is not None and process.uid == self.uid
                    and process.path.endswith(OWNER_SUFFIX))

    def alive(self):
        if self.pid is None:
            return True  # Optional for non-GUI/manual use.
        if self.identity is None:
            return False
        process = self._read()
        return bool(self._eligible(process) and process.identity == self.identity)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


@dataclass(frozen=True)
class Control:
    paused: bool
    generation: str | int | None
    fingerprint: str
    error: str | None = None


def read_control(path):
    """Unreadable controls pause safely; the UI must use atomic replacement."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return Control(True, None, 'unavailable', 'control_unavailable')
    fingerprint = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw)
        generation = data.get('generation')
        if data.get('schema') != 1 or type(data.get('paused')) is not bool:
            raise ValueError('invalid control')
        if generation is not None and (type(generation) not in (str, int) or
                                       isinstance(generation, str) and len(generation) > 128):
            raise ValueError('invalid generation')
        return Control(data['paused'], generation, fingerprint)
    except (ValueError, TypeError, AttributeError):
        return Control(True, None, fingerprint, 'control_invalid')


def log(event, **counts):
    # Only code-owned event strings and scalar counters; no exception text.
    print(json.dumps({'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                      'event': event, **counts}, ensure_ascii=True), flush=True)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=.35):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def digest(path):
    hasher = hashlib.sha256()
    with open(path, 'rb') as src:
        while part := src.read(128 * 1024):
            hasher.update(part)
    return hasher.hexdigest()


class Config:
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        raw = self.path.read_bytes()
        self.fingerprint = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw)
        validate(data)
        self.proxy = data['proxy_url']
        self.socket = data['controller_socket']
        self.system_proxy = data.get('system_proxy_url')
        self.files = data['expected_files']

    def check_hashes(self):
        try:
            if digest(self.path) != self.fingerprint:
                return False
            return all(digest(entry['path']) == entry['sha256'] for entry in self.files)
        except (OSError, ValueError):
            return False


def fast_check(config):
    if not config.check_hashes():
        return 'configuration_changed'
    conn = None
    try:
        if config.system_proxy:
            sysproxy = subprocess.run(['/usr/sbin/scutil', '--proxy'], capture_output=True,
                                      text=True, timeout=.5, check=True).stdout
            values = dict(re.findall(r'^\s*(HTTPEnable|HTTPProxy|HTTPPort|HTTPSEnable|HTTPSProxy|HTTPSPort|ProxyAutoConfigEnable|ProxyAutoDiscoveryEnable)\s*:\s*(.*?)\s*$', sysproxy, re.M))
            host, port = proxy_endpoint(config.system_proxy)
            expected = {'HTTPEnable': '1', 'HTTPProxy': host, 'HTTPPort': str(port),
                        'HTTPSEnable': '1', 'HTTPSProxy': host, 'HTTPSPort': str(port)}
            if any(values.get(key) != value for key, value in expected.items()) or any(
                values.get(key, '0') != '0' for key in ('ProxyAutoConfigEnable', 'ProxyAutoDiscoveryEnable')):
                return 'system_proxy_changed'
        # Numeric loopback avoids proxy-env and DNS behavior in this check.
        with socket.create_connection(proxy_endpoint(config.proxy), timeout=.25):
            pass
        conn = UnixHTTPConnection(config.socket)
        conn.request('GET', '/version', headers={'Connection': 'close'})
        response = conn.getresponse()
        if response.status != 200:
            return 'controller_unavailable'
        body = response.read(65537)
        if len(body) > 65536:
            return 'controller_unavailable'
        version = json.loads(body)
        if not isinstance(version, dict) or not isinstance(version.get('version'), str):
            return 'controller_unavailable'
    except (OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError):
        return 'local_proxy_unavailable'
    finally:
        if conn is not None:
            conn.close()
    return None


class State:
    def __init__(self):
        self.local_fault = None
        self.fault = None
        self.failures = 0
        self.recovery_successes = 0
        self.last_result = None
        self.last_probe_at = None
        self.last_probe_ok = False
        self.last_local = None
        self.local_ok = False
        self.fast_failures = 0
        self.paused = True
        self.control_generation = None
        self.control_error = None
        self.control_fingerprint = None
        self.epoch = 0
        self.stopped = False
        self.inventory_ok = True

    def invalidate(self, clear_fault=False):
        self.epoch += 1
        self.last_result = self.last_probe_at = self.last_local = None
        self.last_probe_ok = self.local_ok = False
        self.failures = self.fast_failures = self.recovery_successes = 0
        if clear_fault:
            self.local_fault = self.fault = None

    def control(self, control):
        if control.fingerprint == self.control_fingerprint:
            return False
        self.invalidate(clear_fault=True)
        self.control_fingerprint = control.fingerprint
        self.control_generation = control.generation
        self.control_error = control.error
        self.paused = control.paused or bool(control.error)
        return True

    def local(self, fault, now=None):
        self.last_local = time.monotonic() if now is None else now
        self.local_ok = fault is None
        self.fast_failures = self.fast_failures + 1 if fault else 0
        previous = self.local_fault
        if fault and self.fast_failures >= 2:
            self.local_fault = fault
            self.fault = fault
            self.recovery_successes = 0
        elif not fault:
            self.local_fault = None
        if not fault and previous:
            self.recovery_successes = 0

    def result(self, ok, now, epoch=None, at=None):
        if self.paused or (epoch is not None and epoch != self.epoch):
            return False
        self.last_result = now
        self.last_probe_at = at or utc_now()
        self.last_probe_ok = ok
        if ok:
            self.failures = 0
            if self.fault and not self.local_fault:
                self.recovery_successes += 1
                if self.recovery_successes >= 2:
                    self.fault = None
                    self.recovery_successes = 0
        else:
            self.failures += 1
            self.recovery_successes = 0
            if self.failures >= 2 and not self.local_fault:
                self.fault = 'egress_unavailable'
        return True

    def wake(self):
        # Old health is never accepted after a scheduling/sleep discontinuity.
        self.invalidate(clear_fault=False)

    def healthy_verified(self, active, now=None):
        now = time.monotonic() if now is None else now
        return bool(active and self.inventory_ok and not self.paused and not self.stopped and not self.fault
                    and not self.control_error and self.local_ok and self.last_probe_ok
                    and self.last_result is not None and 0 <= now - self.last_result <= 30
                    and self.last_local is not None and 0 <= now - self.last_local <= 4)

    def name(self, active, pending=False, now=None):
        if self.stopped:
            return 'stopped'
        if self.control_error:
            return self.control_error
        if self.paused:
            return 'paused'
        if not self.inventory_ok:
            return 'process_inventory_unavailable'
        if self.fault:
            return self.fault
        if not active:
            return 'idle'
        if self.healthy_verified(active, now):
            return 'healthy'
        return 'probe_retry' if self.failures else 'checking'


def probe_round(proxy, cancel_event, probe=probe_pinned_listener, clock=time.monotonic):
    """One normal HEAD, then Apple only on failure; shared ten-second budget."""
    deadline = clock() + 10
    for url in PROBE_URLS:
        if cancel_event.is_set():
            return None
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        if probe(proxy, url, cancel_event=cancel_event, timeout=min(5.0, remaining)):
            return None if cancel_event.is_set() else True
    return None if cancel_event.is_set() else False


class ProbeWork:
    def __init__(self):
        self.future = None
        self.epoch = None
        self.cancel_event = None

    def cancel(self):
        if self.cancel_event is not None:
            self.cancel_event.set()
        if self.future is not None:
            self.future.cancel()

    def start(self, pool, proxy, epoch):
        if self.future is not None:
            return False
        self.epoch = epoch
        self.cancel_event = threading.Event()
        self.future = pool.submit(probe_round, proxy, self.cancel_event)
        return True

    def take(self, epoch):
        if self.future is None or not self.future.done():
            return None
        valid = self.epoch == epoch and not self.cancel_event.is_set()
        try:
            result = self.future.result() if valid else None
        except Exception:
            result = False if valid else None
        self.future = self.cancel_event = self.epoch = None
        return result


class Shutdown:
    """Root SIGTERM once; surviving owned Code SIGTERM once after three seconds.

    Electron's first SIGTERM normally requests Browser::Quit. Repeating it can
    abruptly kill the main process, so identities are remembered for this run.
    There is no SIGKILL and no retry of the same PID/birth/path instance.
    """
    def __init__(self, apply, signaller=None):
        self.apply = apply
        self.signaller = signaller or OneShotSignaller()
        self.started = {}
        self.first_fault = None
        self.reported_still_running = set()

    def step(self, targets, now, allowed=lambda: True):
        if not allowed():
            return
        if self.first_fault is None:
            self.first_fault = now
        roots = [p for p in targets if p.path == ROOT]
        new_roots = [p for p in roots if p.identity not in self.signaller.sent]
        if new_roots:
            count, failed = self.signaller.request(new_roots, apply=self.apply, allowed=allowed)
            for root in new_roots:
                if root.identity in self.signaller.sent:
                    self.started.setdefault(root.identity, now)
            log('exit_requested' if self.apply else 'would_request_exit', count=count, failed=failed)
        for root in roots:
            if root.identity in self.signaller.sent:
                self.started.setdefault(root.identity, now)
        # Preserve a short opportunity for the app to flush and stop its children.
        due = not roots or all(now - self.started.get(p.identity, now) >= 3 for p in roots)
        if due:
            code = [p for p in targets if p.path != ROOT
                    and p.identity not in self.signaller.sent]
            if code:
                count, failed = self.signaller.request(code, apply=self.apply, allowed=allowed)
                log('child_exit_requested' if self.apply else 'would_request_child_exit',
                    count=count, failed=failed)
        for root in roots:
            if now - self.started.get(root.identity, now) >= 10 and root.identity not in self.reported_still_running:
                self.reported_still_running.add(root.identity)
                log('exit_not_observed', count=1)


class PersistentSignaller(OneShotSignaller):
    """Record intent before sending, so launchd restarts cannot send TERM twice."""
    def __init__(self, path):
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            payload = json.loads(self.path.read_text())
            if payload.get('schema') != 1:
                raise ValueError('invalid signal ledger')
            for pid, born, executable in payload['identities']:
                if not isinstance(pid, int) or len(born) != 2 or not isinstance(executable, str):
                    raise ValueError('invalid signal identity')
                self.sent.add((pid, tuple(born), executable))

    def save(self):
        temp = self.path.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as dst:
            json.dump({'schema': 1, 'identities': sorted(self.sent)}, dst)
            dst.write('\n')
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temp, self.path)

    def request(self, targets, *, apply=False, **kwargs):
        allowed = kwargs.get('allowed', lambda: True)
        if not allowed():
            return 0, 0
        new = [p for p in targets if p.identity not in self.sent]
        identities = {p.pid: p.identity for p in new}
        send = kwargs.pop('send', os.kill)
        def durable_send(pid, sig):
            if not allowed():
                return
            identity = identities[pid]
            self.sent.add(identity)
            self.save()  # Intent is durable before an actual OS signal is possible.
            if not allowed():
                # A known cancellation before send must not consume this identity.
                self.sent.remove(identity)
                self.save()
                return
            if apply:
                send(pid, sig)
        # Dry-run invokes only the recording callback and never the real sender.
        return request_sigterm(new, apply=True, send=durable_send, **kwargs)


def status_payload(state, active, pending, apply, started_at, now=None, owner=None):
    mode = 'apply' if apply else 'dry-run'
    return {'schema': 1, 'mode': mode, 'monitor_mode': mode, 'apply': bool(apply),
               'state': state.name(active, pending, now), 'official_process_count': len(active),
               'probe_pending': pending, 'consecutive_probe_failures': state.failures,
               'paused': state.paused, 'control_generation': state.control_generation,
               'last_probe_at': state.last_probe_at,
               'healthy_verified': state.healthy_verified(active, now),
               'controller_pid': os.getpid(), 'started_at': started_at, 'at': utc_now(),
               'owner_pid': owner.pid if owner is not None else None,
               'owner_started': list(owner.identity[1]) if owner is not None and owner.identity else None}


def status_write(path, state, active, pending, apply, started_at, owner=None):
    payload = status_payload(state, active, pending, apply, started_at, owner=owner)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as dst:
        json.dump(payload, dst)
        dst.write('\n')
    os.replace(temp, path)


def schedule_after_result(previous_deadline, now, active, recovery_successes):
    # Active-app scheduling is START-to-START (10 s), not completion + 10 s.
    if active:
        return previous_deadline
    return now + (10 if recovery_successes else 30)


def run(config, apply, state_dir, control_path=None, owner_pid=None):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    control_path = Path(control_path) if control_path else state_dir / 'control.json'
    lock_fd = os.open(state_dir / 'guard.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log('another_monitor_is_running')
        os.close(lock_fd)
        return 2
    running = True
    def stop(_sig, _frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    state = State()
    targets = Targets()
    ledger_name = 'signals.json' if apply else 'dry-run-signals.json'
    shutdown = Shutdown(apply, PersistentSignaller(state_dir / ledger_name))
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='egress-probe')
    work = ProbeWork()
    next_probe = next_fast = next_status = 0.0
    previous_active = False
    previous_state = None
    last_mono, last_wall = time.monotonic(), time.time()
    started_at = utc_now()
    active = []
    owner = OwnerGuard(owner_pid)

    def allowed():
        # Re-read immediately before each signal, not just at the top of the loop.
        control = read_control(control_path)
        return (running and owner.alive() and not state.paused and not control.paused and not control.error
                and control.fingerprint == state.control_fingerprint)

    log('monitor_started', apply=apply)
    try:
        while running:
            if not owner.alive():
                log('owner_exited')
                break  # Finally cancels probes/writes stopped; no Claude signal.
            now, wall = time.monotonic(), time.time()
            control = read_control(control_path)
            if state.control(control):
                work.cancel()
                next_fast = next_probe = next_status = 0
            if now - last_mono > 10 or abs((wall - last_wall) - (now - last_mono)) > 10:
                state.wake()
                work.cancel()
                next_fast = next_probe = next_status = 0
            last_mono, last_wall = now, wall
            try:
                active = targets.update(snapshot())
                inventory_ok = True
            except (OSError, ValueError, subprocess.SubprocessError):
                # Publish an explicit unverified heartbeat instead of leaving green.
                inventory_ok = False
                state.local_ok = False
                state.last_local = None
                state.last_probe_ok = False
                log('process_inventory_unavailable')
            is_active = bool(active)
            state.inventory_ok = inventory_ok
            if inventory_ok and is_active and not previous_active:
                state.invalidate(clear_fault=False)
                work.cancel()
                next_probe = next_fast = next_status = 0
            previous_active = is_active
            if state.paused:
                # Cancellation event stops an already-running owned curl promptly.
                work.cancel()
                work.take(state.epoch)  # Drain only; never apply results while paused.
            else:
                if now >= next_fast:
                    state.local(fast_check(config), now)
                    next_fast = now + (1 if is_active else 5)
                result = work.take(state.epoch)
                if result is not None:
                    state.result(result, now, epoch=state.epoch)
                    next_probe = schedule_after_result(next_probe, now, is_active,
                                                       state.recovery_successes)
                    next_status = 0
                # No external traffic for healthy idle. A first recovery success
                # earns a confirmation after 10 s; otherwise idle retries every 30 s.
                needs_probe = is_active or bool(state.fault)
                if (inventory_ok and needs_probe and state.local_ok and
                    not state.local_fault and work.future is None and now >= next_probe):
                    # Catch a pause written during this iteration before starting I/O.
                    if allowed() and work.start(pool, config.proxy, state.epoch):
                        next_probe = now + 10
                if state.fault and active and inventory_ok and allowed():
                    shutdown.step(active, now, allowed=allowed)
            # A control update during local checks must be acknowledged before status
            # publication; invalidating its epoch also discards a just-finished probe.
            if not owner.alive():
                log('owner_exited')
                break
            latest = read_control(control_path)
            if state.control(latest):
                work.cancel()
                next_fast = next_probe = next_status = 0
            name = state.name(active, work.future is not None, now)
            if not inventory_ok and not state.paused:
                name = 'process_inventory_unavailable'
            if name != previous_state:
                log('state_changed', state=name, count=len(active))
                previous_state = name
                next_status = 0
            if now >= next_status:
                status_write(state_dir / 'status.json', state, active,
                             work.future is not None, apply, started_at, owner)
                next_status = now + 5
            time.sleep(max(.05, 1 - (time.monotonic() - now)))
    finally:
        work.cancel()
        state.stopped = True
        state.last_probe_ok = state.local_ok = False
        # Write stopped before waiting on worker cleanup; UI must not retain green.
        try:
            status_write(state_dir / 'status.json', state, active, False, apply, started_at, owner)
        except OSError:
            pass  # UI rejects the previous heartbeat when it becomes stale.
        pool.shutdown(wait=True, cancel_futures=True)
        os.close(lock_fd)
        log('monitor_stopped')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--check', action='store_true', help='One read-only preflight; never signals')
    parser.add_argument('--state-dir')
    parser.add_argument('--control', help='Default: STATE_DIR/control.json')
    parser.add_argument('--owner-pid', type=int,
                        help='Stop monitoring when this menu-bar app process instance exits')
    args = parser.parse_args()
    try:
        config = Config(args.config)
        if args.check:
            fault = fast_check(config)
            if fault:
                log('preflight_failed', reason=fault)
                return 1
            healthy = probe_round(config.proxy, threading.Event())
            log('preflight_healthy' if healthy else 'preflight_failed', signals_sent=0)
            return 0 if healthy else 1
        state_dir = Path(args.state_dir) if args.state_dir else config.path.parent / 'state'
        return run(config, args.apply, state_dir, args.control, args.owner_pid)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        log('monitor_failed_safely')
        return 2


if __name__ == '__main__':
    sys.exit(main())

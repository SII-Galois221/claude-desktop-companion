#!/usr/bin/env python3
"""Process identity and explicit-proxy probe helpers; no actions on import.

This is NOT a network kill switch. Caller must validate its pinned listener and
schedule separate fast checks and asynchronous HTTPS probes. No startup
grace period permits unverified traffic. Reconnection never starts Claude.
"""
from __future__ import annotations
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import subprocess
import time

ROOT = '/Applications/Claude.app/Contents/MacOS/Claude'
BUNDLE = '/Applications/Claude.app/Contents/'
CODE_BASE = str(Path.home() / 'Library/Application Support/Claude/claude-code')
CODE_PATTERN = re.compile(re.escape(CODE_BASE) + r'/[^/]+/[^/]+/claude\.app/Contents/MacOS/claude\Z')


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    uid: int
    started: tuple[int, int]
    path: str

    @property
    def identity(self):
        return (self.pid, self.started, self.path)


class BSDInfo(ctypes.Structure):
    # Matches the installed macOS SDK sys/proc_info.h struct proc_bsdinfo.
    _fields_ = [(n, ctypes.c_uint32) for n in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
        'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(n, ctypes.c_uint32) for n in (
        'nfiles', 'pgid', 'jobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64),
                ('start_usec', ctypes.c_uint64)]


def get_process(pid: int) -> Process | None:
    """Read exact executable plus creation identity; never read argv/environment."""
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    info = BSDInfo()
    if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        return None
    buf = ctypes.create_string_buffer(4096)
    if lib.proc_pidpath(pid, buf, len(buf)) <= 0:
        return None
    return Process(pid, info.ppid, info.uid,
                   (info.start_sec, info.start_usec), os.fsdecode(buf.value))


def snapshot() -> list[Process]:
    out = subprocess.run(['/bin/ps', '-axo', 'pid='], check=True,
                         capture_output=True, text=True, timeout=2).stdout
    result = []
    for value in out.split():
        process = get_process(int(value))
        if process is not None and process.uid == os.getuid():
            result.append(process)
    return result


class Targets:
    """Track Desktop-owned official executables, preserving known orphans.

    Generic node/bash/ssh/MCP processes are deliberately excluded: killing them
    is not safe based on a name match. This makes 'all descendant traffic'
    outside the promise. PID+birth+path avoids most PID-reuse mistakes, but
    check-then-signal has an unavoidable tiny race without an OS process handle.
    """
    def __init__(self, uid: int | None = None):
        self.uid = os.getuid() if uid is None else uid
        self.known = set()

    def update(self, processes: list[Process]) -> list[Process]:
        allowed = [p for p in processes if p.uid == self.uid]
        descendants = {p.pid for p in allowed if p.path == ROOT}
        while True:
            more = {p.pid for p in allowed if p.ppid in descendants}
            if more <= descendants:
                break
            descendants |= more
        def official(p):
            return p.path.startswith(BUNDLE) or bool(CODE_PATTERN.fullmatch(p.path))
        selected = [p for p in allowed if official(p) and
                    (p.pid in descendants or p.identity in self.known)]
        self.known = {p.identity for p in selected}
        return sorted(selected, key=lambda p: (p.path == ROOT, p.pid))


def request_sigterm(targets: list[Process], *, apply=False,
                    read_process=get_process, send=os.kill,
                    allowed=lambda: True) -> tuple[int, int]:
    """One-shot low-level primitive; wrap in OneShotSignaller for polling.

    Electron POSIX's first SIGTERM normally posts Browser::Quit; it immediately
    restores SIG_DFL, so a second SIGTERM can exit abruptly. Completion of the
    quit lifecycle and history flush still cannot be guaranteed. Dry-run default.
    Never uses killpg, pkill, SIGKILL, or an automatic relaunch.
    """
    attempted = 0
    failed = 0
    for old in targets:
        if not allowed():
            break
        current = read_process(old.pid)
        if current is None or current.identity != old.identity:
            continue
        if current.uid != old.uid:
            continue
        attempted += 1
        if apply:
            try:
                if not allowed():
                    break
                send(old.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                failed += 1
    return attempted, failed


class OneShotSignaller:
    """Never repeatedly signal the same process instance during polling.

    Production should signal root once, allow its normal quit cleanup a short
    bounded interval, then request SIGTERM once for any surviving tracked Code
    processes. Signal all helpers at once only if abrupt helper exits are accepted.
    The process can veto/hang; report that rather than claiming it has exited.
    """
    def __init__(self):
        self.sent = set()

    def request(self, targets, *, apply=False, read_process=get_process, send=os.kill,
                allowed=lambda: True):
        if not allowed():
            return 0, 0
        new = [p for p in targets if p.identity not in self.sent]
        identities = {p.pid: p.identity for p in new}
        def record_then_send(pid, sig):
            if not allowed():
                return
            self.sent.add(identities[pid])
            if apply:
                send(pid, sig)
        # In dry-run the callback records a simulated request without os.kill.
        return request_sigterm(new, apply=True, read_process=read_process,
                               send=record_then_send, allowed=allowed)


@dataclass
class GuardState:
    failures: int = 0
    last_success: float | None = None
    fault: str | None = None

    def fast_result(self, healthy: bool):
        # Configuration/control/listener failure is immediate and fail-closed.
        if not healthy:
            self.fault = 'local_guard_failed'

    def probe_result(self, healthy: bool, now: float):
        if healthy:
            self.failures = 0
            self.last_success = now
        else:
            self.failures += 1
            if self.failures >= 2:
                self.fault = 'egress_probe_failed'
        # A healthy probe never clears a fault latch or opens/restarts Claude.

    def stale_check(self, now: float, max_age: float = 16.0):
        if self.last_success is not None and now - self.last_success > max_age:
            self.fault = 'health_stale'

    @property
    def verified(self):
        return self.fault is None and self.last_success is not None


def probe_pinned_listener(proxy='http://127.0.0.1:17897',
                          url='https://www.gstatic.com/generate_204',
                          cancel_event=None, timeout=5.0) -> bool:
    """Do not call on main fast-poll thread. At most 5 s per request.

    No redirects; no curlrc; explicit proxy; empty no-proxy override; certificate
    verification remains enabled. Any HTTP status proves HTTPS transport worked.
    This cannot establish the residential identity without a separate IP check.
    """
    from network_config import proxy_endpoint
    proxy_endpoint(proxy)
    if url not in ('https://www.gstatic.com/generate_204',
                   'https://www.apple.com/library/test/success.html'):
        raise ValueError('unexpected probe endpoint')
    env = {k: v for k, v in os.environ.items() if k.lower() not in (
        'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
        'curl_ca_bundle', 'ssl_cert_file', 'ssl_cert_dir')}
    if cancel_event is not None and cancel_event.is_set():
        return False
    timeout = max(.01, min(5.0, float(timeout)))
    deadline = time.monotonic() + timeout
    proc = None
    try:
        proc = subprocess.Popen([
            '/usr/bin/curl', '--disable', '--silent', '--output', '/dev/null',
            '--write-out', '%{http_code}', '--head', '--proto', '=https',
            '--proxy', proxy, '--noproxy', '', '--connect-timeout', str(min(3.0, timeout)),
            '--max-time', str(timeout), '--url', url],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (cancel_event is not None and cancel_event.is_set()):
                return False
            try:
                output, _ = proc.communicate(timeout=min(.1, remaining))
                return (proc.returncode == 0 and
                        not (cancel_event is not None and cancel_event.is_set()) and
                        bool(re.fullmatch(r'[1-5]\d\d', output)))
            except subprocess.TimeoutExpired:
                continue
    except OSError:
        return False
    finally:
        if proc is not None and proc.poll() is None:
            # Only the curl process created above, never a user/app process.
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=.25)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=.25)


if __name__ == '__main__':
    # Read-only inventory. Reveals counts only, not tokens, addresses or argv.
    targets = Targets().update(snapshot())
    print({'desktop_official_process_count': len(targets), 'signals_sent': 0})

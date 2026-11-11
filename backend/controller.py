#!/usr/bin/env python3
"""Local menu controller. Status is read-only; no Claude termination is performed."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import uuid

from network_config import ROOT, MANIFEST, OWNER_SUFFIX, load as load_network

RUNTIME = ROOT / 'runtime'
STATE = RUNTIME / 'state'
LABEL = 'io.github.claude-desktop-companion.monitor'
# KeepAlive.SuccessfulExit implicitly starts a job on load, so the service file
# must stay outside LaunchAgents. Only the menu app bootstraps this job.
PLIST = RUNTIME / (LABEL + '.plist')
LEGACY_PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
PYTHON = sys.executable
RESOURCES = Path(__file__).resolve().parent
DOMAIN = f'gui/{os.getuid()}'
SERVICE = DOMAIN + '/' + LABEL

def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {} if default is None else default

def atomic_json(path, value):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.guard-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

def run(args, timeout=10):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

def service_pid():
    result = run(['/bin/launchctl', 'print', SERVICE], timeout=3)
    if result.returncode:
        return None
    match = re.search(r'^\s*pid = (\d+)\s*$', result.stdout, re.M)
    return int(match.group(1)) if match else None

def claude_running():
    from watchdog_core import snapshot, ROOT as CLAUDE
    return any(p.path == CLAUDE for p in snapshot())

def configuration_applied():
    try:
        load_network(MANIFEST)
        return True
    except (OSError, ValueError, TypeError):
        return False

def is_installed():
    return configuration_applied() and (RUNTIME / 'manifest.json').is_file()

def age_seconds(value, now=None):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return None
        age = (now or datetime.now(timezone.utc)).timestamp() - stamp.timestamp()
        return age if age >= 0 else None
    except (ValueError, TypeError, AttributeError):
        return None

DETAILS = {
    'not_configured': '网络监测尚未配置；备份与导入仍可使用。请按 README 设置现有代理。',
    'stopped': '后台监测未运行，固定代理设置可能仍然有效。',
    'stale': '没有收到新鲜且匹配当前进程的状态，无法确认监测正常。',
    'pausing': '正在暂停自动退出，等待后台监测确认。',
    'resuming': '正在恢复并重新验证连接。',
    'paused': '自动退出和外网探测已暂停；固定代理设置仍保留。',
    'idle': '监测已启动，Claude 未运行；正常待命不发外网探测。',
    'checking': '正在验证专用家宽代理连接。',
    'healthy': '专用代理探测成功，自动退出监测正在运行。',
    'probe_retry': '首次探测失败，正在复查。',
    'egress_unavailable': '家宽代理探测持续失败，监测程序将请求退出 Claude。',
    'local_proxy_unavailable': '本机代理不可用。',
    'controller_unavailable': 'Clash 控制接口不可用。',
    'system_proxy_changed': '系统代理已关闭或改变。',
    'configuration_changed': '代理或 Claude 配置已变化，需要重新检查；不会自动信任新配置。',
    'control_invalid': '控制文件无效，无法确认自动退出已启用。',
    'control_unavailable': '控制文件不可用，无法确认自动退出已启用。',
}

def compose_status(installed, pid, raw, control, now=None):
    desired = control.get('paused')
    expected_gen = control.get('generation')
    acknowledged = raw.get('control_generation')
    age = age_seconds(raw.get('at'), now)
    process_match = pid is not None and raw.get('controller_pid') == pid
    fresh = age is not None and age <= 12 and process_match
    mode = raw.get('monitor_mode', raw.get('mode', 'unknown'))
    ack = expected_gen is not None and acknowledged == expected_gen
    paused = raw.get('paused') is True
    if not installed:
        state = 'not_configured'
    elif pid is None:
        state = 'stopped'
    elif not fresh:
        state = 'stale'
    elif not ack or paused != desired:
        state = 'pausing' if desired is True else 'resuming'
    else:
        state = raw.get('state', 'checking')
    healthy = bool(installed and fresh and ack and mode == 'apply'
                   and desired is False and not paused and state == 'healthy'
                   and raw.get('healthy_verified') is True)
    if state == 'healthy' and not healthy:
        state = 'checking'
    return {'ok': True, 'installed': installed, 'running': pid is not None,
            'paused': paused, 'desired_paused': desired, 'state': state,
            'status_age_seconds': age, 'monitor_mode': mode,
            'healthy_verified': healthy, 'last_probe_at': raw.get('last_probe_at'),
            'control_generation': expected_gen, 'ack_generation': acknowledged,
            'owner_pid': raw.get('owner_pid'),
            'detail': DETAILS.get(state, '状态需要检查。'),
            'can_start': installed and pid is None,
            'official_process_count': raw.get('official_process_count', 0)}

def status():
    result = compose_status(is_installed(), service_pid(), read_json(STATE / 'status.json'),
                            read_json(STATE / 'control.json'))
    result['can_start'] = configuration_applied() and not result['running']
    audit = read_json(ROOT / 'audit/connections.json')
    audit_age = age_seconds(audit.get('sampled_at'))
    try:
        config_fingerprint = hashlib.sha256((RUNTIME/'manifest.json').read_bytes()).hexdigest()
    except OSError:
        config_fingerprint = None
    if result['healthy_verified']:
        counts = audit.get('counts', {})
        if (not config_fingerprint or audit.get('config_fingerprint') != config_fingerprint
                or audit_age is None or audit_age > 12 or audit.get('partial') or audit.get('truncated')
                or not audit.get('socket_inspection_available') or not audit.get('controller_available')):
            result.update(state='routing_unverified', healthy_verified=False,
                          detail='专用代理探测通过，但实时连接检查尚未完成或已过期。')
        elif counts.get('direct', 0) or counts.get('other_proxy', 0):
            result.update(state='routing_warning', healthy_verified=False,
                          detail='检测到直连或其它代理路径，请查看实时连接；绿色不代表这些连接安全。')
        elif counts.get('unverified', 0):
            result.update(state='routing_unverified', healthy_verified=False,
                          detail='有连接的出口尚未核实，请查看实时连接。')
        elif audit.get('egress', {}).get('status') == 'mismatch':
            result.update(state='routing_warning', healthy_verified=False,
                          detail='专用入口抽测出口与预期家宽不一致，请查看实时连接。')
        elif audit.get('egress', {}).get('status') != 'verified':
            result.update(state='routing_unverified', healthy_verified=False,
                          detail='连接匹配到家宽代理链，但公网出口抽测尚未确认。')
        elif not counts.get('residential', 0):
            result.update(state='no_active_flows', healthy_verified=False,
                          detail='专用代理探测通过，当前没有可核实的 Claude 外网连接。')
    return result

def set_paused(paused):
    atomic_json(STATE / 'control.json', {'schema': 1, 'paused': paused,
                                       'generation': str(uuid.uuid4())})

def copy_runtime():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    RUNTIME.chmod(0o700)
    STATE.mkdir(mode=0o700, exist_ok=True)
    STATE.chmod(0o700)
    for name in ('daemon.py', 'watchdog_core.py', 'network_config.py'):
        source = RESOURCES / name
        if not source.is_file():
            raise RuntimeError('missing_backend')
        target = RUNTIME / name
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    target = RUNTIME / 'manifest.json'
    target.write_bytes(MANIFEST.read_bytes())
    target.chmod(0o600)
    for name in ('guard.log', 'guard-error.log'):
        path = RUNTIME / name
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)

def gui_owner():
    from watchdog_core import get_process
    process = get_process(os.getppid())
    if (process is None or process.uid != os.getuid() or not process.path.endswith(
            OWNER_SUFFIX)):
        raise RuntimeError('start_requires_menu_app')
    return process.pid

def write_launchagent(owner_pid):
    config = {'Label': LABEL,
              'ProgramArguments': [PYTHON, '-B', str(RUNTIME/'daemon.py'), '--config',
                                   str(RUNTIME/'manifest.json'), '--apply',
                                   '--state-dir', str(STATE), '--owner-pid', str(owner_pid)],
              'RunAtLoad': False, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 10,
              'ProcessType': 'Background', 'Umask': 0o077,
              'StandardOutPath': str(RUNTIME/'guard.log'),
              'StandardErrorPath': str(RUNTIME/'guard-error.log')}
    PLIST.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(config))
    PLIST.chmod(0o600)

def remove_legacy_login_item():
    if not LEGACY_PLIST.exists():
        return
    config = plistlib.loads(LEGACY_PLIST.read_bytes())
    arguments = config.get('ProgramArguments', [])
    if config.get('Label') != LABEL or str(RUNTIME/'daemon.py') not in arguments:
        raise RuntimeError('unexpected_legacy_login_item')
    LEGACY_PLIST.unlink()

def start():
    if not configuration_applied():
        return {'ok': True, 'needs_configuration': True, 'state': 'not_configured',
                'detail': DETAILS['not_configured']}
    owner = gui_owner()
    if service_pid() is not None:
        if read_json(STATE/'status.json').get('owner_pid') == owner:
            set_paused(False)
            return {'ok': True, 'detail': '已请求恢复，等待重新验证。'}
        run(['/bin/launchctl', 'bootout', SERVICE], timeout=5)
        if service_pid() is not None:
            return {'ok': False, 'detail': '旧应用的监测尚未停止，请稍后重试。'}
    # A launchd job without a current PID may still be waiting to restart.
    # Unregister it before replacing its executable files.
    run(['/bin/launchctl', 'bootout', SERVICE], timeout=5)
    if service_pid() is not None:
        return {'ok': False, 'detail': '旧监测进程尚未停止，未替换程序。'}
    copy_runtime()
    remove_legacy_login_item()
    set_paused(False)
    preflight = run([PYTHON, '-B', str(RUNTIME/'daemon.py'), '--config',
                     str(RUNTIME/'manifest.json'), '--check'], timeout=20)
    if preflight.returncode:
        return {'ok': False, 'detail': '代理配置或连接验证未通过，后台监测尚未启动。'}
    write_launchagent(owner)
    result = run(['/bin/launchctl', 'bootstrap', DOMAIN, str(PLIST)], timeout=8)
    if result.returncode:
        return {'ok': False, 'detail': '后台监测启动失败，不能视为保护已启用。'}
    kicked = run(['/bin/launchctl', 'kickstart', SERVICE], timeout=5)
    if kicked.returncode and service_pid() is None:
        return {'ok': False, 'detail': '监测进程尚未启动，不能视为保护已启用。'}
    return {'ok': True, 'detail': '后台监测已启动，等待状态确认。'}

def inspect_egress(active=True):
    if not is_installed():
        return {'status': 'unverified', 'detail': '专用入口尚未配置，当前只核对连接对应的代理链。'}
    config = load_network(RUNTIME/'manifest.json')
    config_fingerprint = hashlib.sha256((RUNTIME/'manifest.json').read_bytes()).hexdigest()
    cache = ROOT/'audit/egress.json'
    cached = read_json(cache)
    age = age_seconds(cached.get('checked_at'))
    if not active:
        return {**cached, 'status': 'idle', 'detail': 'Claude 未运行，未发送新的出口抽测。'}
    if read_json(STATE/'control.json').get('paused') is True:
        return {**cached, 'status': 'paused',
                'detail': '自动退出已暂停，未发送新的出口抽测；已有地址只表示此前结果。'}
    if age is not None and age < 60 and cached.get('config_fingerprint') == config_fingerprint:
        return cached
    result = {'status': 'unverified', 'checked_at': datetime.now(timezone.utc).isoformat(),
              'config_fingerprint': config_fingerprint,
              'detail': '代理出口抽测尚未通过；不能代表所有连接。'}
    command = ['/usr/bin/curl', '-q', '-sS', '--proxy', config['proxy_url'],
               '--noproxy', '', '--connect-timeout', '3', '--max-time', '5',
               '--proto', '=https', 'https://www.cloudflare.com/cdn-cgi/trace']
    try:
        response = run(command, timeout=6)
        values = dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)
        address = str(ipaddress.ip_address(values.get('ip', '')))
        if response.returncode == 0:
            expected = config['expected_ip']
            match = bool(expected and address == expected)
            result.update(status='verified' if match else 'mismatch', ip=address,
                          country=values.get('loc', '未知'),
                          detail='专用代理出口抽测与预期家宽一致；不能代表所有连接。' if match
                          else '专用入口观测出口与原家宽地址不一致，需要检查。')
    except (ValueError, OSError, subprocess.SubprocessError):
        pass
    atomic_json(cache, result)
    return result

def inspect_flows():
    import flows
    if not is_installed():
        return {'ok': True, 'flows': [], 'counts': {}, 'scope': {}, 'partial': True,
                'controller_available': False, 'socket_inspection_available': False,
                'coverage_note': DETAILS['not_configured']}
    config = load_network(RUNTIME/'manifest.json')
    result = flows.inspect_flows(config)
    result['config_fingerprint'] = hashlib.sha256((RUNTIME/'manifest.json').read_bytes()).hexdigest()
    result['egress'] = inspect_egress(active=result.get('scope', {}).get('process_count', 0) > 0)
    result['ok'] = True
    atomic_json(ROOT/'audit/connections.json', result)
    return result

def stop_monitor():
    """Confirm the old process is gone, including an already-unregistered job."""
    from watchdog_core import get_process
    raw_pid = read_json(STATE/'status.json').get('controller_pid')
    registered_pid = service_pid()
    old = get_process(registered_pid or raw_pid) if (registered_pid or isinstance(raw_pid, int)) else None
    if STATE.is_dir():
        set_paused(True)
    try:
        run(['/bin/launchctl', 'bootout', SERVICE], timeout=8)
    except subprocess.TimeoutExpired:
        # bootout may finish while its client times out; verify rather than guess.
        pass
    deadline = time.monotonic() + 2
    while True:
        pid = service_pid()
        current = get_process(old.pid) if old is not None else None
        old_alive = current is not None and current.identity == old.identity
        if pid is None and not old_alive:
            PLIST.unlink(missing_ok=True)
            remove_legacy_login_item()
            return {'ok': True, 'detail': '自动退出监测已停止；固定代理配置仍然保留。'}
        if time.monotonic() >= deadline:
            return {'ok': False, 'detail': '后台监测尚未确认完全停止，请稍后重试退出。'}
        time.sleep(.1)

def execute(command):
    if command == 'status':
        return status()
    if command == 'start':
        return start()
    if command == 'flows':
        return inspect_flows()
    if command in ('pause', 'resume'):
        if not is_installed():
            return {'ok': False, 'detail': '请先启用保护。'}
        if command == 'resume' and service_pid() is None:
            return start()
        set_paused(command == 'pause')
        return {'ok': True, 'detail': '请求已发出，等待菜单栏状态确认。'}
    if command == 'check':
        if not is_installed():
            return {'ok': False, 'detail': '尚未配置，请先启用保护。'}
        result = run([PYTHON, '-B', str(RUNTIME/'daemon.py'), '--config',
                      str(RUNTIME/'manifest.json'), '--check'], timeout=20)
        return {'ok': result.returncode == 0,
                'detail': '专用代理检查通过。' if result.returncode == 0 else '检查未通过；请查看状态和日志。'}
    if command == 'stop':
        return stop_monitor()
    if command == 'open_logs':
        folder = RUNTIME if RUNTIME.is_dir() else ROOT
        result = run(['/usr/bin/open', str(folder)])
        return {'ok': result.returncode == 0, 'detail': '已打开本地状态与日志目录。'}
    raise ValueError('unsupported_command')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'start', 'pause', 'resume', 'check', 'stop', 'open_logs', 'flows'])
    args = parser.parse_args()
    try:
        if args.command in ('status', 'check', 'open_logs', 'flows'):
            result = execute(args.command)
        else:
            ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(ROOT/'controller.lock', 'a') as lock:
                os.chmod(lock.name, 0o600)
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = execute(args.command)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('ok') else 1
    except BlockingIOError:
        print(json.dumps({'ok': False, 'detail': '另一项控制操作正在进行，请稍后重试。'}, ensure_ascii=False))
    except Exception as error:
        try:
            atomic_json(ROOT/'last-controller-error.json', {
                'at': datetime.now(timezone.utc).isoformat(),
                'command': args.command, 'error_type': type(error).__name__})
        except OSError:
            pass
        print(json.dumps({'ok': False, 'detail': '操作未完成；没有确认到保护正常，请查看本地日志或重新检查。'}, ensure_ascii=False))
    return 1

if __name__ == '__main__':
    sys.exit(main())

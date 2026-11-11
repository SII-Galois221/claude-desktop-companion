#!/usr/bin/env python3
"""Read-only, momentary socket inspection for Claude Desktop.

No packet contents, credentials, argv, environment or unrelated process sockets
are collected. Observed routing is not an exhaustive egress/IP guarantee.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import http.client
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from watchdog_core import BUNDLE, CODE_PATTERN, ROOT, Targets, snapshot

# Defaults below are synthetic classification fixtures, not an installed proxy.
RESIDENTIAL = 'Example residential node'
PROXY_PORTS = {7897, 17897}
VERDICTS = ('residential', 'other_proxy', 'direct', 'local', 'unverified')


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def clean_text(value, limit=255):
    if not isinstance(value, (str, int)):
        return ''
    return ''.join(c for c in str(value) if c.isprintable())[:limit]


def address(value):
    try:
        parsed = ipaddress.ip_address(value.split('%', 1)[0])
        return parsed.ipv4_mapped if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped else parsed
    except (ValueError, AttributeError):
        return None


def endpoint(value):
    """lsof -nP's IPv4/IPv6 endpoint, without doing DNS lookups."""
    try:
        host, port = value.rsplit(':', 1)
        host = host.removeprefix('[').removesuffix(']')
        port = int(port)
        if not 0 <= port <= 65535:
            return None
        return {'host': host, 'port': port}
    except (AttributeError, ValueError):
        return None


def format_endpoint(host, port):
    host = clean_text(host)
    if not host:
        return '未知目标'
    if ':' in host and not host.startswith('['):
        host = '[' + host + ']'
    return host + (':' + str(port) if port is not None else '')


def parse_lsof(data):
    """Parse lsof -F0pcfPnT, whose process/file records end in NUL + LF."""
    records = []
    pid = None
    current = None

    def finish():
        nonlocal current
        if current is not None and current.get('transport') in ('TCP', 'UDP'):
            records.append(current)
        current = None

    for field in data.split(b'\0'):
        field = field.lstrip(b'\n')
        if not field:
            continue
        key, value = chr(field[0]), field[1:].decode('utf-8', 'replace')
        if key == 'p':
            finish()
            try:
                pid = int(value)
            except ValueError:
                pid = None
        elif key == 'f':
            finish()
            if pid is not None:
                current = {'pid': pid, 'fd': value, 'transport': '', 'name': '', 'state': ''}
        elif current is not None:
            if key == 'P':
                current['transport'] = value.upper()
            elif key == 'n':
                current['name'] = value
            elif key == 'T' and value.startswith('ST='):
                current['state'] = value[3:]
    finish()
    return records


def read_sockets(pids):
    if not pids:
        return [], True, {'partial': False, 'truncated': False}
    try:
        result = subprocess.run([
            '/usr/sbin/lsof', '-nP', '-a', '-p', ','.join(map(str, sorted(set(pids)))),
            '-i', '-F0pcfPnT'], capture_output=True, timeout=5)
        # lsof returns 1 when this selected set currently has no Internet sockets.
        ok = result.returncode in (0, 1) and not result.stderr.strip()
        records = parse_lsof(result.stdout)
        wanted = set(pids)
        return [r for r in records if r['pid'] in wanted], ok, {
            'partial': not ok, 'truncated': False,
            'reason': None if ok else 'lsof_returned_error_or_warning'}
    except (OSError, subprocess.SubprocessError):
        return [], False, {'partial': True, 'truncated': False, 'reason': 'lsof_unavailable_or_timeout'}


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=1.0)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def read_connections(controller_socket):
    conn = UnixConnection(controller_socket)
    try:
        conn.request('GET', '/connections', headers={'Connection': 'close'})
        response = conn.getresponse()
        limit = 4 * 1024 * 1024
        body = bytearray()
        deadline = time.monotonic() + 2
        while len(body) <= limit:
            if time.monotonic() >= deadline:
                return [], False, {'partial': True, 'truncated': False, 'reason': 'controller_read_deadline'}
            part = response.read1(min(65536, limit + 1 - len(body)))
            if not part:
                break
            body.extend(part)
        if len(body) > limit:
            return [], False, {'partial': True, 'truncated': True, 'reason': 'controller_response_too_large'}
        if response.status != 200:
            return [], False, {'partial': True, 'truncated': False, 'reason': 'controller_http_error'}
        data = json.loads(body)
        items = data.get('connections')
        if not isinstance(items, list):
            return [], False, {'partial': True, 'truncated': False, 'reason': 'invalid_controller_response'}
        return [x for x in items if isinstance(x, dict)], True, {'partial': False, 'truncated': False}
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return [], False, {'partial': True, 'truncated': False, 'reason': 'controller_unavailable_or_timeout'}
    finally:
        conn.close()


def integer(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def reader_result(value):
    # Accept the compact two-field form used by dependency-injected tests/hosts.
    items, ok = value[:2]
    diagnostic = value[2] if len(value) > 2 else {'partial': not ok, 'truncated': False}
    return items, ok, diagnostic


def match_connection(record, source, peer, connections):
    """No destination guessing: source IP/port + transport must match exactly."""
    matches = []
    source_ip = address(source['host'])
    if source_ip is None:
        return None, False
    for connection in connections:
        meta = connection.get('metadata')
        if not isinstance(meta, dict):
            continue
        if (integer(meta.get('sourcePort')) != source['port'] or
            str(meta.get('network', '')).upper() != record['transport'] or
            address(meta.get('sourceIP')) != source_ip):
            continue
        # Some Mihomo versions expose this additional disambiguating field.
        if meta.get('inboundPort') is not None and integer(meta['inboundPort']) != peer['port']:
            continue
        matches.append(connection)
    return (matches[0], False) if len(matches) == 1 else (None, len(matches) > 1)


def classify(record, process, kind, connections, controller_available,
             expected_node=RESIDENTIAL, proxy_ports=PROXY_PORTS):
    result = {'pid': process.pid, 'process': Path(process.path).name,
              'process_path': process.path, 'classification': kind,
              'target': '未知目标', 'transport': record['transport'],
              'via': '未核实', 'chain': [], 'verdict': 'unverified',
              'detail': '', 'socket_state': clean_text(record.get('state', ''))}
    name = record.get('name', '')
    if record.get('state') == 'LISTEN':
        bound = endpoint(name)
        result.update(target=format_endpoint(bound['host'], bound['port']) if bound else '监听端口',
                      via='本机监听', verdict='local', detail='这是接收连接的监听套接字，不是已验证的出站连接。')
        return result
    if '->' not in name:
        bound = endpoint(name)
        result.update(target=format_endpoint(bound['host'], bound['port']) if bound else '未知对端',
                      detail='未显示远端地址，无法从此套接字判定 UDP 或短连接的出口。')
        return result
    left, right = name.split('->', 1)
    source, peer = endpoint(left), endpoint(right)
    if source is None or peer is None:
        result['detail'] = '无法可靠解析套接字端点。'
        return result
    result['target'] = format_endpoint(peer['host'], peer['port'])
    remote = address(peer['host'])
    if remote is None:
        result['detail'] = '远端不是可验证的数值 IP，未进行 DNS 猜测。'
        return result
    if not remote.is_loopback:
        result.update(verdict='direct', via='直接连接非回环地址',
                      detail='未连接已知本机代理入口；显示的是连接目标 IP，不是本机公网出口 IP。此为套接字路径观察，无法排除系统级隧道。')
        return result
    if peer['port'] not in proxy_ports:
        result.update(verdict='local', via='本机内部连接',
                      detail='连接本机回环端口；它可能是内部服务或其它本机代理，不能据此推断后续公网出口。')
        return result
    result['via'] = '本机代理 :' + str(peer['port'])
    if not controller_available:
        result['detail'] = 'Clash 连接表不可用，不能核实此代理连接的出站链路。'
        return result
    connection, ambiguous = match_connection(record, source, peer, connections)
    if connection is None:
        result['detail'] = ('源端口匹配到多个连接，无法唯一确认出口。' if ambiguous else
                            '没有匹配到同源 IP、端口和协议的 Clash 连接，可能在采样间隙关闭。')
        return result
    meta = connection['metadata']
    host = meta.get('host') or meta.get('destinationIP')
    result['target'] = format_endpoint(host, integer(meta.get('destinationPort')))
    chain = connection.get('chains', [])
    result['chain'] = [clean_text(x, 128) for x in chain if isinstance(x, str)] if isinstance(chain, list) else []
    if not result['chain']:
        result['detail'] = '已匹配到代理连接，但没有可验证的出口链信息。'
    elif expected_node and result['chain'][0] == expected_node:
        result.update(verdict='residential', detail='Clash 当前连接表的最终出站节点为家宽；未单独测量该请求的公网出口 IP。')
    elif result['chain'][0] == 'DIRECT':
        result.update(verdict='direct', detail='Clash 当前连接表显示 DIRECT 出站。')
    elif result['chain'][0] in ('REJECT', 'REJECT-DROP', 'PASS'):
        result['detail'] = '连接表未提供可确认的实际外部出口。'
    else:
        result.update(verdict='other_proxy', detail='Clash 当前连接表显示使用其它出站节点。')
    return result


class FlowInspector:
    """Reuse this object to retain proven orphan identity in a long-lived host.

    A fresh one-shot inspector never guesses ownership of already orphaned Code.
    It intentionally does not persist process history or inspect unrelated sockets.
    """
    def __init__(self, uid=None, config=None):
        self.config = config
        self.uid = os.getuid() if uid is None else uid
        self.official = Targets(uid=self.uid)

    def select(self, processes):
        user = [p for p in processes if p.uid == self.uid]
        all_by_pid = {p.pid: p for p in user}
        descendant_pids = {p.pid for p in user if p.path == ROOT}
        while True:
            more = {p.pid for p in user if p.ppid in descendant_pids}
            if more <= descendant_pids:
                break
            descendant_pids |= more
        proven_official = self.official.update(user)
        selected = {pid: all_by_pid[pid] for pid in descendant_pids}
        selected.update((p.pid, p) for p in proven_official)
        kinds = {}
        for pid, p in selected.items():
            kinds[pid] = ('desktop' if p.path == ROOT else
                          'desktop_code' if CODE_PATTERN.fullmatch(p.path) else
                          'helper' if p.path.startswith(BUNDLE) else 'tool_descendant')
        excluded = []
        for p in user:
            if p.pid in selected:
                continue
            if CODE_PATTERN.fullmatch(p.path):
                excluded.append({'pid': p.pid, 'process': Path(p.path).name,
                                 'reason': 'official_code_ancestry_unconfirmed'})
            elif Path(p.path).name == 'claude':
                excluded.append({'pid': p.pid, 'process': Path(p.path).name,
                                 'reason': 'separate_terminal_or_unattributed_claude'})
        return selected, kinds, excluded

    def inspect(self, *, processes=None, socket_reader=read_sockets,
                connection_reader=None, process_reader=snapshot):
        if connection_reader is None:
            if self.config is None:
                connection_reader = lambda: ([], False)
            else:
                connection_reader = lambda: read_connections(self.config['controller_socket'])
        try:
            processes = process_reader() if processes is None else processes
        except (OSError, ValueError, subprocess.SubprocessError):
            return {'sampled_at': utc_now(), 'scope': {}, 'flows': [], 'counts': {},
                    'controller_available': False, 'socket_inspection_available': False,
                    'partial': True, 'truncated': False,
                    'diagnostics': {'processes': 'process_inventory_unavailable'},
                    'coverage_note': '进程清单读取失败，不能据此认为没有外部连接。'}
        selected, kinds, excluded = self.select(processes)
        with ThreadPoolExecutor(max_workers=2) as pool:
            sockets_job = pool.submit(socket_reader, list(selected))
            controller_job = pool.submit(connection_reader)
            records, sockets_ok, socket_diagnostic = reader_result(sockets_job.result())
            connections, controller_ok, controller_diagnostic = reader_result(controller_job.result())
        from network_config import proxy_endpoint
        ports = ({proxy_endpoint(self.config['proxy_url'])[1]} if self.config else PROXY_PORTS)
        if self.config and self.config.get('system_proxy_url'):
            ports.add(proxy_endpoint(self.config['system_proxy_url'])[1])
        expected = self.config['expected_node'] if self.config else None
        flows = [classify(r, selected[r['pid']], kinds[r['pid']], connections, controller_ok, expected, ports)
                 for r in records if r['pid'] in selected]
        counts = dict.fromkeys(VERDICTS, 0)
        counts.update(Counter(f['verdict'] for f in flows))
        counts['total'] = len(flows)
        return {'sampled_at': utc_now(), 'scope': {
                    'name': 'Claude Desktop、可追溯工具子进程和已确认归属的官方 Code 进程',
                    'process_count': len(selected), 'class_counts': dict(Counter(kinds.values())),
                    'excluded_processes': excluded, 'excluded_process_count': len(excluded)},
                'flows': flows, 'counts': counts, 'controller_available': controller_ok,
                'socket_inspection_available': sockets_ok,
                'partial': bool(not sockets_ok or not controller_ok or
                                socket_diagnostic.get('partial') or controller_diagnostic.get('partial')),
                'truncated': bool(socket_diagnostic.get('truncated') or controller_diagnostic.get('truncated')),
                'diagnostics': {'sockets': socket_diagnostic, 'controller': controller_diagnostic},
                'coverage_note': '这是瞬时套接字采样，不读取流量内容。已关闭的短连接可能漏过；'
                    '未连接的 UDP、无法追溯的孤儿进程、其它本机转发器和系统级隧道不能完整确认。'
                    '没有捕获到连接不代表全部流量安全。家宽判定来自 Clash 活动连接表，'
                    '不等于对每个请求独立验证公网出口 IP。'}


def inspect_flows(config=None):
    return FlowInspector(config=config).inspect()


def collect():
    """Menu-controller entry point; does no external network probing."""
    return inspect_flows()


if __name__ == '__main__':
    print(json.dumps(inspect_flows(), ensure_ascii=False))

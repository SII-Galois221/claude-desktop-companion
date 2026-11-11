#!/usr/bin/env python3
"""Opt-in local monitor configuration. Never edits proxy or Claude settings."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path.home() / 'Library/Application Support/Claude-Desktop-Companion'
MANIFEST = ROOT / 'network.json'
OWNER_SUFFIX = '/Claude Desktop Companion.app/Contents/MacOS/ClaudeDesktopCompanion'


def proxy_endpoint(value):
    """Only an explicit credential-free loopback HTTP listener is supported."""
    try:
        url = urlsplit(value)
        address = ipaddress.ip_address(url.hostname or '')
        if (url.scheme != 'http' or not address.is_loopback or url.username is not None
                or url.password is not None or url.path or url.query or url.fragment
                or type(url.port) is not int or not 1 <= url.port <= 65535):
            raise ValueError('invalid proxy')
        return str(address), url.port
    except (ValueError, TypeError, AttributeError):
        raise ValueError('proxy must be http://numeric-loopback-address:port') from None


def validate(data):
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise ValueError('unsupported network configuration')
    proxy_endpoint(data.get('proxy_url'))
    if data.get('system_proxy_url') is not None:
        proxy_endpoint(data['system_proxy_url'])
    if not isinstance(data.get('controller_socket'), str) or not Path(data['controller_socket']).is_absolute():
        raise ValueError('controller_socket must be an absolute Unix socket path')
    node = data.get('expected_node')
    if not isinstance(node, str) or not node.strip() or len(node) > 128 or not node.isprintable() or node in ('DIRECT', 'REJECT', 'REJECT-DROP', 'PASS'):
        raise ValueError('expected_node must name the actual final proxy node')
    ipaddress.ip_address(data.get('expected_ip', ''))
    files = data.get('expected_files')
    if not isinstance(files, list) or not files:
        raise ValueError('at least one trusted configuration file is required')
    seen = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError('invalid trusted file')
        path, digest = entry.get('path'), entry.get('sha256')
        if (not isinstance(path, str) or not Path(path).is_absolute() or path in seen
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('invalid trusted file')
        seen.add(path)
    return data


def load(path=MANIFEST):
    return validate(json.loads(Path(path).read_text()))


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(128 * 1024):
            h.update(block)
    return h.hexdigest()


def initialize(args, destination=MANIFEST):
    files = []
    for name in args.watch_file:
        # Preserve the path the application actually reads. Resolving a symlink
        # here would pin its old target and miss a later repoint of that link.
        path = Path(name).expanduser().absolute()
        if not path.is_file():
            raise ValueError('watched paths must be regular files')
        files.append({'path': str(path), 'sha256': file_digest(path)})
    data = validate({'schema': 1, 'proxy_url': args.proxy_url,
                     'controller_socket': str(Path(args.controller_socket).expanduser()),
                     'expected_node': args.expected_node, 'expected_ip': args.expected_ip,
                     'system_proxy_url': args.system_proxy_url, 'expected_files': files})
    destination = Path(destination)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Exclusive create: changing the trust baseline requires an explicit review
    # and removal of the old configuration while the app is stopped.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return {'ok': True, 'detail': 'Local monitor configuration created. Restart the companion app to enable monitoring.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', help='Trust existing configuration files; no proxy or Claude settings are changed')
    init.add_argument('--proxy-url', required=True)
    init.add_argument('--controller-socket', required=True)
    init.add_argument('--expected-node', required=True)
    init.add_argument('--expected-ip', required=True)
    init.add_argument('--watch-file', action='append', required=True,
                      help='Existing trusted config (repeat for Claude and Mihomo files)')
    init.add_argument('--system-proxy-url', help='Also require macOS HTTP and HTTPS proxies to match this listener')
    args = parser.parse_args()
    try:
        print(json.dumps(initialize(args)))
        return 0
    except (OSError, ValueError, TypeError):
        print(json.dumps({'ok': False, 'detail': 'Configuration not created. Check inputs; an existing network.json is never overwritten.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

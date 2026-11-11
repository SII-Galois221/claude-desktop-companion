#!/usr/bin/env python3
"""Catch common accidental runtime-data leaks; not a complete secret scanner."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_NAMES = {
    'accounts.json', 'history_accounts.json', 'network.json', 'checkpoint.json',
    'Cookies', '.env', 'verification.json', 'traffic-measurement.json',
}
FORBIDDEN_SUFFIXES = {'.jsonl', '.log', '.pem', '.key', '.p12', '.sqlite', '.db', '.zip'}
SKIP_DIRS = {'.git', 'build', 'dist', '__pycache__', '.venv'}
PATTERNS = {
    'absolute personal home path': re.compile(r'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
    'private key': re.compile(r'-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----'),
    'GitHub token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'Anthropic API key': re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}\b'),
}


def files():
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True)
    if result.returncode == 0 and result.stdout:
        return [ROOT / p.decode() for p in result.stdout.split(b'\0') if p]
    return [p for p in ROOT.rglob('*') if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(ROOT).parts)]


def main():
    errors = []
    paths = files()
    for path in paths:
        relative = path.relative_to(ROOT)
        if (path.is_symlink() or path.name in FORBIDDEN_NAMES or path.suffix in FORBIDDEN_SUFFIXES
                or SKIP_DIRS.intersection(relative.parts)
                or {'backups', 'runtime', 'transactions'}.intersection(relative.parts)):
            errors.append(f'{relative}: unexpected private/runtime artifact')
            continue
        if path.stat().st_size > 2_000_000:
            errors.append(f'{relative}: unexpectedly large source artifact')
            continue
        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            errors.append(f'{relative}: unexpected binary artifact')
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                errors.append(f'{relative}: {label}')
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print(f'Public-tree checks passed for {len(paths)} files (manual privacy review still required).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Offline, append-only local Code history for the menu app.

Only config.lastKnownAccountUuid is used for account identity. Authentication
files, project files, proxy settings and transcript contents are never mutated.
Preview candidates and checkpoints live outside the application and repository.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import uuid

import history_sync_engine as engine

ROOT = engine.HISTORY_ROOT
HISTORY_KEYS = (
    'cliSessionId', 'priorCliSessionIds', 'preClearCliSessionId',
    'unarchivedCliSessionId', 'lastAssistantCliSessionId', 'cwd', 'originCwd',
    'rewindEdges', 'transcriptCuts', 'transcriptModelStates',
    'forkedFromSessionId', 'forkedAtMessageUuid', 'priorCliSessionLeftAt',
)
READ_ERRORS = (RuntimeError, OSError, ValueError, KeyError, TypeError)
PREVIEW_LIFETIME = 30 * 60
BACKUP_NAME = re.compile(r'\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{8}\Z')


def json_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def history_signature(record):
    # Sanitization preserves the verified legacy SSH owner fallback, then drops
    # SSH metadata. Titles, activity statistics and permissions do not select
    # which history Desktop reads. All branch/cut/project metadata does.
    clean = engine.sanitized(record, 0)
    # Desktop 5.1231.2 go() hydrates an omitted/null reply owner to the current
    # transcript when lastAssistantUuid is a string (even an empty string).
    # sanitized() has already preserved the legacy SSH unknown-owner fallback
    # as explicit ""; that value and other explicit owners must remain distinct.
    # Normalize comparison only, so viewing an imported session cannot itself
    # make the same history appear to be a new continuation.
    if clean.get('lastAssistantCliSessionId') is None and isinstance(clean.get('lastAssistantUuid'), str):
        clean['lastAssistantCliSessionId'] = clean['cliSessionId']
    return json_hash({key: clean.get(key) for key in HISTORY_KEYS})


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')


def private_dir(path):
    engine.private_dirs(path)
    engine.require(path.stat().st_uid == os.getuid(), '历史目录不属于当前用户。')
    path.chmod(0o700)


def private_file(path):
    engine.safe(path, 'file')
    meta = path.stat()
    engine.require(meta.st_uid == os.getuid() and meta.st_nlink == 1,
                   '历史元数据必须是当前用户拥有的独立普通文件。')
    engine.require(stat.S_IMODE(meta.st_mode) == 0o600, '历史元数据权限必须为 600。')
    return path


def available_files(folder, projects, records):
    """A faithful target checkpoint may explicitly record missing bodies."""
    files = {Path('source-index') / name: folder / name for name in records}
    directories, found = set(), set()
    ids = set().union(*(engine.transcript_ids(r) for r in records.values())) if records else set()
    if projects.exists():
        for project in sorted(engine.safe(projects, 'dir').iterdir()):
            engine.safe(project)
            if not project.is_dir():
                continue
            for sid in ids:
                transcript = project / (sid + '.jsonl')
                if transcript.exists() or transcript.is_symlink():
                    files[Path('projects') / transcript.relative_to(projects)] = engine.safe(transcript, 'file')
                    found.add(sid)
                companion = project / sid
                if companion.exists() or companion.is_symlink():
                    engine.safe(companion, 'dir')
                    directories.add(Path('projects') / companion.relative_to(projects))
                    for base, subdirs, names in os.walk(companion, followlinks=False):
                        for name in subdirs:
                            path = engine.safe(Path(base) / name, 'dir')
                            directories.add(Path('projects') / path.relative_to(projects))
                        for name in names:
                            path = engine.safe(Path(base) / name, 'file')
                            files[Path('projects') / path.relative_to(projects)] = path
    return files, directories, found


class Environment(engine.Environment):
    def running(self):
        # Exact executable paths, including orphan Desktop Code processes;
        # never read argv/environment and never signal or start anything.
        # A second official installation can use the same account data. The
        # broad name check is refusal-only; it never targets a process to quit.
        if super().running():
            return True
        from watchdog_core import snapshot
        bundle = str(self.app / 'Contents') + '/'
        code = str(self.desktop / 'claude-code') + '/'
        return any(p.path.startswith(bundle) or
                   (p.path.startswith(code) and p.path.endswith('/claude.app/Contents/MacOS/claude'))
                   for p in snapshot())


class HistoryService:
    def __init__(self, env=None, root=None):
        self.env = env or Environment()
        self.root = Path(root) if root is not None else ROOT

    @contextmanager
    def operation(self):
        private_dir(self.root)
        path = engine.safe(self.root / 'operation.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            engine.require(os.fstat(fd).st_nlink == 1, '历史操作锁不能是共享链接。')
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('另一项历史操作正在进行，请等待完成。') from exc
            yield
        finally:
            os.close(fd)

    def identity(self, stopped=False):
        state = self.env.state(stopped=stopped)
        return {key: state[key] for key in ('active_alias', 'version', 'accounts_sha256')}

    def register(self, request):
        """Enroll the current official account from identifiers, never credentials.

        Reads lastKnownAccountUuid and existing UUID directory names only. This
        command cannot switch logins, create Claude account folders, or mark an
        unsupported Desktop version as reviewed.
        """
        engine.require(set(request) <= {'alias', 'org'}, '登记只接受 alias 和可选 org。')
        alias = request.get('alias')
        engine.require(isinstance(alias, str) and engine.ALIAS.fullmatch(alias),
                       '账号别名必须为 1–32 位小写字母、数字、下划线或短横线，并以字母开头。')
        registry = engine.safe(self.env.accounts_path)
        before = engine.digest(registry) if registry.exists() else None
        config = self.env.settings()
        version = self.env.version()
        engine.require(version in config['validated_desktop_versions'],
                       '当前 Claude 版本尚未验证，请先完成版本适配，不能通过登记绕过检查。')
        active = engine.read_json(self.env.desktop / 'config.json').get('lastKnownAccountUuid')
        engine.require(isinstance(active, str) and engine.UUID.fullmatch(active),
                       '无法识别当前账号；请先通过官方 Claude 客户端登录。')
        folder = engine.safe(self.env.desktop / 'claude-code-sessions' / active)
        engine.require(folder.is_dir(), '当前账号没有本地 Code 目录；请先在 Claude Code 页面打开一个本地会话。')
        organizations = []
        for child in folder.iterdir():
            if engine.UUID.fullmatch(child.name):
                engine.safe(child, 'dir')
                organizations.append(child.name)
        requested_org = request.get('org')
        if requested_org is not None:
            engine.require(isinstance(requested_org, str) and engine.UUID.fullmatch(requested_org)
                           and requested_org in organizations, 'org 必须是当前账号已有的 Code 组织目录名。')
            org = requested_org
        else:
            engine.require(len(organizations) == 1,
                           '未发现唯一的本地 Code 组织目录；请先打开一个本地 Code 会话，或在登记时指定 org。')
            org = organizations[0]
        entry = {'account': active, 'org': org}
        accounts = config['accounts']
        engine.require(alias not in accounts or accounts[alias] == entry,
                       '此别名已属于其他账号或组织；请使用不同别名。')
        engine.require(not any(value['account'] == active and name != alias for name, value in accounts.items()),
                       '当前账号已经登记为其他别名；不会覆盖原映射。')
        engine.require(engine.read_json(self.env.desktop / 'config.json').get('lastKnownAccountUuid') == active,
                       '登记期间登录账号发生变化，请重新登记。')
        engine.require((engine.digest(registry) if registry.exists() else None) == before,
                       '登记期间账号映射发生变化，请重试。')
        if alias in accounts:
            return {'ok': True, 'alias': alias, 'detail': '当前账号已经登记。'}
        config['accounts'][alias] = entry
        private_dir(registry.parent)
        if registry.exists():
            private_file(registry)
        engine.private_json(registry, config, replace=registry.exists())
        return {'ok': True, 'alias': alias, 'detail': '已在本机登记当前账号；没有保存或修改登录凭据。'}

    def latest_backup(self, alias):
        base = engine.safe(self.root / 'backups' / alias)
        if not base.exists():
            return None
        for folder in sorted(engine.safe(base, 'dir').iterdir(), reverse=True):
            engine.safe(folder, 'dir')
            manifest = engine.safe(folder / 'checkpoint.json')
            if manifest.exists():
                info = engine.read_json(private_file(manifest))
                if info.get('complete') is True and info.get('source') == alias:
                    return str(folder)
        return None

    def status(self):
        config = self.env.settings()
        issues = []
        running = self.env.running()
        version = self.env.version()
        supported = version in config['validated_desktop_versions']
        # Do not retain or emit the surrounding credential-bearing object.
        active_uuid = engine.read_json(self.env.desktop / 'config.json').get('lastKnownAccountUuid')
        active = next((alias for alias, data in config['accounts'].items()
                       if data['account'] == active_uuid), None)
        if not supported:
            issues.append('当前 Claude 版本尚未验证，请先完成版本适配。')
        if active is None:
            issues.append('当前账号未登记；请先在官方 Claude 中打开本地 Code 会话，再运行 history_service.py register 登记。')
        accounts = []
        for alias in config['accounts']:
            item = {'alias': alias, 'sessions': None, 'latest_backup': None,
                    'latest_backup_at': None, 'error': None}
            try:
                records, _ = engine.records_and_markers(self.env.folder(alias))
                item['sessions'] = len(records)
            except READ_ERRORS as exc:
                item['error'] = str(exc)
            try:
                item['latest_backup'] = self.latest_backup(alias)
                if item['latest_backup']:
                    checkpoint = engine.read_json(Path(item['latest_backup']) / 'checkpoint.json')
                    created = datetime.strptime(checkpoint['created_at'], '%Y%m%dT%H%M%S.%fZ')
                    item['latest_backup_at'] = created.replace(tzinfo=timezone.utc).isoformat()
            except READ_ERRORS as exc:
                item['error'] = item['error'] or str(exc)
            accounts.append(item)
        partial = any(item['error'] and item['alias'] == active for item in accounts)
        if partial:
            issues.append('部分账号历史无法安全解析，请查看对应错误；不会静默丢弃字段。')
        warnings = [item['alias'] + ': ' + item['error'] for item in accounts if item['error'] and item['alias'] != active]
        return {'ok': not issues, 'detail': ' '.join(issues) or '本地历史状态已读取。',
                'active_alias': active, 'version': version, 'claude_running': running,
                'supported_version': supported, 'accounts': accounts, 'backups': self.list_backups(), 'warnings': warnings}

    def inventory(self, alias, records, allow_missing=False):
        files, directories, ids = (available_files(self.env.folder(alias), self.env.projects, records)
                                  if allow_missing else engine.collect_files(self.env, alias, records))
        # Include history hints in the same fingerprinted inventory as indexes
        # so a live hint change cannot produce an inconsistent checkpoint.
        for path in self.env.folder(alias).iterdir():
            if path.name == 'archived-sessions.idx' or path.name.startswith('deleted_'):
                files[Path('source-index') / path.name] = engine.safe(path, 'file')
        return files, directories, ids

    def snapshot(self, alias, allow_missing=False):
        """Consistent hashes only: no private message text is serialized here."""
        folder = self.env.folder(alias)
        before = engine.directory_state(folder)
        records, markers = engine.records_and_markers(folder)
        archived = engine.archived_hints(folder)
        files, directories, ids = self.inventory(alias, records, allow_missing)
        entries = {}
        for relative, path in sorted(files.items()):
            start = engine.fingerprint(path)
            sha = engine.digest(path)
            engine.require(engine.fingerprint(path) == start, '历史在读取中变化，请重新预览。')
            entries[str(relative)] = {'sha256': sha, 'fingerprint': start}
        files_after, dirs_after, _ = self.inventory(alias, records, allow_missing)
        engine.require(set(files) == set(files_after) and directories == dirs_after,
                       '正文或附属文件清单发生变化，请重新预览。')
        for relative, path in files_after.items():
            engine.require(engine.fingerprint(path) == entries[str(relative)]['fingerprint'],
                           '历史在读取中变化，请重新预览。')
        engine.require(engine.directory_state(folder) == before, '账号索引发生变化，请重新预览。')
        result = {'directory': before, 'files': entries,
                  'directories': sorted(str(p) for p in directories), 'transcripts': len(ids)}
        if allow_missing:
            refs = set().union(*(engine.transcript_ids(r) for r in records.values())) if records else set()
            result['missing_transcripts'] = sorted(refs - ids)
        return result, records, markers, archived

    def checkpoint(self, alias, identity, allow_missing=False):
        """Stable current-point backup, even when the app remains open."""
        engine.require(self.identity() == identity, '账号或版本已变化，请重新操作。')
        snapshot, records, _, _ = self.snapshot(alias, allow_missing)
        output = self.root / 'backups' / alias / (stamp() + '-' + uuid.uuid4().hex[:8])
        private_dir(output)
        files, directories, ids = self.inventory(alias, records, allow_missing)
        entries = engine.backup_files(output, files, directories)
        engine.require({item['path']: {'sha256': item['sha256'], 'fingerprint': item['fingerprint']}
                        for item in entries} == snapshot['files'],
                       '备份内容与初始检查点不一致，请重试。')
        now, _, _, _ = self.snapshot(alias, allow_missing)
        engine.require(now == snapshot and self.identity() == identity,
                       '备份期间历史或账号变化；本次备份未标记完成，请重试。')
        result = {'path': str(output), 'records': len(records), 'transcripts': len(ids),
                  'files': len(entries), 'bytes': sum(item['bytes'] for item in entries),
                  'source': alias, 'backup_id': alias + '/' + output.name}
        engine.private_json(output / 'checkpoint.json', {
            'format': 1, 'complete': True, 'created_at': stamp(), 'source': alias,
            'identity': identity, 'state': snapshot, 'snapshot_files': entries,
            'source_account': self.env.settings()['accounts'][alias],
            'importable': not bool(snapshot.get('missing_transcripts')),
            'summary': result, 'claude_running': self.env.running(),
        })
        return result

    def backup(self, request):
        identity = self.identity()
        source = request.get('source') or identity['active_alias']
        engine.require(source == identity['active_alias'], '只能手动备份当前账号，请先核对官方登录账号。')
        result = self.checkpoint(source, identity)
        return {'ok': True, 'detail': '当前本地 Code 历史检查点已备份。', **result}

    def pair(self, request, stopped=False):
        identity = self.identity(stopped=stopped)
        source = request.get('source')
        target = request.get('target') or identity['active_alias']
        aliases = self.env.settings()['accounts']
        engine.require(isinstance(source, str) and source in aliases and target in aliases, '请选择已登记的来源和目标账号。')
        engine.require(source != target, '来源与目标账号必须不同。')
        engine.require(target == identity['active_alias'], '目标必须是当前官方登录账号，请先核对账号。')
        return identity, source, target

    def saved_copy_name(self, source, target, record):
        accounts = self.env.settings()['accounts']
        identity = ':'.join(('claude-saved-backup', accounts[source]['account'], accounts[target]['account'],
                             record['sessionId'], history_signature(record)))
        return 'local_' + str(uuid.uuid5(uuid.NAMESPACE_URL, identity)) + '.json'

    def classify(self, source, target, left, right, saved=False):
        _, sources, left_markers, left_archived = left
        _, targets, right_markers, right_archived = right
        markers, archived = left_markers | right_markers, left_archived | right_archived
        signatures = {}
        for name, record in targets.items():
            if not record['isArchived'] and record['sessionId'] not in archived:
                related = engine.transcript_ids(record) | {record['sessionId'], record['sessionId'][6:]}
                if not related & markers:
                    signatures.setdefault(history_signature(record), []).append(name)
        items, selected = [], {'missing': [], 'continuation': []}
        destinations = set(targets)
        for name, record in sources.items():
            existing = targets.get(name)
            related = engine.transcript_ids(record) | {record['sessionId'], record['sessionId'][6:]}
            if existing:
                related |= engine.transcript_ids(existing)
            signature = history_signature(record)
            kind, reason = 'missing', '目标缺少该入口，将新增独立索引。'
            destination = name
            if related & markers:
                kind, reason = 'deleted', '任一侧存在相关删除标记，已排除。'
            elif record['isArchived'] or record['sessionId'] in archived or (existing and existing['isArchived']):
                kind, reason = 'archived', '任一侧已归档，已排除。'
            elif existing and history_signature(existing) == signature:
                kind, reason = 'unchanged', '已有入口引用相同历史；标题、权限等保持各账号现状。'
            elif existing:
                destination = (self.saved_copy_name(source, target, record) if saved
                               else engine.copy_name(self.env, source, target, record))
                candidate = engine.copied_record(record, destination, source, 0)
                copied_signature = history_signature(candidate)
                copy_id = Path(destination).stem
                if {copy_id, copy_id[6:]} & markers:
                    kind, reason = 'deleted', '独立副本存在删除标记，已排除。'
                elif copy_id in archived:
                    kind, reason = 'archived', '独立副本已归档，已排除。'
                elif copied_signature in signatures:
                    kind, reason = 'equivalent', '目标已有等价的后续历史入口，避免往返重复。'
                elif destination in targets:
                    kind, reason = 'conflict', '已有后续副本同 ID 但历史不同；保留双方，需人工检查。'
                else:
                    label = '备份' if saved else '后续'
                    kind, reason = 'continuation', f'历史引用不同，将新增“{source} {label}”副本并保留原入口。'
            elif targets and signature in signatures:
                kind, reason = 'equivalent', '目标已有不同 ID 的等价历史入口，避免往返重复。'
            if kind in selected:
                if destination in destinations:
                    kind, reason = 'conflict', '本次多个候选使用同一目标 ID，需人工检查。'
                elif not Path(record['cwd']).is_dir():
                    kind, reason = 'conflict', '来源项目目录不可用，无法安全导入。'
                else:
                    destinations.add(destination)
                    selected[kind].append(name)
            title = record.get('title')
            items.append({'title': title if isinstance(title, str) else '未命名会话',
                          'kind': kind, 'reason': reason, 'session': name})
        return items, selected

    def preview(self, request):
        identity, source, target = self.pair(request)
        left, right = self.snapshot(source), self.snapshot(target)
        items, selected = self.classify(source, target, left, right)
        # Both account snapshots must still match after classification.
        engine.require(self.snapshot(source)[0] == left[0] and self.snapshot(target)[0] == right[0]
                       and self.identity() == identity, '历史或账号已变化，请重新预览。')
        counts = {kind: sum(item['kind'] == kind for item in items) for kind in
                  ('missing', 'continuation', 'unchanged', 'equivalent', 'archived', 'deleted', 'conflict')}
        can_sync = counts['conflict'] == 0 and counts['missing'] + counts['continuation'] > 0
        preview_id = str(uuid.uuid4())
        path = self.root / 'previews' / preview_id
        private_dir(path)
        record = {'format': 1, 'preview_id': preview_id, 'created': time.time(), 'identity': identity,
                  'source': source, 'target': target, 'source_state': left[0], 'target_state': right[0],
                  'candidates': selected, 'items': items, 'can_sync': can_sync}
        engine.private_json(path / 'preview.json', record)
        engine.private_json(path / 'checksum.json', {'sha256': engine.digest(path / 'preview.json')})
        detail = ('存在冲突，尚不能同步；请先检查冲突条目。' if counts['conflict'] else
                  f"将新增 {counts['missing']} 条缺失入口及 {counts['continuation']} 条独立后续副本。" if can_sync else
                  '可安全补齐的历史已齐全；归档和删除条目已排除。')
        return {'ok': True, 'detail': detail, 'preview_id': preview_id,
                'source': source, 'target': target, 'claude_running': self.env.running(),
                'can_sync': can_sync, 'items': items,
                **{kind + '_count': counts[kind] for kind in ('missing', 'continuation', 'unchanged', 'equivalent', 'conflict')},
                'skipped_count': counts['archived'] + counts['deleted']}

    def read_preview(self, request):
        value = request.get('preview_id')
        engine.require(isinstance(value, str) and engine.UUID.fullmatch(value), '请先生成并检查历史预览。')
        path = engine.safe(self.root / 'previews' / value, 'dir')
        preview = private_file(path / 'preview.json')
        checksum = engine.read_json(private_file(path / 'checksum.json'))
        engine.require(engine.digest(preview) == checksum.get('sha256'), '预览文件发生变化，请重新预览。')
        record = engine.read_json(preview)
        engine.require(record.get('format') == 1 and record.get('preview_id') == value, '预览格式不匹配。')
        engine.require(not (path / 'consumed.json').exists(), '该预览已用于同步，请重新预览。')
        engine.require(0 <= time.time() - record['created'] <= PREVIEW_LIFETIME, '预览已过期，请重新预览。')
        engine.require(record['source'] == request.get('source') and record['target'] == request.get('target'),
                       '来源或目标与已检查的预览不匹配。')
        return path, record

    def sync(self, request):
        identity, source, target = self.pair(request, stopped=True)
        preview_path, preview = self.read_preview(request)
        engine.require(identity == preview['identity'], '账号或版本已变化，请重新预览。')
        engine.require(preview['can_sync'] is True, '该预览没有可安全同步的候选，请重新检查。')
        left, right = self.snapshot(source), self.snapshot(target)
        engine.require(left[0] == preview['source_state'] and right[0] == preview['target_state'],
                       '来源或目标历史自预览后已变化，请重新预览。')
        items, candidates = self.classify(source, target, left, right)
        engine.require(items == preview['items'] and candidates == preview['candidates'], '候选发生变化，请重新预览。')
        target_backup = self.checkpoint(target, identity)
        source_backup = self.checkpoint(source, identity)
        engine.require(self.identity(stopped=True) == identity and
                       self.snapshot(source)[0] == left[0] and self.snapshot(target)[0] == right[0],
                       '备份后历史或账号发生变化，请重新预览。')
        transaction = self.root / 'transactions' / (stamp() + '-' + preview['preview_id'])
        private_dir(transaction)
        plans = {}
        for kind, names in candidates.items():
            if names:
                output = transaction / kind
                engine.plan_history(self.env, source, target, output, names, kind == 'continuation')
                plan = engine.read_json(output / 'plan.json')
                engine.require(len(plan['entries']) == len(names), '实际候选与预览不一致，请重新预览。')
                plans[kind] = output
        # All candidates have passed engine validation before the first write.
        engine.require(self.snapshot(source)[0] == left[0] and self.snapshot(target)[0] == right[0],
                       '准备期间历史发生变化，请重新预览。')
        engine.private_json(preview_path / 'consumed.json', {'transaction': str(transaction), 'at': stamp()})
        receipt = {'source': source, 'target': target, 'added': 0, 'status': 'in_progress',
                   'backup_path': target_backup['path'], 'source_backup_path': source_backup['path'],
                   'transaction_path': str(transaction), 'preview_id': preview['preview_id']}
        engine.private_json(transaction / 'summary.json', receipt)
        expected_target_directory = dict(right[0]['directory'])
        try:
            for kind, output in plans.items():
                engine.require(self.identity(stopped=True) == identity, '账号或运行状态发生变化。')
                current = self.snapshot(target)[0]
                engine.require(self.snapshot(source)[0] == left[0] and current['directory'] == expected_target_directory,
                               '同步期间历史发生变化，已停止后续写入。')
                # Original target transcript and index fingerprints must remain
                # identical; only this transaction's appended files may appear.
                engine.require(all(current['files'].get(key) == value for key, value in right[0]['files'].items()),
                               '目标原历史发生变化，已停止后续写入。')
                if receipt['added']:
                    # The first batch appended the reviewed missing entries.
                    # Re-plan the already reviewed second batch against that
                    # exact expected directory, without relaxing any guard.
                    output = transaction / (kind + '-after-append')
                    engine.plan_history(self.env, source, target, output, candidates[kind], kind == 'continuation')
                plan = engine.read_json(output / 'plan.json')
                applied = engine.apply_history(self.env, output)
                receipt['added'] += len(applied['added'])
                for entry in plan['entries']:
                    expected_target_directory[entry['name']] = {'kind': 'file', 'sha256': entry['sha256']}
                engine.private_json(transaction / 'summary.json', receipt, replace=True)
            final = self.snapshot(target)[0]
            engine.require(self.identity(stopped=True) == identity and self.snapshot(source)[0] == left[0]
                           and final['directory'] == expected_target_directory
                           and all(final['files'].get(k) == v for k, v in right[0]['files'].items()),
                           '同步后检查发现变化，请检查事务记录。')
            receipt['status'] = 'applied'
            engine.private_json(transaction / 'summary.json', receipt, replace=True)
            return {'ok': True, **receipt, 'detail': f"已新增 {receipt['added']} 条本地 Code 入口；权限为 Manual，未确认续跑。"}
        except Exception as exc:
            # Engine receipts retain exact paths if a write stopped mid-batch.
            added = sum(len(engine.read_json(p).get('added', []))
                        for p in transaction.glob('*/receipt-*.json'))
            receipt.update(status='partial' if added else 'failed', added=added)
            engine.private_json(transaction / 'summary.json', receipt, replace=True)
            return {'ok': False, **receipt,
                    'detail': f'同步已停止，已新增 {added} 条；请查看事务记录后重新预览。原因：{exc}'}

    def read_backup(self, backup_id, full=True):
        """Resolve an opaque catalog ID, never a caller-supplied filesystem path."""
        engine.require(isinstance(backup_id, str) and backup_id.count('/') == 1, '请选择保存的备份。')
        source, name = backup_id.split('/')
        config = self.env.settings()
        engine.require(source in config['accounts'] and BACKUP_NAME.fullmatch(name), '备份标识无效。')
        root = engine.safe(self.root / 'backups' / source / name, 'dir')
        manifest_path = private_file(root / 'checkpoint.json')
        manifest = engine.read_json(manifest_path)
        engine.require(manifest.get('format') == 1 and manifest.get('complete') is True
                       and manifest.get('source') == source, '备份尚未完成或格式不受支持。')
        engine.require(manifest.get('importable', True) is True and not manifest['state'].get('missing_transcripts'),
                       '该记录是缺失正文的导入前现场备份，不能作为完整导入来源。')
        identity = manifest['identity']
        engine.require(identity.get('version') in config['validated_desktop_versions'], '备份来源版本尚未验证。')
        provenance = manifest.get('source_account')
        if provenance is None:
            engine.require(identity.get('accounts_sha256') == engine.digest(self.env.accounts_path),
                           '旧备份缺少可核验的账号映射，请检查备份来源。')
        else:
            engine.require(provenance == config['accounts'][source], '备份来源账号映射不匹配。')
        created = datetime.strptime(manifest['created_at'], '%Y%m%dT%H%M%S.%fZ').replace(tzinfo=timezone.utc).isoformat()
        summary = manifest['summary']
        metadata = {'backup_id': backup_id, 'source': source, 'created_at': created,
                    'records': summary['records'], 'bytes': summary['bytes'], 'path': str(root)}
        engine.require(type(summary['records']) is int and summary['records'] >= 0
                       and type(summary['bytes']) is int and summary['bytes'] >= 0, '备份统计格式不正确。')
        result = {'root': root, 'manifest': manifest, 'metadata': metadata}
        if not full:
            return result
        entries = manifest['snapshot_files']
        engine.require(isinstance(entries, list), '备份文件清单无效。')
        listed, state, expected_dirs = {}, {}, set()
        for entry in entries:
            raw = entry['path']
            engine.require(isinstance(raw, str), '备份路径无效。')
            relative = Path(raw)
            engine.require(not relative.is_absolute() and '..' not in relative.parts and raw == relative.as_posix()
                           and raw not in listed and len(relative.parts) >= 2
                           and relative.parts[0] in ('source-index', 'projects'), '备份含无效或重复路径。')
            path = private_file(root / 'snapshot' / relative)
            fp, digest = engine.fingerprint(path), engine.digest(path)
            engine.require(digest == entry['sha256'] and fp[2] == entry['bytes'], '备份文件校验失败，未执行导入。')
            if path.suffix == '.jsonl':
                engine.validate_jsonl(path)
            listed[raw] = entry
            state[raw] = {'sha256': digest, 'fingerprint': fp}
            expected_dirs.update(str(p.relative_to(root)) for p in path.parents if root in p.parents)
        index_folder = root / 'snapshot/source-index'
        if index_folder.exists():
            records, markers = engine.records_and_markers(index_folder)
            archived = engine.archived_hints(index_folder)
        else:
            records, markers, archived = {}, set(), set()
        engine.require(len(records) == summary['records'], '备份会话数不匹配。')
        refs = set().union(*(engine.transcript_ids(r) for r in records.values())) if records else set()
        found = set()
        for raw in listed:
            parts = Path(raw).parts
            if parts[0] == 'source-index':
                engine.require(len(parts) == 2 and (parts[1] in records or parts[1] == 'archived-sessions.idx'
                               or parts[1].startswith('deleted_')), '备份包含不允许的索引文件。')
            else:
                engine.require(len(parts) >= 3 and parts[1] not in ('.', '..'), '备份正文路径无效。')
                sid = Path(parts[2]).stem if len(parts) == 3 and parts[2].endswith('.jsonl') else parts[2]
                engine.require(sid in refs and ((len(parts) == 3 and parts[2] == sid + '.jsonl') or len(parts) >= 4),
                               '备份包含未引用的正文或附属文件。')
                if len(parts) == 3:
                    found.add(sid)
        engine.require(found == refs, '备份缺少引用正文，不能导入。')
        recorded_dirs = manifest['state']['directories']
        for raw in recorded_dirs:
            relative = Path(raw)
            parts = relative.parts
            engine.require(raw == relative.as_posix() and not relative.is_absolute() and '..' not in parts
                           and len(parts) >= 3 and parts[0] == 'projects' and parts[2] in refs,
                           '备份附属目录路径无效。')
            directory = engine.safe(root / 'snapshot' / relative, 'dir')
            expected_dirs.add(str(directory.relative_to(root)))
            expected_dirs.update(str(p.relative_to(root)) for p in directory.parents if root in p.parents)
        expected_files = {'checkpoint.json'} | {'snapshot/' + raw for raw in listed}
        actual_files, actual_dirs = set(), set()
        for path in [root, *root.rglob('*')]:
            engine.safe(path)
            meta = path.stat()
            engine.require(meta.st_uid == os.getuid() and stat.S_IMODE(meta.st_mode) == (0o700 if path.is_dir() else 0o600),
                           '备份权限必须为私有的 700/600。')
            if path == root:
                continue
            if path.is_dir():
                actual_dirs.add(str(path.relative_to(root)))
            else:
                private_file(path)
                actual_files.add(str(path.relative_to(root)))
        engine.require(actual_files == expected_files and actual_dirs == expected_dirs, '备份实际文件清单发生变化。')
        original_state = manifest['state']['files']
        engine.require(set(original_state) == set(listed) and all(
            original_state[key]['sha256'] == entry['sha256'] and original_state[key]['fingerprint'] == entry['fingerprint']
            for key, entry in listed.items()), '备份检查点清单不一致。')
        for key, item in state.items():
            engine.require(engine.fingerprint(root / 'snapshot' / key) == item['fingerprint'], '校验期间备份发生变化。')
        result.update(records=records, markers=markers, archived=archived, entries=listed,
                      directories=recorded_dirs, token=json_hash({'manifest': engine.digest(manifest_path), 'files': state}))
        return result

    def list_backups(self):
        result = []
        for alias in self.env.settings()['accounts']:
            base = engine.safe(self.root / 'backups' / alias)
            if not base.exists():
                continue
            for folder in engine.safe(base, 'dir').iterdir():
                if BACKUP_NAME.fullmatch(folder.name):
                    try:
                        result.append(self.read_backup(alias + '/' + folder.name, full=False)['metadata'])
                    except READ_ERRORS:
                        continue
        return sorted(result, key=lambda item: item['created_at'], reverse=True)

    def destination_state(self, path, directory=False):
        engine.safe(path)
        if not path.exists():
            return {'kind': 'missing'}
        if directory:
            return {'kind': 'dir'} if path.is_dir() else {'kind': 'conflict'}
        if not path.is_file() or path.stat().st_nlink != 1:
            return {'kind': 'conflict'}
        before = engine.fingerprint(path)
        digest = engine.digest(path)
        engine.require(engine.fingerprint(path) == before, '目标正文正在变化，请重新预览。')
        return {'kind': 'file', 'sha256': digest, 'fingerprint': before}

    def body_path(self, relative):
        parts = Path(relative).parts
        engine.require(parts and parts[0] == 'projects' and '..' not in parts, '恢复路径无效。')
        return engine.safe(self.env.projects.joinpath(*parts[1:]))

    @staticmethod
    def is_prefix(backup, current):
        engine.validate_jsonl(backup)
        engine.validate_jsonl(current)
        with backup.open('rb') as left, current.open('rb') as right:
            while True:
                chunk = left.read(1024 * 1024)
                if not chunk:
                    return True
                if right.read(len(chunk)) != chunk:
                    return False

    def saved_plan(self, request, indexed_at=None):
        identity = self.identity()
        target = request.get('target') or identity['active_alias']
        engine.require(target == identity['active_alias'], '目标必须是当前官方登录账号。')
        backup = self.read_backup(request.get('backup_id'))
        source = backup['metadata']['source']
        right = self.snapshot(target, allow_missing=True)
        left = ({}, backup['records'], backup['markers'], backup['archived'])
        items, candidates = self.classify(source, target, left, right, saved=True)
        eligible = {i['session'] for i in items if i['kind'] in ('missing', 'continuation', 'unchanged', 'equivalent')}
        refs = set().union(*(engine.transcript_ids(backup['records'][name]) for name in eligible)) if eligible else set()
        bodies, directories = {}, set()
        for raw, entry in backup['entries'].items():
            parts = Path(raw).parts
            if parts[0] != 'projects':
                continue
            sid = Path(parts[2]).stem if len(parts) == 3 else parts[2]
            if sid not in refs:
                continue
            path = self.body_path(raw)
            current = self.destination_state(path)
            action = 'restore' if current['kind'] == 'missing' else 'conflict'
            if current['kind'] == 'file':
                if current['sha256'] == entry['sha256']:
                    action = 'equal'
                elif path.suffix == '.jsonl' and current['fingerprint'][2] > entry['bytes']:
                    if self.is_prefix(backup['root'] / 'snapshot' / raw, path):
                        action = 'newer'
                engine.require(self.destination_state(path) == current, '目标正文正在变化，请重新预览。')
            bodies[raw] = {'state': current, 'action': action, 'sha256': entry['sha256']}
            parent = Path(raw).parent
            while parent != Path('.'):
                directories.add(str(parent)); parent = parent.parent
        for raw in backup['directories']:
            if Path(raw).parts[2] in refs:
                directory = Path(raw)
                while directory != Path('.'):
                    directories.add(str(directory)); directory = directory.parent
        dir_state = {raw: self.destination_state(self.body_path(raw), directory=True) for raw in sorted(directories)}
        conflicts = [raw for raw, entry in bodies.items() if entry['action'] == 'conflict']
        conflicts += [raw for raw, entry in dir_state.items() if entry['kind'] == 'conflict']
        for raw in conflicts:
            items.append({'title': '正文或附属文件冲突', 'kind': 'conflict', 'reason': '当前内容与备份分歧，保留当前文件并停止导入。', 'file': raw})
        prepared = {}
        indexed_at = int(time.time() * 1000) if indexed_at is None else indexed_at
        engine.require(type(indexed_at) is int and 0 <= indexed_at <= int(time.time() * 1000) + 60000,
                       '导入索引时间无效。')
        for kind, names in candidates.items():
            for name in names:
                record = backup['records'][name]
                if kind == 'missing':
                    prepared[name] = engine.sanitized(record, indexed_at)
                else:
                    destination = self.saved_copy_name(source, target, record)
                    record = engine.copied_record(record, destination, source, indexed_at)
                    record['title'] = backup['records'][name].get('title', '未命名会话') + f'（{source} 备份）'
                    prepared[destination] = record
        counts = {kind: sum(i['kind'] == kind for i in items) for kind in
                  ('missing', 'continuation', 'unchanged', 'equivalent', 'archived', 'deleted', 'conflict')}
        restores = sum(i['action'] == 'restore' for i in bodies.values())
        newer = sum(i['action'] == 'newer' for i in bodies.values())
        engine.require(self.snapshot(target, allow_missing=True)[0] == right[0] and self.identity() == identity,
                       '目标历史或账号正在变化，请重新预览。')
        engine.require(self.read_backup(request['backup_id'])['token'] == backup['token'], '备份正在变化，请重新预览。')
        for raw, entry in bodies.items():
            engine.require(self.destination_state(self.body_path(raw)) == entry['state'], '目标正文正在变化，请重新预览。')
        return {'format': 2, 'kind': 'saved-backup', 'identity': identity, 'indexed_at': indexed_at, 'backup_id': request['backup_id'],
                'backup_token': backup['token'], 'source': source, 'target': target,
                'backup_created_at': backup['metadata']['created_at'], 'target_state': right[0],
                'bodies': bodies, 'body_directories': dir_state, 'prepared': prepared, 'items': items,
                'counts': counts, 'restore_files_count': restores, 'retained_newer_count': newer,
                'can_sync': counts['conflict'] == 0 and (bool(prepared) or restores > 0)}

    def preview_saved(self, request):
        plan = self.saved_plan(request)
        plan.update(preview_id=str(uuid.uuid4()), created=time.time())
        path = self.root / 'previews' / plan['preview_id']
        private_dir(path)
        engine.private_json(path / 'preview.json', plan)
        engine.private_json(path / 'checksum.json', {'sha256': engine.digest(path / 'preview.json')})
        counts = plan['counts']
        return {'ok': True, **{key: plan[key] for key in ('preview_id', 'backup_id', 'source', 'target', 'backup_created_at',
                                                        'items', 'can_sync', 'restore_files_count', 'retained_newer_count')},
                'restore_file_count': plan['restore_files_count'], 'claude_running': self.env.running(),
                **{kind + '_count': counts[kind] for kind in ('missing', 'continuation', 'unchanged', 'equivalent', 'conflict')},
                'skipped_count': counts['archived'] + counts['deleted'],
                'detail': ('当前内容与备份存在冲突，未执行导入。' if counts['conflict'] else
                           f"将新增 {len(plan['prepared'])} 个入口、恢复 {plan['restore_files_count']} 个文件；保留 {plan['retained_newer_count']} 个较新文件。")}

    def verify_saved_target(self, plan, expected_directory, expected_bodies, expected_dirs):
        engine.require(self.identity(stopped=True) == plan['identity'], '当前账号、版本或运行状态发生变化。')
        current = self.snapshot(plan['target'], allow_missing=True)[0]
        engine.require(current['directory'] == expected_directory, '当前账号索引发生变化，请重新预览。')
        for raw, original in plan['target_state']['files'].items():
            engine.require(current['files'].get(raw) == original, '当前原有历史发生变化，请重新预览。')
        allowed_files = set(plan['target_state']['files']) | set(expected_bodies) | {
            'source-index/' + name for name in expected_directory if engine.SESSION.fullmatch(name)}
        engine.require(set(current['files']) <= allowed_files, '当前历史文件清单发生变化，请重新预览。')
        for raw, expected in expected_bodies.items():
            engine.require(self.destination_state(self.body_path(raw)) == expected, '恢复目标文件发生变化，请重新预览。')
        for raw, expected in expected_dirs.items():
            engine.require(self.destination_state(self.body_path(raw), directory=True) == expected, '恢复目标目录发生变化，请重新预览。')
        engine.require(self.identity(stopped=True) == plan['identity'], '发布前账号、版本或运行状态发生变化。')

    @staticmethod
    def append_file(source, target, expected_sha256, before_publish=None):
        engine.safe(target)
        fd, temporary_name = tempfile.mkstemp(prefix='.history-restore-', dir=target.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb') as output, source.open('rb') as original:
                for chunk in iter(lambda: original.read(1024 * 1024), b''):
                    output.write(chunk)
                output.flush(); os.fsync(output.fileno())
            engine.require(engine.digest(temporary) == expected_sha256 and engine.digest(source) == expected_sha256,
                           '恢复暂存文件校验失败。')
            if before_publish is not None:
                before_publish()
            os.link(temporary, target, follow_symlinks=False)
        finally:
            temporary.unlink(missing_ok=True)
        engine.require(target.stat().st_nlink == 1, '恢复文件不是独立文件。')

    def sync_saved(self, request):
        identity = self.identity(stopped=True)
        value = request.get('preview_id')
        engine.require(isinstance(value, str) and engine.UUID.fullmatch(value), '请先生成导入预览。')
        path = engine.safe(self.root / 'previews' / value, 'dir')
        saved_file = private_file(path / 'preview.json')
        engine.require(engine.digest(saved_file) == engine.read_json(private_file(path / 'checksum.json'))['sha256'], '预览文件已变化。')
        plan = engine.read_json(saved_file)
        engine.require(plan.get('format') == 2 and plan.get('kind') == 'saved-backup'
                       and plan.get('backup_id') == request.get('backup_id') and plan.get('target') == request.get('target'),
                       '备份、目标或预览类型不匹配，请重新预览。')
        engine.require(plan['identity'] == identity and plan['target'] == identity['active_alias'], '当前账号或版本与预览不匹配。')
        engine.require(0 <= time.time() - plan['created'] <= PREVIEW_LIFETIME and not (path / 'consumed.json').exists(), '预览过期或已使用，请重新预览。')
        engine.require(plan['can_sync'], '预览没有可安全导入的内容或存在冲突。')
        backup = self.read_backup(plan['backup_id'])
        engine.require(backup['token'] == plan['backup_token'], '保存的备份已变化，请重新预览。')
        engine.require(self.snapshot(plan['target'], allow_missing=True)[0] == plan['target_state'], '当前历史自预览后发生变化。')
        expected_directory = dict(plan['target_state']['directory'])
        expected_bodies = {raw: entry['state'] for raw, entry in plan['bodies'].items()}
        expected_dirs = dict(plan['body_directories'])
        self.verify_saved_target(plan, expected_directory, expected_bodies, expected_dirs)
        # A checksum alone is not schema authorization. Re-derive every index,
        # destination and restore decision from the verified backup and current
        # target, just as the append-only engine re-derives sanitized indexes.
        derived = self.saved_plan(request, indexed_at=plan['indexed_at'])
        engine.require(derived == {key: val for key, val in plan.items() if key not in ('created', 'preview_id')},
                       '预览内容与备份可安全导入的内容不一致，请重新预览。')
        before = self.checkpoint(plan['target'], identity, allow_missing=True)
        self.verify_saved_target(plan, expected_directory, expected_bodies, expected_dirs)
        transaction = self.root / 'transactions' / (stamp() + '-' + value)
        private_dir(transaction)
        engine.private_json(transaction / 'restore-plan.json', plan)
        private_dir(transaction / 'indexes')
        for name, record in plan['prepared'].items():
            engine.private_json(transaction / 'indexes' / name, record)
        # Re-read every immutable source file before the first target mutation.
        engine.require(self.read_backup(plan['backup_id'])['token'] == plan['backup_token'], '保存的备份已变化。')
        engine.private_json(path / 'consumed.json', {'transaction': str(transaction), 'at': stamp()})
        receipt = {'source': plan['source'], 'target': plan['target'], 'backup_id': plan['backup_id'],
                   'backup_path': before['path'], 'transaction_path': str(transaction), 'preview_id': value, 'status': 'in_progress',
                   'added': 0, 'restored_files': 0, 'retained_newer_count': plan['retained_newer_count'],
                   'added_indexes': [], 'restored_paths': [], 'created_directories': []}
        receipt_file = transaction / 'summary.json'
        engine.private_json(receipt_file, receipt)
        def publish_guard():
            # A same-directory temporary file is present until atomic publish;
            # full inventory was checked before staging, and is checked again
            # after it. Recheck process/account immediately before publishing.
            engine.require(self.identity(stopped=True) == plan['identity'], '发布前账号、版本或运行状态发生变化。')
        try:
            for raw in sorted(expected_dirs, key=lambda item: (len(Path(item).parts), item)):
                if expected_dirs[raw]['kind'] != 'missing':
                    continue
                self.verify_saved_target(plan, expected_directory, expected_bodies, expected_dirs)
                destination = self.body_path(raw)
                destination.mkdir(mode=0o700)
                expected_dirs[raw] = {'kind': 'dir'}
                receipt['created_directories'].append(raw)
                engine.private_json(receipt_file, receipt, replace=True)
            for raw, entry in plan['bodies'].items():
                if entry['action'] != 'restore':
                    continue
                self.verify_saved_target(plan, expected_directory, expected_bodies, expected_dirs)
                source = private_file(backup['root'] / 'snapshot' / raw)
                engine.require(engine.digest(source) == entry['sha256'], '备份文件在恢复期间发生变化。')
                receipt['pending_write'] = {'kind': 'body', 'path': raw, 'sha256': entry['sha256']}
                engine.private_json(receipt_file, receipt, replace=True)
                self.append_file(source, self.body_path(raw), entry['sha256'], before_publish=publish_guard)
                receipt['restored_paths'].append(raw)
                receipt['restored_files'] += 1
                receipt.pop('pending_write', None)
                engine.private_json(receipt_file, receipt, replace=True)
                expected_bodies[raw] = self.destination_state(self.body_path(raw))
                engine.require(expected_bodies[raw]['sha256'] == entry['sha256'], '恢复文件校验失败。')
            for name, record in plan['prepared'].items():
                self.verify_saved_target(plan, expected_directory, expected_bodies, expected_dirs)
                prepared = private_file(transaction / 'indexes' / name)
                engine.require(engine.read_json(prepared) == record, '待导入索引发生变化。')
                destination = self.env.folder(plan['target']) / name
                digest = engine.digest(prepared)
                receipt['pending_write'] = {'kind': 'index', 'path': name, 'sha256': digest}
                engine.private_json(receipt_file, receipt, replace=True)
                self.append_file(prepared, destination, digest, before_publish=publish_guard)
                receipt['added_indexes'].append(name)
                receipt['added'] += 1
                receipt.pop('pending_write', None)
                engine.private_json(receipt_file, receipt, replace=True)
                expected_directory[name] = {'kind': 'file', 'sha256': engine.digest(prepared)}
            self.verify_saved_target(plan, expected_directory, expected_bodies, expected_dirs)
            engine.require(self.read_backup(plan['backup_id'])['token'] == plan['backup_token'], '备份在导入期间发生变化，请检查事务记录。')
            receipt['status'] = 'applied'
            engine.private_json(receipt_file, receipt, replace=True)
            return {'ok': True, **receipt, 'detail': f"已新增 {receipt['added']} 个入口、恢复 {receipt['restored_files']} 个文件；保留当前较新正文，新增入口为 Manual、未确认续跑。"}
        except Exception as exc:
            receipt['status'] = 'partial' if (receipt['added'] or receipt['restored_files'] or receipt['created_directories']
                                            or receipt.get('pending_write')) else 'failed'
            engine.private_json(receipt_file, receipt, replace=True)
            return {'ok': False, **receipt, 'detail': f"导入已停止，已新增 {receipt['added']} 个入口、恢复 {receipt['restored_files']} 个文件。请检查事务记录后重新预览：{exc}"}

    def execute(self, command, request=None):
        request = {} if request is None else request
        engine.require(isinstance(request, dict), '输入必须是 JSON 对象。')
        if command == 'register':
            with self.operation():
                return self.register(request)
        engine.require(set(request) <= {'source', 'target', 'preview_id', 'backup_id'}, '存在不支持的输入字段。')
        engine.require(not ('source' in request and 'backup_id' in request), '不能同时选择实时账号和保存的备份。')
        if command == 'status':
            return self.status()
        engine.require(command in ('backup', 'preview', 'sync'), '未知历史操作。')
        with self.operation():
            if 'backup_id' in request:
                engine.require(command in ('preview', 'sync'), '备份操作只能针对当前账号。')
                return self.preview_saved(request) if command == 'preview' else self.sync_saved(request)
            return getattr(self, command)(request)


def main(argv=None, service=None):
    os.umask(0o077)
    args = sys.argv[1:] if argv is None else argv
    try:
        engine.require(len(args) == 1 and args[0] in ('status', 'register', 'backup', 'preview', 'sync'),
                       '用法：history_service.py status|register|backup|preview|sync')
        command = args[0]
        request = {}
        if command != 'status':
            raw = sys.stdin.read(65537)
            engine.require(len(raw) <= 65536, '输入过长。')
            request = json.loads(raw or '{}')
            if command in ('preview', 'sync'):
                engine.require(isinstance(request, dict) and 'backup_id' in request and 'source' not in request,
                               '请选择保存的备份；导入不读取实时来源账号。')
        result = (service or HistoryService()).execute(command, request)
    except READ_ERRORS as exc:
        result = {'ok': False, 'detail': str(exc)}
    except Exception:
        result = {'ok': False, 'detail': '无法可靠读取本机状态，操作已停止；请重新检查。'}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())

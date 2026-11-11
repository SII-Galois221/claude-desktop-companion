"""Synthetic-only tests; no installed Claude data, authentication or processes."""
from contextlib import redirect_stdout
import io
import json
import math
from pathlib import Path
import plistlib
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import history_service as service
import history_sync_engine as engine


def uid(value):
    return f'00000000-0000-4000-8000-{value:012d}'


class HistoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='guard-history-synthetic-')
        self.base = Path(self.temp.name).resolve()
        self.desktop = self.base / 'Desktop'
        self.projects = self.base / 'projects'
        self.app = self.base / 'Claude.app'
        self.work = self.base / 'workspace'
        self.work.mkdir()
        self.projects.mkdir()
        (self.app / 'Contents').mkdir(parents=True)
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': '5.1231.2'}))
        self.mapping = {'validated_desktop_versions': ['5.1231.2'], 'accounts': {
            alias: {'account': uid(i), 'org': uid(i + 10)}
            for i, alias in enumerate(('personal', 'work', 'research'), 1)}}
        self.accounts = self.base / 'accounts.json'
        self.accounts.write_text(json.dumps(self.mapping))
        self.accounts.chmod(0o600)
        for account in self.mapping['accounts'].values():
            (self.desktop / 'claude-code-sessions' / account['account'] / account['org']).mkdir(parents=True)
        self.set_active('research')
        self.env = engine.Environment(self.desktop, self.projects, self.app, self.accounts)
        self.running = False
        self.env.running = lambda: self.running
        self.root = self.base / 'private-history'
        self.service = service.HistoryService(self.env, self.root)
        self.request = {'source': 'work', 'target': 'research'}

    def tearDown(self):
        self.temp.cleanup()

    def set_active(self, alias):
        (self.desktop / 'config.json').write_text(json.dumps({
            'lastKnownAccountUuid': self.mapping['accounts'][alias]['account'],
            'accessToken': 'SYNTHETIC-SECRET-NEVER-COPY'}))

    def put_transcript(self, number):
        project = self.projects / 'project'
        project.mkdir(exist_ok=True)
        transcript = project / (uid(number) + '.jsonl')
        if not transcript.exists():
            transcript.write_text('{"type":"user","message":"synthetic private body"}\n')
        companion = project / uid(number) / 'subagents'
        companion.mkdir(parents=True, exist_ok=True)
        (companion / 'agent.jsonl').write_text('{"type":"assistant","message":"synthetic companion"}\n')
        return transcript

    def add(self, alias='work', number=100, transcript=None, **extra):
        transcript = transcript or number
        name = 'local_' + uid(number) + '.json'
        record = {'sessionId': name[:-5], 'cliSessionId': uid(transcript), 'priorCliSessionIds': [],
                  'isArchived': False, 'cwd': str(self.work), 'title': 'Synthetic title',
                  'permissionMode': 'bypassPermissions', 'sessionPermissionUpdates': ['unsafe'],
                  'alwaysAllowedReasons': ['unsafe'], 'resumeConfirmed': True,
                  'spawnSeed': {'systemPrompt': 'never inherit'}, 'isRunning': True,
                  'interruptedByQuitAt': 100, 'stagedTranscriptPath': '/never/read', **extra}
        self.write(alias, name, record)
        self.put_transcript(transcript)
        return name

    def write(self, alias, name, record):
        (self.env.folder(alias) / name).write_text(json.dumps(record))

    def record(self, alias, name):
        return engine.read_json(self.env.folder(alias) / name)

    def preview(self):
        return self.service.execute('preview', self.request)

    def sync(self, preview):
        return self.service.execute('sync', {**self.request, 'preview_id': preview['preview_id']})

    def test_default_environment_uses_supported_system_app_location(self):
        with patch.object(engine.Path, 'home', return_value=self.base):
            self.assertEqual(engine.Environment().app, Path('/Applications/Claude.app'))

    def test_missing_app_has_clear_installation_error(self):
        (self.app / 'Contents/Info.plist').unlink()
        with self.assertRaisesRegex(RuntimeError, '/Applications/Claude.app'):
            self.service.execute('status')
        with self.assertRaisesRegex(RuntimeError, '/Applications/Claude.app'):
            self.service.execute('register', {'alias': 'research'})

    def test_missing_registry_is_read_only_empty_state(self):
        self.accounts.unlink()
        result = self.service.execute('status')
        self.assertFalse(result['ok'])
        self.assertIsNone(result['active_alias'])
        self.assertEqual(result['accounts'], [])
        self.assertEqual(result['backups'], [])
        self.assertIn('register', result['detail'])
        self.assertFalse(self.accounts.exists())
        self.assertFalse(self.root.exists())

    def test_register_first_account_does_not_copy_credentials(self):
        self.accounts.unlink()
        before = (self.desktop / 'config.json').read_bytes()
        result = self.service.execute('register', {'alias': 'primary'})
        self.assertTrue(result['ok'])
        config = engine.read_json(self.accounts)
        self.assertEqual(config['accounts'], {'primary': self.mapping['accounts']['research']})
        self.assertEqual(stat.S_IMODE(self.accounts.stat().st_mode), 0o600)
        self.assertNotIn('SYNTHETIC-SECRET', self.accounts.read_text())
        self.assertNotIn('SYNTHETIC-SECRET', json.dumps(result))
        self.assertEqual((self.desktop / 'config.json').read_bytes(), before)
        self.assertTrue(self.service.execute('status')['ok'])

    def test_register_single_account_can_backup(self):
        self.add('research')
        self.accounts.unlink()
        self.service.execute('register', {'alias': 'research'})
        result = self.service.execute('backup', {})
        self.assertEqual(result['records'], 1)
        self.assertEqual(result['source'], 'research')

    def test_register_is_idempotent_and_never_renames_existing_alias(self):
        before = self.accounts.read_bytes()
        self.assertTrue(self.service.execute('register', {'alias': 'research'})['ok'])
        self.assertEqual(self.accounts.read_bytes(), before)
        with self.assertRaisesRegex(RuntimeError, '其他别名'):
            self.service.execute('register', {'alias': 'newalias'})
        self.assertEqual(self.accounts.read_bytes(), before)

    def test_register_rejects_alias_collision_and_invalid_alias(self):
        for alias in ['personal', '../invalid', 'UPPERCASE', 'a' * 33, '']:
            with self.subTest(alias=alias), self.assertRaises(RuntimeError):
                self.service.execute('register', {'alias': alias})

    def test_register_rejects_missing_code_folder_without_creating_one(self):
        folder = self.env.folder('research').parent
        shutil.rmtree(folder)
        with self.assertRaisesRegex(RuntimeError, '本地 Code'):
            self.service.execute('register', {'alias': 'research'})
        self.assertFalse(folder.exists())

    def test_register_requires_explicit_org_when_ambiguous(self):
        folder = self.env.folder('research').parent
        (folder / uid(998)).mkdir()
        with self.assertRaisesRegex(RuntimeError, '唯一'):
            self.service.execute('register', {'alias': 'research'})
        self.assertTrue(self.service.execute('register', {'alias': 'research', 'org': uid(13)})['ok'])
        with self.assertRaisesRegex(RuntimeError, 'org'):
            self.service.execute('register', {'alias': 'research', 'org': '../escape'})

    def test_register_refuses_unreviewed_desktop_version(self):
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': '99.0.0'}))
        with self.assertRaisesRegex(RuntimeError, '尚未验证'):
            self.service.execute('register', {'alias': 'research'})

    def test_register_does_not_accept_authentication_or_version_settings(self):
        for extra in [{'accessToken': 'fake'}, {'validated_desktop_versions': ['99.0.0']}]:
            with self.assertRaisesRegex(RuntimeError, '只接受'):
                self.service.execute('register', {'alias': 'research', **extra})

    def test_register_refuses_symlink_organization_directory(self):
        folder = self.env.folder('research').parent
        (folder / uid(998)).symlink_to(self.work, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            self.service.execute('register', {'alias': 'research'})

    def test_register_rechecks_identity_before_writing_registry(self):
        self.accounts.unlink()
        original = engine.read_json
        calls = 0
        def changed(path):
            nonlocal calls
            result = original(path)
            if path == self.desktop / 'config.json':
                calls += 1
                if calls > 1:
                    result['lastKnownAccountUuid'] = uid(999)
            return result
        with patch.object(engine, 'read_json', side_effect=changed), self.assertRaisesRegex(RuntimeError, '发生变化'):
            self.service.execute('register', {'alias': 'primary'})
        self.assertFalse(self.accounts.exists())



    def test_status_is_read_only_and_has_three_accounts(self):
        self.add()
        result = self.service.execute('status')
        self.assertTrue(result['ok'])
        self.assertEqual(result['active_alias'], 'research')
        self.assertEqual([a['alias'] for a in result['accounts']], ['personal', 'work', 'research'])
        self.assertEqual(result['accounts'][1]['sessions'], 1)
        self.assertFalse(self.root.exists())

    def test_running_check_refuses_second_claude_installation(self):
        env = service.Environment(self.desktop, self.projects, self.app, self.accounts)
        with patch.object(engine.subprocess, 'run', return_value=SimpleNamespace(returncode=0)), \
                patch('watchdog_core.snapshot', return_value=[]):
            self.assertTrue(env.running())

    def test_running_check_includes_orphan_desktop_code_process(self):
        env = service.Environment(self.desktop, self.projects, self.app, self.accounts)
        process = SimpleNamespace(path=str(self.desktop / 'claude-code/version/bundle/claude.app/Contents/MacOS/claude'))
        with patch.object(engine.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), \
                patch('watchdog_core.snapshot', return_value=[process]):
            self.assertTrue(env.running())

    def test_status_isolates_unsupported_account(self):
        self.add('personal', historyOnlyCliSessionId=uid(500))
        self.add('work')
        result = self.service.status()
        self.assertTrue(result['ok'])
        self.assertTrue(result['warnings'])
        self.assertIsNone(result['accounts'][0]['sessions'])
        self.assertIn('historyOnlyCliSessionId', result['accounts'][0]['error'])
        self.assertEqual(result['accounts'][1]['sessions'], 1)
        self.assertTrue(self.preview()['can_sync'])

    def test_unknown_account_status_visible_but_preview_stops(self):
        (self.desktop / 'config.json').write_text(json.dumps({'lastKnownAccountUuid': uid(999)}))
        result = self.service.status()
        self.assertIsNone(result['active_alias'])
        self.assertEqual(len(result['accounts']), 3)
        with self.assertRaisesRegex(RuntimeError, 'not registered'):
            self.preview()

    def test_unsupported_version_stops_before_backup(self):
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString': '999'}))
        self.assertFalse(self.service.status()['supported_version'])
        with self.assertRaisesRegex(RuntimeError, 'not been validated'):
            self.service.execute('backup')
        self.assertFalse((self.root / 'backups').exists())

    def test_running_preview_is_read_only_sync_requires_full_exit(self):
        name = self.add()
        original = (self.env.folder('work') / name).read_bytes()
        self.running = True
        preview = self.preview()
        self.assertTrue(preview['claude_running'])
        self.assertTrue(preview['can_sync'])
        self.assertEqual((self.env.folder('work') / name).read_bytes(), original)
        self.assertEqual(list(self.env.folder('research').iterdir()), [])
        with self.assertRaisesRegex(RuntimeError, 'Completely quit'):
            self.sync(preview)
        self.running = False
        self.assertTrue(self.sync(preview)['ok'])

    def test_target_must_be_current_account(self):
        with self.assertRaisesRegex(RuntimeError, '目标必须'):
            self.service.execute('preview', {'source': 'work', 'target': 'personal'})

    def test_live_backup_is_private_and_auth_excluded(self):
        self.add('research')
        (self.env.folder('research') / 'scheduled-tasks.json').write_text('{"secret":"unrelated"}')
        self.running = True
        result = self.service.execute('backup', {})
        self.assertEqual(result['records'], 1)
        self.assertEqual(result['transcripts'], 1)
        self.assertEqual(result['files'], 3)
        path = Path(result['path'])
        all_bytes = b''.join(p.read_bytes() for p in path.rglob('*') if p.is_file())
        self.assertNotIn(b'SYNTHETIC-SECRET-NEVER-COPY', all_bytes)
        self.assertNotIn(b'unrelated', all_bytes)
        for item in self.root.rglob('*'):
            self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o700 if item.is_dir() else 0o600)
            self.assertFalse(item.is_symlink())
            if item.is_file():
                self.assertEqual(item.stat().st_nlink, 1)
        self.assertEqual(self.service.status()['accounts'][2]['latest_backup'], str(path))

    def test_backup_live_change_not_marked_complete(self):
        name = self.add('research')
        original = engine.backup_files
        def changing(*args):
            result = original(*args)
            record = self.record('research', name)
            record['title'] = 'changed while copying'
            self.write('research', name, record)
            return result
        with patch.object(engine, 'backup_files', side_effect=changing):
            with self.assertRaisesRegex(RuntimeError, '备份期间'):
                self.service.execute('backup')
        self.assertIsNone(self.service.latest_backup('research'))

    def test_empty_checkpoint_is_valid(self):
        result = self.service.execute('backup')
        self.assertEqual((result['records'], result['files'], result['bytes']), (0, 0, 0))
        self.assertTrue((Path(result['path']) / 'checkpoint.json').exists())

    def test_backup_hint_change_and_restore_never_marked_complete(self):
        name = self.add('research')
        hint = self.env.folder('research') / 'archived-sessions.idx'
        original_hint = json.dumps({'v': 1, 'archived': []})
        hint.write_text(original_hint)
        original = engine.backup_files
        def change_and_restore(*args):
            hint.write_text(json.dumps({'v': 1, 'archived': [name[:-5]]}))
            try:
                return original(*args)
            finally:
                hint.write_text(original_hint)
        with patch.object(engine, 'backup_files', side_effect=change_and_restore):
            with self.assertRaisesRegex(RuntimeError, '初始检查点不一致'):
                self.service.execute('backup')
        self.assertIsNone(self.service.latest_backup('research'))

    def test_missing_import_is_append_only_manual_and_not_resumed(self):
        name = self.add()
        source_before = (self.env.folder('work') / name).read_bytes()
        transcript = self.put_transcript(100)
        transcript_before = transcript.read_bytes()
        preview = self.preview()
        self.assertEqual(preview['missing_count'], 1)
        result = self.sync(preview)
        self.assertTrue(result['ok'])
        self.assertEqual(result['added'], 1)
        record = self.record('research', name)
        self.assertEqual(record['permissionMode'], 'default')
        self.assertFalse(record['resumeConfirmed'])
        self.assertEqual(record['importedFrom'], 'local-1p-code')
        self.assertEqual(record['sessionPermissionUpdates'], [])
        self.assertEqual(record['alwaysAllowedReasons'], [])
        for field in ('spawnSeed', 'isRunning', 'stagedTranscriptPath', 'interruptedByQuitAt'):
            self.assertNotIn(field, record)
        self.assertEqual((self.env.folder('work') / name).read_bytes(), source_before)
        self.assertEqual(transcript.read_bytes(), transcript_before)
        self.assertEqual((self.env.folder('research') / name).stat().st_nlink, 1)
        self.assertTrue(Path(result['backup_path']).is_dir())
        self.assertTrue(Path(result['source_backup_path']).is_dir())
        self.assertFalse(self.preview()['can_sync'])
        with self.assertRaisesRegex(RuntimeError, '已用于'):
            self.sync(preview)

    def test_mixed_missing_and_continuation_preserves_existing(self):
        self.add('work', 100)
        self.add('work', 200, transcript=201, priorCliSessionIds=[uid(200)],
                 forkedFromSessionId='local_' + uid(99), forkedAtMessageUuid=uid(98))
        self.add('research', 200)
        name = 'local_' + uid(200) + '.json'
        target_before = (self.env.folder('research') / name).read_bytes()
        preview = self.preview()
        self.assertEqual((preview['missing_count'], preview['continuation_count']), (1, 1))
        result = self.sync(preview)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['added'], 2)
        self.assertEqual((self.env.folder('research') / name).read_bytes(), target_before)
        copy = engine.copy_name(self.env, 'work', 'research', self.record('work', name))
        record = self.record('research', copy)
        self.assertTrue(record['title'].endswith('（work 后续）'))
        self.assertNotIn('forkedFromSessionId', record)
        self.assertEqual(self.preview()['continuation_count'], 0)

    def test_equivalent_ids_are_skipped_in_nonempty_target(self):
        self.add('work', 100)
        self.add('research', 200, transcript=100)
        result = self.preview()
        self.assertEqual(result['equivalent_count'], 1)
        self.assertEqual(result['missing_count'], 0)

    def test_empty_target_preserves_source_existing_equivalent_entries(self):
        self.add('work', 100)
        self.add('work', 200, transcript=100)
        preview = self.preview()
        self.assertEqual(preview['missing_count'], 2)
        self.assertEqual(self.sync(preview)['added'], 2)

    def test_same_title_different_history_not_equivalent(self):
        self.add('work', 100)
        self.add('research', 200)
        self.assertEqual(self.preview()['missing_count'], 1)

    def test_same_transcript_different_cuts_not_equivalent(self):
        self.add('work', 100, transcriptCuts={uid(100): 4})
        self.add('research', 200, transcript=100, transcriptCuts={uid(100): 7})
        self.assertEqual(self.preview()['missing_count'], 1)

    def test_client_hydrated_reply_owner_does_not_create_false_continuation(self):
        for owner in (None, 'omitted'):
            with self.subTest(source_owner=owner):
                name = self.add('work', 100, lastAssistantUuid=uid(800))
                source = self.record('work', name)
                if owner is None:
                    source['lastAssistantCliSessionId'] = None
                    self.write('work', name, source)
                hydrated = engine.sanitized(source, 0)
                hydrated['lastAssistantCliSessionId'] = uid(100)
                self.write('research', name, hydrated)
                before = (self.env.folder('work') / name).read_bytes()
                preview = self.preview()
                self.assertEqual(preview['unchanged_count'], 1)
                self.assertEqual(preview['continuation_count'], 0)
                self.assertFalse(preview['can_sync'])
                self.assertEqual((self.env.folder('work') / name).read_bytes(), before)

    def test_reply_owner_normalization_matches_loader_string_semantics(self):
        record = {'sessionId': 'local_' + uid(100), 'cliSessionId': uid(100), 'lastAssistantUuid': ''}
        derived = service.history_signature(record)
        self.assertEqual(derived, service.history_signature({**record, 'lastAssistantCliSessionId': uid(100)}))
        self.assertNotEqual(derived, service.history_signature({**record, 'lastAssistantCliSessionId': ''}))
        self.assertNotEqual(derived, service.history_signature({**record, 'lastAssistantCliSessionId': uid(200)}))
        for absent in (None, 100, False):
            with self.subTest(lastAssistantUuid=absent):
                no_reply = {**record, 'lastAssistantUuid': absent}
                self.assertNotEqual(service.history_signature(no_reply), service.history_signature({
                    **no_reply, 'lastAssistantCliSessionId': uid(100)}))

    def test_legacy_ssh_unknown_owner_remains_distinct_from_current(self):
        record = {'sessionId': 'local_' + uid(100), 'cliSessionId': uid(100),
                  'lastAssistantUuid': uid(800), 'sshReattach': {'namedCliSessionId': uid(100)}}
        unknown = service.history_signature(record)
        self.assertEqual(unknown, service.history_signature({**record, 'lastAssistantCliSessionId': ''}))
        self.assertNotEqual(unknown, service.history_signature({**record, 'lastAssistantCliSessionId': uid(100)}))
        record['sshReattach']['namedCliSessionId'] = uid(200)
        self.assertEqual(service.history_signature(record), service.history_signature({
            **record, 'lastAssistantCliSessionId': uid(100)}))

    def test_copy_id_collision_blocks_entire_direction(self):
        name = self.add('work', 100, transcript=101)
        self.add('research', 100)
        self.add('work', 200)
        record = self.record('work', name)
        copy = engine.copy_name(self.env, 'work', 'research', record)
        candidate = engine.copied_record(record, copy, 'work', 0)
        candidate['transcriptCuts'] = {uid(101): 4}
        self.write('research', copy, candidate)
        preview = self.preview()
        self.assertEqual(preview['conflict_count'], 1)
        self.assertFalse(preview['can_sync'])
        with self.assertRaisesRegex(RuntimeError, '没有可安全'):
            self.sync(preview)

    def test_archived_deleted_and_unchanged_classifications(self):
        a = self.add('work', 100, isArchived=True)
        b = self.add('work', 200)
        c = self.add('work', 300)
        self.add('research', 300, title='different title')
        (self.env.folder('research') / ('deleted_' + b)).write_text('{}')
        preview = self.preview()
        self.assertEqual(preview['skipped_count'], 2)
        self.assertEqual(preview['unchanged_count'], 1)
        self.assertEqual({i['kind'] for i in preview['items']}, {'archived', 'deleted', 'unchanged'})

    def test_source_index_change_invalidates_preview(self):
        name = self.add()
        preview = self.preview()
        record = self.record('work', name)
        record['title'] = 'changed'
        self.write('work', name, record)
        with self.assertRaisesRegex(RuntimeError, '自预览后已变化'):
            self.sync(preview)
        self.assertFalse((self.env.folder('research') / name).exists())

    def test_source_transcript_change_invalidates_preview(self):
        self.add()
        preview = self.preview()
        self.put_transcript(100).write_text('{"type":"user","message":"changed"}\n')
        with self.assertRaisesRegex(RuntimeError, '自预览后已变化'):
            self.sync(preview)

    def test_target_transcript_change_invalidates_preview(self):
        self.add('work', 100)
        self.add('research', 200)
        preview = self.preview()
        self.put_transcript(200).write_text('{"type":"user","message":"changed"}\n')
        with self.assertRaisesRegex(RuntimeError, '自预览后已变化'):
            self.sync(preview)

    def test_target_new_deletion_invalidates_preview(self):
        name = self.add()
        preview = self.preview()
        (self.env.folder('research') / ('deleted_' + name)).write_text('{}')
        with self.assertRaisesRegex(RuntimeError, '自预览后已变化'):
            self.sync(preview)

    def test_account_change_invalidates_preview(self):
        self.add()
        preview = self.preview()
        self.set_active('personal')
        with self.assertRaisesRegex(RuntimeError, '目标必须'):
            self.sync(preview)

    def test_preview_tampering_and_expiry_rejected(self):
        self.add()
        preview = self.preview()
        with patch.object(service.time, 'time', return_value=service.time.time() + 3600):
            with self.assertRaisesRegex(RuntimeError, '过期'):
                self.sync(preview)
        path = self.root / 'previews' / preview['preview_id'] / 'preview.json'
        path.write_text(path.read_text() + ' ')
        with self.assertRaisesRegex(RuntimeError, '预览文件发生变化'):
            self.sync(preview)

    def test_preview_traversal_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, '先生成'):
            self.service.execute('sync', {**self.request, 'preview_id': '../../anything'})

    def test_operation_lock_rejects_reentry(self):
        with self.service.operation():
            with self.assertRaisesRegex(RuntimeError, '另一项历史操作'):
                self.service.execute('backup')

    def test_symlink_project_companion_is_rejected(self):
        name = self.add()
        (self.projects / 'project' / uid(100) / 'linked').symlink_to(self.work, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            self.preview()

    def test_missing_transcript_stops_without_history_mutation(self):
        name = self.add()
        self.put_transcript(100).unlink()
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            self.preview()
        self.assertFalse((self.env.folder('research') / name).exists())

    def test_invalid_jsonl_backup_not_complete(self):
        self.add('research')
        self.put_transcript(100).write_text('{"partial":')
        with self.assertRaisesRegex(RuntimeError, 'Truncated'):
            self.service.execute('backup')
        self.assertIsNone(self.service.latest_backup('research'))

    def test_cli_one_json_on_invalid_input_and_no_secret_output(self):
        self.add()
        stream = io.StringIO()
        with patch('sys.stdin', io.StringIO('{')), redirect_stdout(stream):
            code = service.main(['preview'], self.service)
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stream.getvalue())['ok'])
        stream = io.StringIO()
        with redirect_stdout(stream):
            service.main(['status'], self.service)
        self.assertNotIn('SYNTHETIC-SECRET', stream.getvalue())
        self.assertNotIn('synthetic private body', stream.getvalue())
        self.assertEqual(len(stream.getvalue().splitlines()), 1)

    def test_left_at_preserved_and_prior_reference_backed_up(self):
        self.put_transcript(99)
        timestamps = {uid(99): 1794386400000}
        name = self.add(priorCliSessionIds=[uid(99)], priorCliSessionLeftAt=timestamps)
        preview = self.preview()
        result = self.sync(preview)
        self.assertTrue(result['ok'])
        self.assertEqual(self.record('research', name)['priorCliSessionLeftAt'], timestamps)
        backup = engine.read_json(Path(result['source_backup_path']) / 'checkpoint.json')
        self.assertEqual(backup['summary']['transcripts'], 2)

    def test_left_at_change_requires_independent_continuation(self):
        self.put_transcript(99)
        self.add('work', 100, priorCliSessionIds=[uid(99)], priorCliSessionLeftAt={uid(99): 200})
        self.add('research', 100, priorCliSessionIds=[uid(99)], priorCliSessionLeftAt={uid(99): 100})
        self.assertEqual(self.preview()['continuation_count'], 1)

    def test_left_at_invalid_shapes_keys_and_timestamps_rejected(self):
        invalid = [[], '', 'bad', True, 5, {'bad': 3}, {uid(88): 3},
                   {uid(99): True}, {uid(99): 0}, {uid(99): -1},
                   {uid(99): math.nan}, {uid(99): math.inf}, {uid(99): '5'}, {uid(99): 10**400}]
        for value in invalid:
            with self.subTest(value=value):
                record = {'cliSessionId': uid(100), 'priorCliSessionIds': [uid(99)],
                          'priorCliSessionLeftAt': value}
                with self.assertRaisesRegex(RuntimeError, 'priorCliSessionLeftAt'):
                    engine.transcript_ids(record)

    def test_five_unreviewed_fields_remain_blocked(self):
        self.assertEqual(len(engine.UNSUPPORTED_HISTORY_FIELDS), 5)
        for field in engine.UNSUPPORTED_HISTORY_FIELDS:
            with self.subTest(field=field):
                with self.assertRaisesRegex(RuntimeError, 'Unsupported history field'):
                    engine.transcript_ids({'cliSessionId': uid(100), field: [uid(99)]})

    def test_engine_rejects_left_at_tampered_prepared_index_even_with_hash_update(self):
        self.put_transcript(99)
        self.add(priorCliSessionIds=[uid(99)], priorCliSessionLeftAt={uid(99): 100})
        out = self.base / 'engine-plan'
        engine.plan_history(self.env, 'work', 'research', out)
        plan = engine.read_json(out / 'plan.json')
        item = plan['entries'][0]
        index = out / 'indexes' / item['name']
        changed = engine.read_json(index)
        changed['priorCliSessionLeftAt'][uid(99)] = 200
        engine.private_json(index, changed, replace=True)
        item['sha256'] = engine.digest(index)
        engine.private_json(out / 'plan.json', plan, replace=True)
        (out / 'plan.sha256').write_text(engine.digest(out / 'plan.json') + '\n')
        with self.assertRaisesRegex(RuntimeError, 'allowed schema'):
            engine.apply_history(self.env, out)


class SavedBackupTests(unittest.TestCase):
    # Reuse only synthetic fixture helpers, without rerunning legacy cases.
    setUp = HistoryServiceTests.setUp
    tearDown = HistoryServiceTests.tearDown
    set_active = HistoryServiceTests.set_active
    put_transcript = HistoryServiceTests.put_transcript
    add = HistoryServiceTests.add
    write = HistoryServiceTests.write
    record = HistoryServiceTests.record
    def save_source(self, alias='work'):
        self.set_active(alias)
        result = self.service.execute('backup')
        self.set_active('research')
        self.saved_request = {'backup_id': result['backup_id'], 'target': 'research'}
        return result

    def saved_preview(self):
        return self.service.execute('preview', self.saved_request)

    def saved_sync(self, preview):
        return self.service.execute('sync', {**self.saved_request, 'preview_id': preview['preview_id']})

    def test_saved_import_does_not_depend_on_live_source(self):
        name = self.add()
        backup = self.save_source()
        shutil.rmtree(self.env.folder('work'))
        status = self.service.status()
        self.assertTrue(status['ok'])
        self.assertIn(backup['backup_id'], [b['backup_id'] for b in status['backups']])
        preview = self.saved_preview()
        result = self.saved_sync(preview)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['added'], 1)
        imported = self.record('research', name)
        self.assertFalse(imported['resumeConfirmed'])
        self.assertEqual(imported['permissionMode'], 'default')
        self.assertNotIn('spawnSeed', imported)

    def test_saved_index_keeps_backup_content_after_live_source_changes(self):
        name = self.add(title='Saved title')
        self.save_source()
        source = self.record('work', name)
        source['title'] = 'Later live title'
        self.write('work', name, source)
        result = self.saved_sync(self.saved_preview())
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.record('research', name)['title'], 'Saved title')

    def test_saved_restore_missing_body_and_companion(self):
        name = self.add()
        self.save_source()
        shutil.rmtree(self.projects / 'project')
        preview = self.saved_preview()
        self.assertEqual(preview['restore_files_count'], 2)
        result = self.saved_sync(preview)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['restored_files'], 2)
        self.assertEqual((self.projects / 'project' / (uid(100) + '.jsonl')).stat().st_nlink, 1)
        self.assertEqual(stat.S_IMODE((self.projects / 'project' / (uid(100) + '.jsonl')).stat().st_mode), 0o600)
        self.assertEqual(self.record('research', name)['cliSessionId'], uid(100))

    def test_same_account_missing_index_can_restore_from_saved_backup(self):
        name = self.add('research')
        self.save_source('research')
        (self.env.folder('research') / name).unlink()
        result = self.saved_sync(self.saved_preview())
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['added'], 1)

    def test_same_account_files_only_restore_with_missing_body_preimage(self):
        name = self.add('research')
        self.save_source('research')
        body = self.projects / 'project' / (uid(100) + '.jsonl')
        body.unlink()
        preview = self.saved_preview()
        self.assertEqual(preview['missing_count'], 0)
        self.assertEqual(preview['continuation_count'], 0)
        self.assertEqual(preview['restore_files_count'], 1)
        self.assertTrue(preview['can_sync'])
        before = (self.env.folder('research') / name).read_bytes()
        result = self.saved_sync(preview)
        self.assertTrue(result['ok'], result)
        self.assertEqual((result['added'], result['restored_files']), (0, 1))
        self.assertEqual((self.env.folder('research') / name).read_bytes(), before)
        manifest = engine.read_json(Path(result['backup_path']) / 'checkpoint.json')
        self.assertFalse(manifest['importable'])
        self.assertEqual(manifest['state']['missing_transcripts'], [uid(100)])
        self.assertNotIn(result['backup_path'], [b['path'] for b in self.service.status()['backups']])

    def test_newer_valid_jsonl_prefix_is_preserved(self):
        self.add()
        self.save_source()
        body = self.projects / 'project' / (uid(100) + '.jsonl')
        with body.open('a') as stream:
            stream.write('{"type":"assistant","message":"newer retained"}\n')
        before = body.read_bytes()
        preview = self.saved_preview()
        self.assertEqual(preview['retained_newer_count'], 1)
        result = self.saved_sync(preview)
        self.assertTrue(result['ok'], result)
        self.assertEqual(body.read_bytes(), before)

    def test_diverged_body_blocks_all_writes(self):
        self.add()
        self.save_source()
        body = self.projects / 'project' / (uid(100) + '.jsonl')
        body.write_text('{"type":"user","message":"diverged"}\n')
        before = body.read_bytes()
        preview = self.saved_preview()
        self.assertFalse(preview['can_sync'])
        self.assertEqual(preview['conflict_count'], 1)
        with self.assertRaisesRegex(RuntimeError, '冲突'):
            self.saved_sync(preview)
        self.assertEqual(body.read_bytes(), before)
        self.assertEqual(list(self.env.folder('research').iterdir()), [])

    def test_unreferenced_target_body_change_invalidates_saved_preview(self):
        self.add()
        self.save_source()
        preview = self.saved_preview()
        body = self.projects / 'project' / (uid(100) + '.jsonl')
        with body.open('a') as stream:
            stream.write('{"type":"assistant","message":"after preview"}\n')
        with self.assertRaisesRegex(RuntimeError, '恢复目标文件发生变化'):
            self.saved_sync(preview)
        self.assertEqual(list(self.env.folder('research').iterdir()), [])

    def test_backup_tamper_and_unlisted_injection_rejected(self):
        self.add()
        backup = self.save_source()
        root = Path(backup['path'])
        added = root / 'snapshot/projects/project/extra.txt'
        added.write_text('injection')
        added.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, '实际文件清单'):
            self.saved_preview()
        added.unlink()
        saved = root / 'snapshot/projects/project' / (uid(100) + '.jsonl')
        saved.write_text('{"type":"user","message":"tampered"}\n')
        with self.assertRaisesRegex(RuntimeError, '校验失败'):
            self.saved_preview()

    def test_unknown_backup_version_and_path_escape_rejected(self):
        self.add()
        backup = self.save_source()
        manifest_path = Path(backup['path']) / 'checkpoint.json'
        original = engine.read_json(manifest_path)
        changed = json.loads(json.dumps(original))
        changed['identity']['version'] = 'unknown'
        engine.private_json(manifest_path, changed, replace=True)
        with self.assertRaisesRegex(RuntimeError, '来源版本'):
            self.saved_preview()
        changed = json.loads(json.dumps(original))
        changed['snapshot_files'][0]['path'] = '../escape'
        engine.private_json(manifest_path, changed, replace=True)
        with self.assertRaisesRegex(RuntimeError, '无效或重复路径'):
            self.saved_preview()

    def test_companion_directory_file_collision_is_conflict(self):
        self.add()
        self.save_source()
        companion = self.projects / 'project' / uid(100)
        shutil.rmtree(companion)
        companion.write_text('directory replaced with file')
        # An ancestor file is a conflict, never permission to overwrite it.
        preview = self.saved_preview()
        self.assertFalse(preview['can_sync'])
        self.assertGreater(preview['conflict_count'], 0)

    def test_saved_login_change_and_running_app_block_import(self):
        self.add()
        self.save_source()
        preview = self.saved_preview()
        self.running = True
        with self.assertRaisesRegex(RuntimeError, 'Completely quit'):
            self.saved_sync(preview)
        self.running = False
        self.set_active('work')
        with self.assertRaisesRegex(RuntimeError, '当前账号'):
            self.saved_sync(preview)

    def test_saved_same_id_changed_history_uses_backup_copy_label(self):
        self.add('work', 100, transcriptCuts={uid(100): 2})
        self.save_source()
        name = self.add('research', 100, transcriptCuts={uid(100): 4})
        before = (self.env.folder('research') / name).read_bytes()
        preview = self.saved_preview()
        self.assertEqual(preview['continuation_count'], 1)
        result = self.saved_sync(preview)
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.env.folder('research') / name).read_bytes(), before)
        copied = self.record('research', result['added_indexes'][0])
        self.assertTrue(copied['title'].endswith('（work 备份）'))

    def test_empty_saved_backup_is_readable_and_no_changes(self):
        self.save_source()
        preview = self.saved_preview()
        self.assertFalse(preview['can_sync'])
        self.assertEqual(preview['restore_files_count'], 0)

    def test_cli_requires_saved_backup_and_rejects_legacy_preview(self):
        stream = io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps(self.request))), redirect_stdout(stream):
            code = service.main(['preview'], self.service)
        self.assertEqual(code, 1)
        self.assertIn('保存的备份', json.loads(stream.getvalue())['detail'])

    def test_saved_import_partial_receipt_records_restored_file(self):
        self.add()
        self.save_source()
        shutil.rmtree(self.projects / 'project')
        preview = self.saved_preview()
        original = self.service.append_file
        calls = []
        def stop_after_first(source, target, digest, **kwargs):
            if calls:
                raise RuntimeError('synthetic interruption')
            original(source, target, digest, **kwargs)
            calls.append(target)
        with patch.object(self.service, 'append_file', side_effect=stop_after_first):
            result = self.saved_sync(preview)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['restored_files'], 1)
        receipt = engine.read_json(Path(result['transaction_path']) / 'summary.json')
        self.assertEqual(len(receipt['restored_paths']), 1)
        self.assertIn('pending_write', receipt)

    def test_saved_prepared_tamper_rejected_even_with_updated_checksum(self):
        self.add()
        self.save_source()
        for field, value in [('permissionMode', 'bypassPermissions'), ('resumeConfirmed', True), ('cliSessionId', uid(999))]:
            with self.subTest(field=field):
                preview = self.saved_preview()
                folder = self.root / 'previews' / preview['preview_id']
                plan = engine.read_json(folder / 'preview.json')
                next(iter(plan['prepared'].values()))[field] = value
                engine.private_json(folder / 'preview.json', plan, replace=True)
                engine.private_json(folder / 'checksum.json', {'sha256': engine.digest(folder / 'preview.json')}, replace=True)
                with self.assertRaisesRegex(RuntimeError, '预览内容与备份'):
                    self.saved_sync(preview)
                self.assertEqual(list(self.env.folder('research').iterdir()), [])

    def test_saved_target_index_change_invalidates_preview(self):
        self.add()
        self.save_source()
        preview = self.saved_preview()
        self.add('research', 200)
        before = (self.env.folder('research') / ('local_' + uid(200) + '.json')).read_bytes()
        with self.assertRaisesRegex(RuntimeError, '自预览后发生变化'):
            self.saved_sync(preview)
        self.assertEqual(len(list(self.env.folder('research').iterdir())), 1)
        self.assertEqual((self.env.folder('research') / ('local_' + uid(200) + '.json')).read_bytes(), before)

    def test_restore_file_created_after_preview_is_never_overwritten(self):
        self.add()
        self.save_source()
        body = self.projects / 'project' / (uid(100) + '.jsonl')
        body.unlink()
        preview = self.saved_preview()
        body.write_text('{"type":"user","message":"external writer"}\n')
        before = body.read_bytes()
        with self.assertRaisesRegex(RuntimeError, '恢复目标文件发生变化'):
            self.saved_sync(preview)
        self.assertEqual(body.read_bytes(), before)

    def test_starting_claude_during_staging_stops_before_publish(self):
        self.add()
        self.save_source()
        body = self.projects / 'project' / (uid(100) + '.jsonl')
        body.unlink()
        preview = self.saved_preview()
        original = self.service.append_file
        def start_before_publish(source, target, digest, before_publish):
            def guard():
                self.running = True
                before_publish()
            return original(source, target, digest, before_publish=guard)
        with patch.object(self.service, 'append_file', side_effect=start_before_publish):
            result = self.saved_sync(preview)
        self.assertFalse(result['ok'])
        self.assertFalse(body.exists())
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['restored_files'], 0)

    def test_previous_menu_app_checkpoint_is_compatible(self):
        self.add()
        backup = self.save_source()
        path = Path(backup['path']) / 'checkpoint.json'
        manifest = engine.read_json(path)
        manifest.pop('source_account')
        manifest.pop('importable')
        manifest['summary'].pop('backup_id')
        engine.private_json(path, manifest, replace=True)
        self.assertTrue(self.saved_preview()['can_sync'])


class BackupRetentionTests(unittest.TestCase):
    setUp = HistoryServiceTests.setUp
    tearDown = HistoryServiceTests.tearDown
    set_active = HistoryServiceTests.set_active
    put_transcript = HistoryServiceTests.put_transcript
    add = HistoryServiceTests.add
    write = HistoryServiceTests.write
    record = HistoryServiceTests.record

    def test_third_account_backup_removes_global_oldest_but_preserves_live_history(self):
        backups = []
        for number, alias in enumerate(('personal', 'work', 'research'), 100):
            self.add(alias, number=number)
            self.set_active(alias)
            backups.append(self.service.execute('backup'))
        first, second, third = backups
        self.assertEqual(third['retention']['removed'], [first['backup_id']])
        self.assertEqual(third['retention']['warnings'], [])
        self.assertFalse(Path(first['path']).exists())
        for item in (second, third):
            self.assertTrue(Path(item['path']).is_dir())
        self.assertEqual(len(self.service.list_backups()), 2)
        for number, alias in enumerate(('personal', 'work', 'research'), 100):
            self.assertTrue((self.env.folder(alias) / ('local_' + uid(number) + '.json')).exists())
            self.assertTrue((self.projects / 'project' / (uid(number) + '.jsonl')).exists())

    def test_global_rotation_keeps_two_newest_even_when_both_belong_to_same_account(self):
        self.add('work')
        self.add('research', number=200)
        first = self.service.checkpoint('work', self.service.identity())
        second = self.service.execute('backup')
        third = self.service.execute('backup')
        self.assertEqual(third['retention']['removed'], [first['backup_id']])
        self.assertEqual({item['backup_id'] for item in self.service.list_backups()},
                         {second['backup_id'], third['backup_id']})

    def test_failed_backup_keeps_both_previous_backups(self):
        self.add('research')
        previous = [self.service.execute('backup') for _ in range(2)]
        with patch.object(engine, 'backup_files', side_effect=OSError('synthetic copy failure')):
            with self.assertRaises(OSError):
                self.service.execute('backup')
        self.assertTrue(all(Path(item['path']).is_dir() for item in previous))

    def test_failed_import_does_not_rotate_checkpoints(self):
        self.add('work')
        previous = [self.service.execute('backup') for _ in range(2)]
        preview = self.service.execute('preview', self.request)
        with patch.object(engine, 'apply_history', side_effect=RuntimeError('synthetic import failure')):
            result = self.service.execute('sync', {**self.request, 'preview_id': preview['preview_id']})
        self.assertFalse(result['ok'])
        self.assertTrue(all(Path(item['path']).is_dir() for item in previous))
        self.assertEqual(len(list((self.root / 'backups/research').iterdir())), 3)

    def test_same_account_import_keeps_source_until_success_then_rotates(self):
        name = self.add('research')
        first = self.service.execute('backup')
        second = self.service.execute('backup')
        (self.env.folder('research') / name).unlink()
        request = {'backup_id': first['backup_id'], 'target': 'research'}
        preview = self.service.execute('preview', request)
        original = self.service.append_file
        def assert_source_still_exists(*args, **kwargs):
            self.assertTrue(Path(first['path']).is_dir())
            self.assertEqual(len(list((self.root / 'backups/research').iterdir())), 3)
            return original(*args, **kwargs)
        with patch.object(self.service, 'append_file', side_effect=assert_source_still_exists):
            result = self.service.execute('sync', {**request, 'preview_id': preview['preview_id']})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['retention']['removed'], [first['backup_id']])
        self.assertFalse(Path(first['path']).exists())
        self.assertTrue(Path(second['path']).is_dir())
        self.assertTrue((self.env.folder('research') / name).is_file())

    def test_explicit_prune_ignores_unfinished_unknown_and_symlink_folders(self):
        self.add('research')
        previous = [self.service.checkpoint(alias, self.service.identity())
                    for alias in ('personal', 'work', 'research')]
        folder = self.root / 'backups/research'
        unfinished = folder / '20000101T000000.000000Z-aaaaaaaa'
        unfinished.mkdir(mode=0o700)
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'keep.txt').write_text('keep')
        link = folder / '20000101T000000.000001Z-bbbbbbbb'
        link.symlink_to(outside, target_is_directory=True)
        unknown = self.root / 'backups/unregistered'
        unknown.mkdir()
        result = self.service.execute('prune_backups')
        self.assertTrue(result['ok'])
        self.assertEqual(result['retention']['removed'], [previous[0]['backup_id']])
        self.assertEqual(len(result['retention']['warnings']), 2)
        self.assertTrue(unfinished.is_dir())
        self.assertTrue(link.is_symlink())
        self.assertTrue((outside / 'keep.txt').is_file())
        self.assertTrue(unknown.is_dir())

    def test_cleanup_failure_does_not_report_successful_backup_as_failed(self):
        self.add('research')
        previous = [self.service.execute('backup') for _ in range(2)]
        with patch.object(service.shutil, 'rmtree', side_effect=OSError('synthetic cleanup failure')):
            result = self.service.execute('backup')
        self.assertTrue(result['ok'])
        self.assertEqual(result['retention']['removed'], [])
        self.assertIn('synthetic cleanup failure', result['retention']['warnings'][0])
        self.assertTrue(all(Path(item['path']).is_dir() for item in previous))



if __name__ == '__main__':
    unittest.main()

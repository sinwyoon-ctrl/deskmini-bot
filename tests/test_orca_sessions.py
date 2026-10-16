import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.context import WorkspacePaths
from app.lookup import ReadOnlyLookup
from app.orca_bridge import NativeOrca, OrcaQueue
from app.orca_sessions import capture_sessions, read_sessions, describe_sessions, session_summary
from app.state import StateStore

ROOT = Path(__file__).resolve().parents[1]


class Native:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def call(self, *args, timeout=90):
        self.calls.append(args)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def result(state='done', verdict='live', **extra):
    return {'worktrees': [{'displayName': 'mobile-created', 'repo': 'ione24',
        'worktreeId': 'repo::/dev/ione24', 'hostId': 'local', 'terminalVerdict': verdict,
        'agents': [{'agentType': 'codex', 'state': state, 'lastAssistantMessage': 'API 40/40 확인, 운영 반영은 보류',
                    'updatedAt': 12345}]}], 'hostScope': {'omittedHostIds': []}, **extra}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root/'state/db.sqlite', ROOT/'schema.sql')
        self.config = {'enabled': True}
        self.cache = self.store.db_path.parent/'orca-sessions.json'

    def test_mobile_session_not_in_supervised_queue_is_visible_without_changing_tasks(self):
        native = Native(result())
        capture_sessions(self.store, self.config, native)
        self.assertEqual(native.calls, [('worktree', 'ps', '--limit', '12')])
        data = read_sessions(self.store)
        self.assertEqual(data['status'], 'ok')
        self.assertEqual(data['items'][0]['repo'], 'ione24')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM orca_requests').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        text = OrcaQueue(self.store, self.config).describe()
        self.assertIn('mobile-created', text)
        self.assertIn('API 40/40', text)
        self.assertIn('다음 지시 대기', text)
        self.assertIn('프로젝트 전체의 완료 여부는 별도 확인', text)
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)

    def test_failure_replaces_previous_live_success_with_explicit_unknown(self):
        capture_sessions(self.store, self.config, Native(result()))
        capture_sessions(self.store, self.config, Native(subprocess.TimeoutExpired('orca', 90)))
        data = read_sessions(self.store)
        self.assertEqual(data['status'], 'error')
        self.assertEqual(data['items'], [])
        self.assertNotIn('API 40/40', describe_sessions(self.store))

    def test_stale_future_and_naive_observations_are_not_current_evidence(self):
        capture_sessions(self.store, self.config, Native(result()))
        original = json.loads(self.cache.read_text())
        for offset in (-121, 60):
            original['observed_at'] = (datetime.now(UTC)+timedelta(seconds=offset)).isoformat()
            self.cache.write_text(json.dumps(original))
            self.assertEqual(read_sessions(self.store)['status'], 'stale')
            self.assertEqual(read_sessions(self.store)['items'], [])
        original['observed_at'] = datetime.now().isoformat()
        self.cache.write_text(json.dumps(original))
        self.assertEqual(read_sessions(self.store)['status'], 'unavailable')

    def test_corrupt_or_missing_cache_does_not_hide_supervised_record_status(self):
        queue = OrcaQueue(self.store, self.config)
        self.assertEqual(queue.describe(), 'Orca 등록 작업이 없습니다.')
        self.cache.write_text('{broken')
        self.assertIn('현재 작업 상태를 확인하지 못했습니다', queue.describe())
        self.assertEqual(read_sessions(self.store)['status'], 'unavailable')

    def test_lookup_is_read_only_and_returns_live_mobile_session_with_source_age(self):
        capture_sessions(self.store, self.config, Native(result()))
        paths = WorkspacePaths(home=self.root, agent_hub=self.root/'hub', work=self.root/'work')
        lookup = ReadOnlyLookup(self.store, paths, 100, (), None)
        before = hashlib.sha256(self.cache.read_bytes()).hexdigest()
        with self.store.connect() as db:
            count = db.execute('SELECT count(*) FROM events').fetchone()[0]
        with patch('subprocess.run', side_effect=AssertionError('lookup must not launch processes')):
            observed = lookup({'source': 'orca', 'query': 'ione24'})
        self.assertEqual(observed['status'], 'ok')
        self.assertEqual(observed['items'][0]['kind'], 'agent_session')
        self.assertIn('observed_at', observed['items'][0])
        self.assertEqual(hashlib.sha256(self.cache.read_bytes()).hexdigest(), before)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], count)

    def test_unknown_or_nonlive_session_is_not_called_finished(self):
        capture_sessions(self.store, self.config, Native(result('brand_new_state', 'unverifiable')))
        self.assertIn('상태 미확인', describe_sessions(self.store))
        self.assertIn('연결 유지 여부 확인 필요', describe_sessions(self.store))

    def test_partial_and_empty_live_result_are_distinct(self):
        native = Native(result(truncated=True))
        capture_sessions(self.store, self.config, native)
        self.assertEqual(read_sessions(self.store)['status'], 'partial')
        self.assertIn('생략', describe_sessions(self.store))
        capture_sessions(self.store, self.config, Native({'worktrees': [], 'hostScope': {'omittedHostIds': []}}))
        self.assertEqual(read_sessions(self.store)['status'], 'ok')
        self.assertIn('관측된 에이전트 세션 없음', describe_sessions(self.store))

    def test_malformed_native_result_does_not_become_empty_success(self):
        for value in ({}, {'worktrees': 'bad'}, {'worktrees': [{'agents': 'bad'}]}):
            capture_sessions(self.store, self.config, Native(value))
            self.assertEqual(read_sessions(self.store)['status'], 'error')

    def test_another_runtime_cannot_publish_live_state(self):
        config = {'enabled': True, 'cli': '/fixed/orca', 'cwd': str(self.root), 'runtime_id': 'expected'}
        response = {'ok': True, '_meta': {'runtimeId': 'other'}, 'result': result()}
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(response))):
            capture_sessions(self.store, config, NativeOrca(config))
        self.assertEqual(read_sessions(self.store)['status'], 'error')
        self.assertEqual(read_sessions(self.store)['items'], [])

    def test_model_snapshot_includes_recent_last_response_without_full_prompt(self):
        capture_sessions(self.store, self.config, Native(result()))
        data = session_summary(self.store)
        self.assertEqual(data['total_sessions'], 1)
        self.assertIn('API 40/40', data['items'][0]['last_response'])
        self.assertNotIn('prompt', data['items'][0])


import test_watch as fixtures


class MobileStatusRoutes(fixtures.BotFixture):
    def test_orca_status_shows_untracked_mobile_session_with_fake_telegram(self):
        capture_sessions(self.bot.store, {'enabled': True}, Native(result()))
        self.bot._handle_update(self.message('/orca status', 7001))
        text = self.tg.sent[-1]['text']
        self.assertIn('mobile-created', text)
        self.assertIn('다음 지시 대기', text)
        self.assertIn('40/40', text)
        self.assertEqual(self.bot.store.active_task_count(), 0)

    def test_natural_who_is_working_reads_same_session_observation(self):
        capture_sessions(self.bot.store, {'enabled': True}, Native(result()))
        self.bot._handle_update(self.message('지금 누가 작업 중이야', 7002))
        self.assertIn('mobile-created', self.tg.sent[-1]['text'])
        self.assertIn('40/40', self.tg.sent[-1]['text'])

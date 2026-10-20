import json
import sqlite3

import unittest

import test_agent_routing as routing
from app.context import ROUTING_LIMIT, WorkspacePaths, build_snapshot, routing_summary


class ContextRoutingTests(unittest.TestCase):
    def setUp(self):
        f = routing.RoutingTests()
        f.setUp()
        self.f = f
        self.addCleanup(f.tearDown)
        self.addCleanup(f.doCleanups)

    def snapshot(self):
        paths = WorkspacePaths(home=self.f.home, agent_hub=self.f.home / 'hub', work=self.f.home / 'work')
        return json.loads(build_snapshot(self.f.bot.store, paths))

    def sql(self, query, *args):
        with self.f.bot.store.connect() as db:
            db.execute(query, args[0] if len(args) == 1 and isinstance(args[0], tuple) else args)

    def test_empty_state(self):
        route = routing_summary(self.f.bot.store)
        self.assertEqual(route['pending_questions'], {'codex': 0, 'claude': 0})
        self.assertEqual(route['open_ask_cards'], [])
        self.assertEqual(route['external_approvals_pending'], [])

    def test_codex_focus_claude_question_card_and_approval_visible(self):
        self.f.say('코덱스야, 텔레그램 봇 상태 알려줘')
        row = self.f.claude()
        self.f.question(row)
        self.sql('INSERT INTO telegram_agent_inputs VALUES(?,?,?,?)',
                 ('src:1', 100, 'fp', json.dumps({'ask': True, 'body': 'x' * 200 + ' 애매한 문장'})))
        self.sql('INSERT INTO telegram_agent_inputs VALUES(?,?,?,?)',
                 ('src:2', 100, 'fp2', json.dumps({'ask': True, 'picked': 'work', 'body': '이미 고른 카드'})))
        self.sql("INSERT INTO external_approvals(id,path,requester,summary,created_at,expires_at,state,seen_at) VALUES(?,?,?,?,?,?,?,?)",
                 ('APR-1', 'a.md', 'main', '배포 승인 요청 sk-ant-api03-' + 'a' * 40, '2026-10-20T10:00:00', '2026-10-21T10:00:00', 'PENDING', 'x'))
        self.sql("INSERT INTO external_approvals(id,path,requester,summary,created_at,expires_at,state,seen_at) VALUES(?,?,?,?,?,?,?,?)",
                 ('APR-2', 'b.md', 'main', '끝난 것', '2026-10-20T10:00:00', '2026-10-21T10:00:00', 'DECIDED', 'x'))
        text = build_snapshot(self.f.bot.store, WorkspacePaths(self.f.home, self.f.home / 'hub', self.f.home / 'work'))
        route = json.loads(text)['telegram_routing']
        self.assertIn(route['selected_agent'], ('codex', 'claude'))
        self.assertIn('claude', route['targets'])
        self.assertEqual(route['targets']['claude']['project'], 'bot-claude')
        self.assertEqual(route['pending_questions']['claude'], 1)
        self.assertEqual(len(route['open_ask_cards']), 1)
        self.assertLessEqual(len(route['open_ask_cards'][0]), 80)
        self.assertEqual([a['id'] for a in route['external_approvals_pending']], ['APR-1'])
        self.assertNotIn('sk-ant-api03-' + 'a' * 40, text)
        self.assertNotIn(str(self.f.bot.settings.allowed_chat_id), json.dumps(route))
        self.assertNotIn('"chat_id"', json.dumps(route))

    def test_length_limit(self):
        for i in range(8):
            self.sql("INSERT INTO external_approvals(id,path,requester,summary,created_at,expires_at,state,seen_at) VALUES(?,?,?,?,?,?,?,?)",
                     (f'A{i}', f'{i}.md', 'm', '가' * 400, f'2026-10-20T10:0{i}:00', '2026-10-21T10:00:00', 'PENDING', 'x'))
        route = routing_summary(self.f.bot.store)
        self.assertLessEqual(len(json.dumps(route, ensure_ascii=False, separators=(',', ':'))), ROUTING_LIMIT)

    def test_read_only(self):
        before = self.f.rows('SELECT COUNT(*) n FROM telegram_agent_inputs')
        routing_summary(self.f.bot.store)
        self.assertEqual(self.f.rows('SELECT COUNT(*) n FROM telegram_agent_inputs'), before)


class NoSchemaTests(unittest.TestCase):
    def test_missing_tables_do_not_raise(self):
        class Store:
            def connect(self_inner):
                return sqlite3.connect(':memory:')
        route = routing_summary(Store())
        self.assertIsNone(route['selected_agent'])
        self.assertEqual(route['external_approvals_pending'], [])

    def test_failure_is_unconfirmed(self):
        class Broken:
            def connect(self_inner):
                raise RuntimeError('x')
        self.assertEqual(routing_summary(Broken()), '미확인')


if __name__ == '__main__':
    unittest.main()

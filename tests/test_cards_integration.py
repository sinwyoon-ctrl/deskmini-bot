"""F-1 cards through the real bot: xa / ar / cx / clp card text, buttons, edit after tap, two-step, direct input."""
import json
import os
import re
from unittest.mock import patch

import test_telegram_90 as t90
from adapters.telegram import TelegramError
from app import external_approvals as xa
from app.claude_permissions import enqueue, process_identity, register
from app.codex_bridge import Bridge
from app.codex_session import CodexSessions
from app.orca_bridge import OrcaQueue
from test_external_approvals import request_text
from test_watch import BotFixture

CARD_ID = 77


class Tap:
    """Callback presses that carry the tapped card (as Telegram does), so the bot can edit it."""

    def tap(self, data, text, update_id, message_id=CARD_ID):
        self.bot._handle_update({"update_id": update_id, "callback_query": {
            "id": "cb%d" % update_id, "data": data, "from": {"id": 100},
            "message": {"message_id": message_id, "text": text, "chat": {"type": "private", "id": 100}}}})

    def edited(self):
        return self.tg.__dict__.get("edited", [])

    def keys(self, markup):
        return [(b["text"], b["callback_data"]) for row in (markup or {}).get("inline_keyboard", []) for b in row]


class ExternalApprovalCardTests(Tap, BotFixture):
    def setUp(self):
        super().setUp()
        self.dir = self.bot.paths.agent_hub / 'approvals'
        self.dir.mkdir(parents=True)

    def put(self, impact=None):
        raw = request_text('PAY-0042', created=self.now, summary='광고 집행 예산 30만 원 결제 승인')
        if impact:
            raw = raw.replace('summary:', 'impact: ' + impact + '\nsummary:', 1)
        (self.dir / 'PAY-0042.md').write_text(raw, encoding='utf-8')
        xa.scan(self.bot)
        return self.tg.sent[-1]

    def test_card_first_line_impact_line_expiry_last_and_buttons(self):
        card = self.put('money')
        lines = card['text'].split('\n')
        self.assertEqual(lines[0], '[승인 요청 · PAY-0042]')
        self.assertEqual(lines[1], '광고 집행 예산 30만 원 결제 승인')
        self.assertEqual(lines[2], '영향: 비용 발생')
        self.assertTrue(lines[-1].startswith('⏳ ') and lines[-1].endswith('까지'))
        self.assertEqual([t for t, _ in self.keys(card['reply_markup'])], ['✅ 승인', '⛔ 거절'])
        self.assertEqual([d for _, d in self.keys(card['reply_markup'])], ['xa:1:a', 'xa:1:r'])

    def test_no_impact_line_by_default(self):
        card = self.put()
        self.assertFalse(any(l.startswith('영향') for l in card['text'].split('\n')))

    def test_tap_edits_original_card_once_and_removes_buttons(self):
        card = self.put()
        sent_before = len(self.tg.sent)
        self.tap('xa:1:a', card['text'], 60)
        self.assertEqual(len(self.edited()), 1)
        edit = self.edited()[0]
        self.assertEqual(edit['message_id'], CARD_ID)
        self.assertRegex(edit['text'].split('\n')[-1], r'^✅ 승인됨 · \d\d/\d\d \d\d:\d\d · 대표 → approvals/PAY-0042\.decision\.md$')
        self.assertNotIn('⏳', edit['text'])
        self.assertEqual(self.keys(edit['reply_markup']), [])
        self.assertEqual(len(self.tg.sent), sent_before)
        self.assertTrue((self.dir / 'PAY-0042.decision.md').exists())

    def test_reject_tap_marks_rejected(self):
        card = self.put()
        self.tap('xa:1:r', card['text'], 61)
        self.assertIn('⛔ 거절됨', self.edited()[0]['text'])

    def test_retap_is_acknowledged_as_already_handled_and_never_rewritten(self):
        card = self.put()
        self.tap('xa:1:a', card['text'], 60)
        before = (self.dir / 'PAY-0042.decision.md').read_text()
        self.tap('xa:1:r', card['text'], 61)
        self.assertEqual((self.dir / 'PAY-0042.decision.md').read_text(), before)
        self.assertIn('이미 처리됨 · approved', self.edited()[-1]['text'])

    def test_edit_failure_after_48_hours_falls_back_to_a_new_notice(self):
        card = self.put()
        error = TelegramError("Bad Request: message can't be edited")
        with patch.object(self.tg, 'edit_message_text', side_effect=error, create=True):
            self.tap('xa:1:a', card['text'], 60)
        self.assertIn('승인했습니다', self.tg.sent[-1]['text'])
        self.assertTrue((self.dir / 'PAY-0042.decision.md').exists())

    def test_transport_without_edit_support_also_falls_back(self):
        card = self.put()
        tg = self.tg
        with patch.object(type(tg), 'edit_message_text', side_effect=AttributeError('no edit'), create=True):
            self.tap('xa:1:a', card['text'], 60)
        self.assertIn('승인했습니다', self.tg.sent[-1]['text'])

    def test_money_approval_is_two_step_and_first_tap_records_nothing(self):
        card = self.put('money')
        self.tap('xa:1:a', card['text'], 60)
        self.assertFalse((self.dir / 'PAY-0042.decision.md').exists())
        confirm = self.edited()[-1]
        self.assertEqual(self.keys(confirm['reply_markup']), [('정말 승인 — 비용 발생', 'xa:1:a2'), ('취소', 'xa:1:c')])
        self.assertIn('⏳', confirm['text'])
        self.tap('xa:1:a2', confirm['text'], 61)
        self.assertIn('decision: approved', (self.dir / 'PAY-0042.decision.md').read_text())
        self.assertIn('✅ 승인됨', self.edited()[-1]['text'])

    def test_irreversible_confirm_label_and_cancel_restores_original_buttons(self):
        card = self.put('irreversible')
        self.tap('xa:1:a', card['text'], 60)
        self.assertEqual(self.keys(self.edited()[-1]['reply_markup'])[0][0], '정말 승인 — 되돌릴 수 없음')
        self.tap('xa:1:c', card['text'], 61)
        self.assertEqual(self.keys(self.edited()[-1]['reply_markup']), [('✅ 승인', 'xa:1:a'), ('⛔ 거절', 'xa:1:r')])
        self.assertFalse((self.dir / 'PAY-0042.decision.md').exists())
        self.tap('xa:1:r', card['text'], 62)
        self.assertIn('decision: rejected', (self.dir / 'PAY-0042.decision.md').read_text())

    def test_reject_is_single_step_even_for_money(self):
        card = self.put('money')
        self.tap('xa:1:r', card['text'], 60)
        self.assertIn('decision: rejected', (self.dir / 'PAY-0042.decision.md').read_text())

    def test_external_impact_has_line_but_stays_single_step(self):
        card = self.put('external')
        self.assertIn('영향: 외부 전송 있음', card['text'])
        self.tap('xa:1:a', card['text'], 60)
        self.assertTrue((self.dir / 'PAY-0042.decision.md').exists())

    def test_legacy_button_without_card_text_still_works(self):
        self.put()
        self.press('xa:1:a')
        self.assertTrue((self.dir / 'PAY-0042.decision.md').exists())
        self.assertIn('승인했습니다', self.tg.sent[-1]['text'])


class AskCardTests(Tap, t90.SemanticFixture):
    @property
    def bot(self):
        return self.f.bot

    @property
    def tg(self):
        return self.f.tg

    def ask(self):
        with self.answer('ambiguous'):
            self.f.say('해제 승인해줘 코덱스')
        return self.f.tg.sent[-1]

    def test_card_has_numbered_list_and_number_buttons(self):
        card = self.ask()
        lines = card['text'].split('\n')
        self.assertEqual(lines[0], '[봇 안내 · 코덱스]')
        self.assertIn('이 문장을 어디로 보낼까요', lines[1])
        self.assertTrue(lines[2].startswith('1) ') and lines[3].startswith('2) ') and lines[4].startswith('3) '))
        self.assertTrue(lines[-1].startswith('⏳'))
        self.assertEqual([t for t, _ in self.keys(card['reply_markup'])], ['1', '2', '3'])
        self.assertEqual([d.rsplit(':', 1)[1] for _, d in self.keys(card['reply_markup'])], ['work', 'chat', 'assistant'])

    def test_tap_marks_number_and_removes_buttons_and_retap_is_already_handled(self):
        card = self.ask()
        data = self.keys(card['reply_markup'])[1][1]
        self.tap(data, card['text'], 700)
        edit = self.edited()[-1]
        self.assertRegex(edit['text'].split('\n')[-1], r'^✅ 2번 선택 · \d\d/\d\d \d\d:\d\d · 대표$')
        self.assertEqual(self.keys(edit['reply_markup']), [])
        count = len(self.edited())
        self.tap(data, card['text'], 701)
        self.assertEqual(len(self.edited()), count + 1)
        self.assertIn('이미 처리됨 · 2번 선택', self.edited()[-1]['text'])

    def test_old_button_data_format_is_still_handled(self):
        card = self.ask()
        row = self.keys(card['reply_markup'])[0][1].split(':')[1]
        self.f.press('ar:%s:chat' % row, update_id=702)
        with self.bot.store.connect() as db:
            plan = json.loads(db.execute('SELECT plan FROM telegram_agent_inputs ORDER BY rowid DESC').fetchone()[0])
        self.assertEqual(plan['picked'], 'chat')


class CodexCardTests(Tap, BotFixture):
    def setUp(self):
        super().setUp()
        config = {'enabled': True, 'projects': {'trial': {'path': str(self.home)}}}
        self.bridge = Bridge(self.bot.store, config, self.home / 'socket')
        self.bridge.runtime('READY')
        patcher = patch('app.codex_session.load_config', return_value=config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = CodexSessions(self.bot.store, config)
        self.key = self.api.create(100, 'trial', 'create-a')
        self.bridge.update(self.key, thread_id='thread-a', epoch=self.bridge.epoch, state='RUNNING', turn_id='turn-a')
        self.number = 9000

    def session(self):
        with self.bot.store.connect() as db:
            return dict(db.execute('SELECT * FROM codex_sessions WHERE id=?', (self.key,)).fetchone())

    def ask(self, method='item/tool/requestUserInput', questions=None, **extra):
        self.number += 1
        params = {'threadId': 'thread-a', 'turnId': 'turn-a', 'itemId': 'item-a', **extra}
        if questions is not None:
            params['questions'] = questions
        self.bridge.question(self.session(), {'method': method, 'id': self.number, 'params': params})
        payload = self.bot.store.pending_outbox()[-1]['payload']
        qid = self.api.pending(self.key)[-1]['id']
        return payload, qid

    def single(self):
        return [{'id': 'name', 'header': '이름', 'question': '시험 파일 이름을 고르세요',
                 'options': [{'label': 'alpha', 'description': '기존 문서와 같음'}, {'label': 'beta'}, {'label': 'gamma'}]}]

    def test_single_question_card_numbers_short_names_and_free_input(self):
        card, qid = self.ask(questions=self.single())
        text = card['text']
        self.assertIn('시험 파일 이름을 고르세요', text.split('\n')[2])
        self.assertIn('1) alpha\n   기존 문서와 같음\n2) beta\n3) gamma', text)
        self.assertIn('⏳ 10분 안에', text)
        self.assertEqual(self.keys(card['reply_markup']), [('1 · alpha', 'cx:%s:1' % qid), ('2 · beta', 'cx:%s:2' % qid),
                                                          ('3 · gamma', 'cx:%s:3' % qid), ('✏️ 직접 입력', 'cx:%s:free' % qid)])

    def test_long_names_use_numbers_only_and_choice_tap_edits_card(self):
        questions = self.single()
        questions[0]['options'] = [{'label': 'a-rather-long-option-name'}, {'label': 'another-long-option-name'}]
        card, qid = self.ask(questions=questions)
        self.assertEqual([t for t, _ in self.keys(card['reply_markup'])], ['1', '2', '✏️ 직접 입력'])
        self.tap('cx:%s:2' % qid, card['text'], 9100)
        edit = self.edited()[-1]
        self.assertTrue(any(re.match(r'^✅ 2번 선택 · ', l) for l in edit['text'].split('\n')), edit['text'])
        self.assertEqual(self.keys(edit['reply_markup']), [])
        self.assertIn('확인 번호: ' + qid, edit['text'])
        with self.bot.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM codex_questions WHERE id=?', (qid,)).fetchone()[0], 'ANSWERING')

    def test_execution_approval_card_effects_and_labels(self):
        card, qid = self.ask('item/commandExecution/requestApproval', command='printf test', cwd=str(self.home),
                             availableDecisions=['accept', 'decline', 'cancel'])
        text = card['text']
        self.assertIn('영향: 셸 명령 실행', text)
        self.assertIn('printf test', text)
        self.assertEqual([t for t, _ in self.keys(card['reply_markup'])], ['✅ 이번만 허용', '⛔ 허용 안 함', '⛔ 거절하고 중단'])
        self.tap('cx:%s:accept' % qid, text, 9101)
        self.assertIn('✅ 이번만 허용됨', self.edited()[-1]['text'])
        self.assertEqual(self.keys(self.edited()[-1]['reply_markup']), [])

    def test_multi_question_card_only_adds_direct_input_button(self):
        card, qid = self.ask(questions=[{'id': 'a', 'question': '하나', 'header': 'a'}, {'id': 'b', 'question': '둘', 'header': 'b'}])
        self.assertEqual(self.keys(card['reply_markup']), [('✏️ 직접 입력', 'cx:%s:free' % qid)])

    def test_direct_input_flow_next_text_becomes_the_answer(self):
        card, qid = self.ask(questions=self.single())
        self.tap('cx:%s:free' % qid, card['text'], 9102)
        self.assertIn('✅ 직접 입력 대기', self.edited()[-1]['text'])
        self.assertIn('다음 답장을 이 질문의 답으로 받습니다(10분)', self.tg.sent[-1]['text'])
        with self.bot.store.connect() as db:
            row = dict(db.execute('SELECT * FROM telegram_direct_inputs').fetchone())
            self.assertEqual((row['chat_id'], row['kind'], row['key'], row['message_id']), (100, 'codex', qid, CARD_ID))
            self.assertEqual(db.execute('SELECT state FROM codex_questions WHERE id=?', (qid,)).fetchone()[0], 'PENDING')
        self.bot._handle_update(self.message('delta 로 해줘', 9103))
        with self.bot.store.connect() as db:
            q = db.execute('SELECT state,response FROM codex_questions WHERE id=?', (qid,)).fetchone()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM telegram_direct_inputs').fetchone()[0], 0)
        self.assertEqual(q['state'], 'ANSWERING')
        self.assertEqual(json.loads(q['response']), {'answers': {'name': {'answers': ['delta 로 해줘']}}})
        self.assertRegex(self.edited()[-1]['text'], r'✅ 직접 입력 · \d\d/\d\d \d\d:\d\d')
        self.assertEqual(self.keys(self.edited()[-1]['reply_markup']), [])

    def test_slash_command_does_not_consume_direct_input(self):
        card, qid = self.ask(questions=self.single())
        self.tap('cx:%s:free' % qid, card['text'], 9102)
        self.bot._handle_update(self.message('/status', 9104))
        with self.bot.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM telegram_direct_inputs').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT state FROM codex_questions WHERE id=?', (qid,)).fetchone()[0], 'PENDING')

    def test_expired_wait_is_not_consumed_and_text_goes_the_normal_way(self):
        card, qid = self.ask(questions=self.single())
        self.tap('cx:%s:free' % qid, card['text'], 9102)
        with self.bot.store.connect() as db:
            db.execute("UPDATE telegram_direct_inputs SET expires_at='2000-01-01T00:00:00+00:00'")
        self.bot._handle_update(self.message('안녕', 9105))
        with self.bot.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM codex_questions WHERE id=?', (qid,)).fetchone()[0], 'PENDING')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM telegram_direct_inputs').fetchone()[0], 0)

    def test_new_wait_replaces_the_old_one_per_chat(self):
        first, q1 = self.ask(questions=self.single())
        second, q2 = self.ask(questions=self.single())
        self.tap('cx:%s:free' % q1, first['text'], 9102, message_id=1)
        self.tap('cx:%s:free' % q2, second['text'], 9103, message_id=2)
        with self.bot.store.connect() as db:
            rows = db.execute('SELECT key,message_id FROM telegram_direct_inputs').fetchall()
        self.assertEqual([(r['key'], r['message_id']) for r in rows], [(q2, 2)])

    def test_free_tap_on_answered_question_is_refused_without_a_wait(self):
        card, qid = self.ask(questions=self.single())
        self.tap('cx:%s:1' % qid, card['text'], 9100)
        self.tap('cx:%s:free' % qid, card['text'], 9101)
        with self.bot.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM telegram_direct_inputs').fetchone()[0], 0)

    def test_free_tap_on_approval_question_is_refused(self):
        card, qid = self.ask('item/commandExecution/requestApproval', command='printf test', cwd=str(self.home))
        self.tap('cx:%s:free' % qid, card['text'], 9101)
        with self.bot.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM telegram_direct_inputs').fetchone()[0], 0)


class ClaudePermissionCardTests(Tap, BotFixture):
    def setUp(self):
        super().setUp()
        workspace = self.home / 'claude-work'
        workspace.mkdir()
        self.workspace = workspace
        self.queue = OrcaQueue(self.bot.store, {'enabled': True, 'projects': {'proof': {'path': str(workspace)}}})
        row = self.queue.submit('proof:1', 'proof', 'claude', 'isolated permission proof', 100)
        self.queue.update(row['id'], state='RUNNING', dispatch_id='dispatch-1', task_id='task-1', run_id='run-1')
        self.row = self.queue.rows()[0]
        self.pid = os.getpid()
        self.started = process_identity(self.pid)
        self.payload = {'hook_event_name': 'PermissionRequest', 'permission_mode': 'default', 'session_id': 'session-1',
                        'cwd': str(workspace), 'tool_name': 'Bash', 'tool_input': {'command': 'echo hello', 'description': '인사 출력'}}

    def request(self):
        with self.bot.store.connect() as db:
            register(db, self.row['id'], self.workspace, self.payload, self.pid, self.started)
            key = enqueue(db, self.row, self.payload, self.pid, self.started, 600)
            outgoing = json.loads(db.execute('select payload_json from outbox where dedupe_key=?', ('clp:' + key,)).fetchone()[0])
        return key, outgoing

    def test_card_has_one_line_impact_with_tool_and_place_and_expiry(self):
        key, card = self.request()
        lines = card['text'].split('\n')
        self.assertEqual(lines[0], '[봇 안내 · 클로드]')
        self.assertIn('Bash', lines[1])
        self.assertTrue(lines[2].startswith('영향: 도구 Bash · 실행 위치 '))
        self.assertEqual(lines[-1], '⏳ 10분 안에')
        self.assertIn('echo hello', card['text'])
        self.assertEqual([t for t, _ in self.keys(card['reply_markup'])], ['✅ 이번 실행 허용', '⛔ 거절'])
        self.assertEqual([d for _, d in self.keys(card['reply_markup'])], ['clp:allow:' + key, 'clp:deny:' + key])

    def test_tap_edits_card_and_removes_buttons(self):
        key, card = self.request()
        self.tap('clp:allow:' + key, card['text'], 9200)
        edit = self.edited()[-1]
        self.assertRegex(edit['text'].split('\n')[-1], r'^✅ 허용됨 · \d\d/\d\d \d\d:\d\d · 대표$')
        self.assertEqual(self.keys(edit['reply_markup']), [])

    def test_retap_says_already_handled_without_new_state(self):
        key, card = self.request()
        self.tap('clp:deny:' + key, card['text'], 9200)
        self.tap('clp:allow:' + key, card['text'], 9201)
        self.assertIn('이미 처리됨', self.edited()[-1]['text'])
        with self.bot.store.connect() as db:
            self.assertEqual(db.execute('select state from claude_permissions where id=?', (key,)).fetchone()[0], 'DENIED')

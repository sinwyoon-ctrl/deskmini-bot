import os
import stat
import subprocess
import sys
import threading
import time
import unittest
from datetime import timedelta

from app import external_approvals as xa
from tests.test_watch import BotFixture, ROOT


def request_text(approval_id='APRIL-READ-01', created=None, body='브라우저 조사 범위 설명', header_id=None, summary='추가 조사 승인'):
    lines = ['---', 'id: '+(header_id or approval_id), 'from: claude@main-pc']
    if created is not None:
        lines.append('created: '+created.isoformat())
    lines += ['expires_hours: 24', 'summary: '+summary, '---', body, '']
    return '\n'.join(lines)


class ExternalApprovalTests(BotFixture):
    def setUp(self):
        super().setUp()
        self.dir = self.bot.paths.agent_hub/'approvals'
        self.dir.mkdir(parents=True)

    def tearDown(self):
        if self.dir.exists():
            os.chmod(self.dir, 0o755)
        super().tearDown()

    def put(self, approval_id='APRIL-READ-01', **kwargs):
        (self.dir/(approval_id+'.md')).write_text(request_text(approval_id, created=kwargs.pop('created', self.now), **kwargs), encoding='utf-8')

    def rows(self):
        with self.bot.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT rowid AS rid,* FROM external_approvals')]

    def decision_file(self, approval_id='APRIL-READ-01'):
        return self.dir/(approval_id+'.decision.md')

    def test_missing_folder_is_inactive(self):
        self.dir.rmdir()
        xa.scan(self.bot)
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.rows(), [])

    def test_request_creates_one_card_with_two_buttons(self):
        self.put()
        xa.scan(self.bot)
        xa.scan(self.bot)
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn('[승인 요청 · APRIL-READ-01]', self.tg.sent[0]['text'])
        self.assertEqual([b[1] for b in self.buttons()], ['xa:1:a', 'xa:1:r'])
        self.assertEqual([r['state'] for r in self.rows()], ['PENDING'])

    def test_approve_tap_writes_decision_once(self):
        self.put()
        xa.scan(self.bot)
        self.press('xa:1:a')
        text = self.decision_file().read_text(encoding='utf-8')
        self.assertIn('decision: approved', text)
        self.assertEqual(self.rows()[0]['state'], 'DECIDED')
        self.assertIn('승인했습니다', self.tg.sent[-1]['text'])
        before = self.decision_file().stat().st_mtime_ns
        self.press('xa:1:r', update_id=51)
        self.assertIn('이미 처리된 승인 요청입니다', self.tg.sent[-1]['text'])
        self.assertEqual(self.decision_file().read_text(encoding='utf-8'), text)
        self.assertEqual(self.decision_file().stat().st_mtime_ns, before)

    def test_reject_tap(self):
        self.put()
        xa.scan(self.bot)
        self.press('xa:1:r')
        self.assertIn('decision: rejected', self.decision_file().read_text(encoding='utf-8'))
        self.assertEqual(self.rows()[0]['decision'], 'rejected')

    def test_expired_request_is_expired_and_not_approvable(self):
        self.put(created=self.now - timedelta(hours=30))
        xa.scan(self.bot)
        self.assertIn('decision: expired', self.decision_file().read_text(encoding='utf-8'))
        self.assertEqual(self.rows()[0]['state'], 'EXPIRED')
        self.assertIn('유효시간 지남', self.tg.sent[-1]['text'])
        self.press('xa:1:a')
        self.assertIn('decision: expired', self.decision_file().read_text(encoding='utf-8'))
        self.assertEqual(self.rows()[0]['state'], 'EXPIRED')

    def test_tap_after_expiry_time_passes(self):
        self.put()
        xa.scan(self.bot)
        self.now += timedelta(hours=25)
        self.press('xa:1:a')
        self.assertIn('유효시간이 지나 승인하지 않았습니다', self.tg.sent[-1]['text'])
        self.assertIn('decision: expired', self.decision_file().read_text(encoding='utf-8'))

    def test_already_decided_elsewhere_gets_no_card(self):
        self.put()
        self.decision_file().write_text('---\nid: APRIL-READ-01\ndecision: approved\n---\n', encoding='utf-8')
        xa.scan(self.bot)
        self.assertEqual(self.tg.sent, [])
        self.assertEqual([(r['state'], r['decision']) for r in self.rows()], [('DECIDED_EXTERNAL', 'approved')])

    def test_bad_files_ignored_with_single_notice(self):
        (self.dir/'bad name.md').write_text(request_text('bad name'), encoding='utf-8')
        self.put('MISMATCH-01', header_id='OTHER-02')
        for _ in range(3):
            xa.scan(self.bot)
        self.assertEqual(len(self.tg.sent), 2)
        self.assertTrue(all('승인 요청 파일 무시' in m['text'] for m in self.tg.sent))
        self.assertEqual(self.rows(), [])

    def test_secrets_are_redacted_on_card(self):
        self.put(body='키 sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123 와 token=supersecretvalue123 사용', summary='api_key=abcdef1234567890xyz')
        xa.scan(self.bot)
        card = self.tg.sent[0]['text']
        for secret in ('sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123', 'supersecretvalue123', 'abcdef1234567890xyz'):
            self.assertNotIn(secret, card)

    def agent_inputs(self):
        with self.bot.store.connect() as db:
            return db.execute('SELECT COUNT(*) FROM telegram_agent_inputs').fetchone()[0]

    def test_typed_approval_reply_reposts_card_only(self):
        self.put()
        xa.scan(self.bot)
        count = len(self.tg.sent)
        message = {'update_id': 70, 'message': {'chat': {'type': 'private', 'id': 100}, 'from': {'id': 100}, 'text': '승인',
                   'reply_to_message': {'text': '알림\n\n[승인 요청 APRIL-READ-01]\n내용'}}}
        self.bot._handle_update(message)
        self.assertEqual(len(self.tg.sent), count + 1)
        self.assertIn('글로 쓴 승인은 실행하지 않습니다', self.tg.sent[-1]['text'])
        self.assertEqual([b[1] for b in self.buttons()], ['xa:1:a', 'xa:1:r'])
        self.assertFalse(self.decision_file().exists())
        self.assertEqual(self.agent_inputs(), 0)
        message['update_id'] = 71
        message['message']['reply_to_message']['text'] = '[승인 요청 NOPE-99]'
        self.bot._handle_update(message)
        self.assertIn('등록된 승인 요청이 아닙니다', self.tg.sent[-1]['text'])
        self.assertEqual(self.agent_inputs(), 0)

    def test_plain_id_and_word_message(self):
        self.bot._handle_update(self.message('APRIL-READ-01 승인', update_id=80))
        self.assertIn('등록된 승인 요청이 아닙니다', self.tg.sent[-1]['text'])
        self.put()
        xa.scan(self.bot)
        self.bot._handle_update(self.message('april-read-01 승인', update_id=81))
        self.assertIn('글로 쓴 승인은 실행하지 않습니다', self.tg.sent[-1]['text'])
        self.assertFalse(self.decision_file().exists())
        self.assertEqual(self.agent_inputs(), 0)
        self.assertFalse(xa.handle_message(self.bot, {'text': '승인'}, 82))

    def test_unregistered_id_ends_in_notice_even_with_selected_model(self):
        with self.bot.store.transaction() as db:
            db.execute("INSERT INTO telegram_agent_focus VALUES(100,'codex',1)")
        self.bot._handle_update(self.message('APRIL-READ-01 승인', update_id=90))
        self.assertIn('등록된 승인 요청이 아닙니다', self.tg.sent[-1]['text'])
        self.assertEqual(self.agent_inputs(), 0)

    def test_write_failure_keeps_pending(self):
        self.put()
        xa.scan(self.bot)
        os.chmod(self.dir, stat.S_IRUSR | stat.S_IXUSR)
        if os.access(self.dir, os.W_OK):
            self.skipTest('directory permissions are not enforced for this user')
        self.press('xa:1:a')
        self.assertIn('결정을 기록하지 못했습니다', self.tg.sent[-1]['text'])
        self.assertEqual(self.rows()[0]['state'], 'PENDING')
        os.chmod(self.dir, 0o755)
        self.press('xa:1:a', update_id=51)
        self.assertEqual(self.rows()[0]['state'], 'DECIDED')

    def run_cli(self, *args):
        return xa.main(list(args))

    def test_cli_request_scan_tap_wait(self):
        hub = str(self.bot.paths.agent_hub)
        self.assertEqual(self.run_cli('request', '--hub', hub, '--id', 'CLI-01', '--from', 'tester', '--summary', 'cli 요청'), 0)
        self.assertEqual(self.run_cli('request', '--hub', hub, '--id', 'CLI-01', '--from', 'tester', '--summary', 'again'), 1)
        self.bot.clock = lambda: __import__('datetime').datetime.now(__import__('datetime').UTC)
        xa.scan(self.bot)
        self.assertIn('[승인 요청 · CLI-01]', self.tg.sent[0]['text'])
        self.assertEqual(self.run_cli('wait', '--hub', hub, '--id', 'CLI-01', '--timeout', '0.05', '--interval', '0.01'), 3)
        timer = threading.Timer(0.1, lambda: self.press('xa:1:a'))
        timer.start()
        code = self.run_cli('wait', '--hub', hub, '--id', 'CLI-01', '--timeout', '5', '--interval', '0.02')
        timer.join()
        self.assertEqual(code, 0)

    def test_cli_module_entry_point(self):
        done = subprocess.run([sys.executable, '-m', 'app.external_approvals', 'wait', '--hub', str(self.bot.paths.agent_hub),
                               '--id', 'NONE-01', '--timeout', '0.05', '--interval', '0.01'], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(done.returncode, 3)


if __name__ == '__main__':
    unittest.main()

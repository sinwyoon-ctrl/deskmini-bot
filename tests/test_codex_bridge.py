import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.codex_bridge import Bridge, RpcError, server_argv, QUESTION_INSTRUCTIONS
from app.codex_session import CodexSessions
from app.state import StateStore, ConflictError, utc_now

ROOT = Path(__file__).resolve().parents[1]


class Sessions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root/'state/db',ROOT/'schema.sql')
        self.config = {'enabled':True,'projects':{'trial':{'path':str(self.root),'scope':'Only this test workspace'}}}
        self.api = CodexSessions(self.store,self.config)
        self.bridge = Bridge(self.store,self.config,self.root/'socket')
        self.bridge.runtime('READY')
        self.key = self.api.create(100,'trial','create-a')
        self.bridge.update(self.key,thread_id='thread-a',epoch=self.bridge.epoch,state='RUNNING',turn_id='turn-a')

    def query(self, sql, *args):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute(sql,args)]

    def session(self):
        return self.query('SELECT * FROM codex_sessions WHERE id=?',self.key)[0]

    def question(self, method='item/tool/requestUserInput', key=7, **extra):
        p = {'threadId':'thread-a','turnId':'turn-a','itemId':'item-a','questions':[
            {'id':'choice','header':'선택','question':'어느 이름으로 저장할까요?',
             'options':[{'label':'첫째','description':'A'},{'label':'둘째','description':'B'}]}],**extra}
        self.bridge.question(self.session(),{'method':method,'id':key,'params':p})
        return self.query('SELECT * FROM codex_questions ORDER BY rowid DESC')[0]

    def test_duplicate_create_restores_original_focus_after_crash(self):
        other = self.api.create(100,'trial','create-b')
        self.assertEqual(self.api.focused(100)['id'],other)
        self.assertEqual(self.api.create(100,'trial','create-a'),self.key)
        self.assertEqual(self.api.focused(100)['id'],self.key)
        self.assertEqual(len(self.api.rows(100)),2)

    def test_message_target_is_fixed_when_focus_changes_and_update_replays(self):
        self.api.submit(100,'input-a','첫 작업 지시')
        self.api.create(100,'trial','create-b')
        self.assertEqual(self.api.submit(100,'input-a','첫 작업 지시'),self.key)
        row = self.query('SELECT * FROM codex_commands WHERE source=?','input-a')[0]
        self.assertEqual(row['session_id'],self.key)
        self.assertEqual(row['expected_turn'],'turn-a')

    def test_approval_requires_explicit_choice_and_is_consumed_once(self):
        q = self.question('item/commandExecution/requestApproval',command='printf test',cwd=str(self.root))
        with self.assertRaises(ConflictError):
            self.api.answer(100,q['id'],'응')
        self.api.answer(100,q['id'],'accept')
        with self.assertRaises(ConflictError):
            self.api.answer(100,q['id'],'accept')
        self.assertEqual(json.loads(self.query('SELECT * FROM codex_questions')[0]['response']),{'decision':'accept'})

    def test_question_choice_becomes_same_question_answer(self):
        q = self.question()
        self.api.answer(100,q['id'],'2')
        row = self.query('SELECT * FROM codex_questions')[0]
        self.assertEqual(json.loads(row['response']),{'answers':{'choice':{'answers':['둘째']}}})

    def test_cancel_only_denial_is_rendered_and_allowed(self):
        q = self.question('item/commandExecution/requestApproval',command='printf test',availableDecisions=['accept','cancel'])
        payload = self.store.pending_outbox()[-1]['payload']
        self.assertIn(':cancel',json.dumps(payload['reply_markup']))
        self.assertNotIn(':decline',json.dumps(payload['reply_markup']))
        self.api.answer(100,q['id'],'cancel')

    def test_expired_or_previous_server_approval_cannot_be_reused(self):
        q = self.question()
        self.bridge.epoch = 'new-server'
        self.bridge.runtime('READY')
        with self.assertRaises(ConflictError):
            self.api.answer(100,q['id'],'1')

    def test_secret_question_is_not_answered_over_telegram(self):
        q = self.question(questions=[{'id':'secret','question':'SECRET PROMPT','header':'secret','isSecret':True}])
        self.assertNotIn('SECRET PROMPT',str(self.store.pending_outbox()))
        with self.assertRaises(ConflictError):
            self.api.answer(100,q['id'],'secret value')

    def test_large_approval_has_no_accept_button_and_cannot_be_forged(self):
        q = self.question('item/commandExecution/requestApproval',command='echo '+'x'*2800,cwd=str(self.root))
        payload = self.store.pending_outbox()[-1]['payload']
        self.assertNotIn(':accept',json.dumps(payload['reply_markup']))
        with self.assertRaises(ConflictError):
            self.api.answer(100,q['id'],'accept')

    def test_additional_permissions_and_network_host_are_displayed(self):
        self.question('item/commandExecution/requestApproval',command='printf test',cwd=str(self.root),
                      additionalPermissions={'network':{'enabled':True}},networkApprovalContext={'host':'example.test','protocol':'https'})
        text = self.store.pending_outbox()[-1]['payload']['text']
        for word in ('example.test','추가로 요청한 권한',str(self.root)):
            self.assertIn(word,text)

    def test_multi_question_accepts_numbered_answers_without_changing_protocol(self):
        q = self.question(questions=[{'id':'name','question':'이름은?','options':[]},
                                     {'id':'folder','question':'폴더는?','options':[]}])
        text = self.store.pending_outbox()[-1]['payload']['text']
        self.assertIn('질문 번호와 답',text)
        self.assertNotIn('/codex answer',text)
        self.api.answer(100,q['id'],'1. alpha\n2. results')
        response = json.loads(self.query('SELECT response FROM codex_questions WHERE id=?',q['id'])[0]['response'])
        self.assertEqual(response,{'answers':{'name':{'answers':['alpha']},'folder':{'answers':['results']}}})

    def test_multi_question_rejects_ambiguous_answers_and_preserves_json_compatibility(self):
        q = self.question(questions=[{'id':'name','question':'이름은?'},{'id':'folder','question':'폴더는?'}])
        for answer in ('alpha','1. alpha\n1. results','1. alpha\n3. results'):
            with self.assertRaises(ConflictError):
                self.api.answer(100,q['id'],answer)
            self.assertEqual(self.api.pending(self.key)[0]['state'],'PENDING')
        self.api.answer(100,q['id'],'{"name":"alpha","folder":"results"}')
        self.assertEqual(self.query('SELECT state FROM codex_questions WHERE id=?',q['id'])[0]['state'],'ANSWERING')

    def test_approval_card_keeps_exact_command_scope_and_unfamiliar_permission_fields(self):
        command = "python -c 'print(1 < 2)'"
        self.question('item/commandExecution/requestApproval',command=command,cwd=str(self.root),
                      additionalPermissions={'newPermission':{'resource':['one','two']}})
        text = self.store.pending_outbox()[-1]['payload']['text']
        for value in (command,str(self.root),'newPermission','one','two'):
            self.assertIn(value,text)
        self.assertNotIn('"command":',text)
        self.assertIn('확인 번호: q-',text)

    def test_unknown_status_shows_recovery_step_without_claiming_work_is_running(self):
        self.bridge.update(self.key,state='UNKNOWN',observed_at=utc_now())
        text = self.api.describe(100)
        self.assertIn('/codex recover '+self.key,text)
        self.assertIn('자동으로 다시 실행하지 않습니다',text)
        self.assertIn('한국 시간',text)
        self.assertNotIn('UNKNOWN',text)

    def test_permissions_card_explains_turn_scope_and_keeps_actual_scope(self):
        q = self.question('item/permissions/requestApproval',permissions={'network':{'enabled':True}})
        payload = self.store.pending_outbox()[-1]['payload']
        self.assertIn('이번 요청의 작업이 끝날 때까지',payload['text'])
        self.assertIn('여러 번 사용될 수 있습니다',payload['text'])
        self.assertIn('이번 작업 동안 허용',json.dumps(payload['reply_markup'],ensure_ascii=False))
        self.api.answer(100,q['id'],'accept')
        response = json.loads(self.query('SELECT response FROM codex_questions WHERE id=?',q['id'])[0]['response'])
        self.assertEqual(response,{'permissions':{'network':{'enabled':True}},'scope':'turn'})

    def test_stale_heartbeat_does_not_accept_work(self):
        with self.store.connect() as db:
            db.execute('UPDATE codex_runtime SET observed_at=?',((datetime.now(UTC)-timedelta(seconds=20)).isoformat(),))
        with self.assertRaises(ConflictError):
            self.api.submit(100,'new','do work')

    def test_unknown_session_is_not_restarted_by_plain_message(self):
        self.bridge.update(self.key,state='UNKNOWN')
        with self.assertRaises(ConflictError):
            self.api.submit(100,'new','do work')
        self.api.recover(100,self.key,'recover-a')
        self.assertEqual(self.query("SELECT op FROM codex_commands WHERE source='recover-a'")[0]['op'],'recover')


class Events(Sessions):
    def run_async(self, fn):
        return asyncio.run(fn())

    def test_started_tool_before_approval_is_described_as_preparation(self):
        async def check():
            await self.bridge.event({'method':'item/started','params':{'threadId':'thread-a','item':{
                'type':'commandExecution','id':'command-before-approval'}}})
        self.run_async(check)
        text = self.store.pending_outbox()[-1]['payload']['text']
        self.assertIn('준비하고 있습니다',text)
        self.assertNotIn('실행하고 있습니다',text)
        self.assertEqual(self.session()['state'],'RUNNING')

    def test_completion_then_resolved_never_reverts_to_running(self):
        self.question()
        async def check():
            await self.bridge.event({'method':'turn/completed','params':{'threadId':'thread-a','turn':{'id':'turn-a','status':'completed'}}})
            await self.bridge.event({'method':'serverRequest/resolved','params':{'threadId':'thread-a','requestId':7}})
        self.run_async(check)
        self.assertEqual(self.session()['state'],'IDLE')
        self.assertIsNone(self.session()['turn_id'])

    def test_one_resolved_question_does_not_hide_other_pending_question(self):
        self.question(key=7)
        self.question(key=8)
        async def check():
            await self.bridge.event({'method':'serverRequest/resolved','params':{'threadId':'thread-a','requestId':7}})
        self.run_async(check)
        self.assertEqual(self.session()['state'],'WAITING')
        self.assertEqual(len(self.api.pending(self.key)),1)

    def test_start_response_cannot_overwrite_question_event(self):
        self.bridge.update(self.key,state='IDLE',turn_id=None)
        self.api.submit(100,'input-a','실행')
        async def rpc(method,params):
            self.question()
            return {'turn':{'id':'turn-a','status':'inProgress'}}
        self.bridge.rpc = rpc
        async def check():
            await self.bridge.command(self.query("SELECT * FROM codex_commands WHERE source='input-a'")[0])
        self.run_async(check)
        self.assertEqual(self.session()['state'],'WAITING')

    def test_stop_never_cancels_a_newer_turn(self):
        self.api.submit(100,'stop-a','','stop')
        self.bridge.update(self.key,state='RUNNING',turn_id='turn-b')
        async def rpc(*args):
            raise AssertionError('must not interrupt turn-b')
        self.bridge.rpc = rpc
        async def check():
            await self.bridge.command(self.query("SELECT * FROM codex_commands WHERE source='stop-a'")[0])
        self.run_async(check)
        self.assertEqual(self.query("SELECT state FROM codex_commands WHERE source='stop-a'")[0]['state'],'REFUSED')

    def test_lost_ack_is_unknown_and_never_replayed(self):
        self.bridge.update(self.key,state='IDLE',turn_id=None)
        self.api.submit(100,'input-a','실행')
        calls = []
        async def rpc(method,params):
            calls.append(method)
            raise TimeoutError()
        self.bridge.rpc = rpc
        row = self.query("SELECT * FROM codex_commands WHERE source='input-a'")[0]
        async def check():
            await self.bridge.command(row)
            await self.bridge.command(row)
        self.run_async(check)
        self.assertEqual(calls,['turn/start'])
        self.assertEqual(self.query("SELECT state FROM codex_commands WHERE source='input-a'")[0]['state'],'UNKNOWN')

    def test_multiple_expirations_interrupt_only_current_pending_turn(self):
        self.question(key=7)
        self.question(key=8)
        with self.store.connect() as db:
            db.execute("UPDATE codex_questions SET expires_at='2000-01-01'")
        calls = []
        async def rpc(method,params):
            calls.append(method)
            await self.bridge.event({'method':'turn/completed','params':{'threadId':'thread-a','turn':{'id':'turn-a','status':'interrupted'}}})
        self.bridge.rpc = rpc
        self.run_async(self.bridge.answers)
        self.assertEqual(calls,['turn/interrupt'])
        self.assertEqual(self.session()['state'],'IDLE')

    def async_question(self):
        async def check():
            await self.bridge.event({'method':'item/completed','params':{'threadId':'thread-a','turnId':'turn-a',
                'item':{'type':'agentMessage','id':'async-item','text':'파일 이름을 골라 주세요.','delivery':'async',
                        'questions':[{'title':'파일 이름은?','options':['alpha','beta']}]}}})
        self.run_async(check)
        return self.query('SELECT * FROM codex_questions ORDER BY rowid DESC')[0]

    def test_actual_server_command_enables_default_mode_question_tool(self):
        args = server_argv('/fixed/codex',self.root/'socket')
        self.assertEqual(args[args.index('--enable')+1],'default_mode_request_user_input')
        self.assertIn('request_user_input_async',QUESTION_INSTRUCTIONS)

    def test_async_agent_question_renders_real_buttons_and_survives_turn_end(self):
        q = self.async_question()
        payload = self.store.pending_outbox()[-1]['payload']
        self.assertIn('alpha',json.dumps(payload['reply_markup']))
        async def complete():
            await self.bridge.event({'method':'turn/completed','params':{'threadId':'thread-a','turn':{'id':'turn-a','status':'completed'}}})
        self.run_async(complete)
        self.assertEqual(self.session()['state'],'WAITING')
        self.assertEqual(self.api.pending(self.key)[0]['id'],q['id'])

    def test_async_answer_after_turn_end_goes_to_original_thread_once(self):
        q = self.async_question()
        self.bridge.update(self.key,state='WAITING',turn_id=None)
        self.api.answer(100,q['id'],'2')
        self.run_async(self.bridge.answers)
        self.run_async(self.bridge.answers)
        rows = self.query('SELECT * FROM codex_commands WHERE source=?','answer:'+q['id'])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['session_id'],self.key)
        self.assertIn('beta',rows[0]['body'])
        calls = []
        async def rpc(method,params):
            calls.append((method,params))
            return {'turn':{'id':'turn-b','status':'inProgress'}}
        self.bridge.rpc = rpc
        async def command():
            await self.bridge.command(rows[0])
        self.run_async(command)
        self.assertEqual(calls[0][0],'turn/start')
        self.assertEqual(calls[0][1]['threadId'],'thread-a')
        self.assertEqual(self.query('SELECT state FROM codex_questions WHERE id=?',q['id'])[0]['state'],'RESOLVED')

    def test_async_answer_during_same_turn_steers_the_exact_turn(self):
        q = self.async_question()
        self.api.answer(100,q['id'],'1')
        self.run_async(self.bridge.answers)
        row = self.query('SELECT * FROM codex_commands WHERE source=?','answer:'+q['id'])[0]
        calls = []
        async def rpc(method,params):
            calls.append((method,params))
            return {'turnId':'turn-a'}
        self.bridge.rpc = rpc
        async def command():
            await self.bridge.command(row)
        self.run_async(command)
        self.assertEqual(calls[0][0],'turn/steer')
        self.assertEqual(calls[0][1]['expectedTurnId'],'turn-a')

    def test_async_old_question_cannot_answer_a_new_turn(self):
        q = self.async_question()
        self.bridge.update(self.key,state='RUNNING',turn_id='turn-b')
        with self.assertRaises(ConflictError):
            self.api.answer(100,q['id'],'1')

    def test_failed_async_delivery_does_not_leave_a_permanent_wait(self):
        q = self.async_question()
        self.api.answer(100,q['id'],'1')
        self.run_async(self.bridge.answers)
        self.bridge.update(self.key,state='WAITING',turn_id=None)
        row = self.query('SELECT * FROM codex_commands WHERE source=?','answer:'+q['id'])[0]
        async def command():
            await self.bridge.command(row)
        self.run_async(command)
        self.assertEqual(self.query('SELECT state FROM codex_questions WHERE id=?',q['id'])[0]['state'],'CLOSED')
        self.assertEqual(self.session()['state'],'IDLE')

    def test_expired_async_question_releases_idle_conversation(self):
        q = self.async_question()
        self.bridge.update(self.key,state='WAITING',turn_id=None)
        with self.store.connect() as db:
            db.execute("UPDATE codex_questions SET expires_at='2000-01-01' WHERE id=?",(q['id'],))
        self.run_async(self.bridge.answers)
        self.assertEqual(self.session()['state'],'IDLE')
        self.api.submit(100,'after-expiry','질문이 만료된 뒤의 새 지시')
        calls = []
        async def rpc(method,params):
            calls.append(method)
            return {'turn':{'id':'next','status':'inProgress'}}
        self.bridge.rpc = rpc
        async def command():
            await self.bridge.command(self.query("SELECT * FROM codex_commands WHERE source='after-expiry'")[0])
        self.run_async(command)
        self.assertEqual(calls,['turn/start'])

    def test_async_question_stays_closed_after_a_newer_turn_has_completed(self):
        q = self.async_question()
        async def newer():
            await self.bridge.event({'method':'turn/started','params':{'threadId':'thread-a','turn':{'id':'turn-b','status':'inProgress'}}})
            await self.bridge.event({'method':'turn/completed','params':{'threadId':'thread-a','turn':{'id':'turn-b','status':'completed'}}})
        self.run_async(newer)
        with self.assertRaises(ConflictError):
            self.api.answer(100,q['id'],'1')

    def test_queued_async_answer_is_not_sent_after_a_newer_turn(self):
        q = self.async_question()
        self.bridge.update(self.key,state='WAITING',turn_id=None)
        self.api.answer(100,q['id'],'1')
        self.run_async(self.bridge.answers)
        row = self.query('SELECT * FROM codex_commands WHERE source=?','answer:'+q['id'])[0]
        async def newer():
            await self.bridge.event({'method':'turn/started','params':{'threadId':'thread-a','turn':{'id':'turn-b','status':'inProgress'}}})
            await self.bridge.event({'method':'turn/completed','params':{'threadId':'thread-a','turn':{'id':'turn-b','status':'completed'}}})
            await self.bridge.command(row)
        self.run_async(newer)
        self.assertEqual(self.query('SELECT state FROM codex_commands WHERE id=?',row['id'])[0]['state'],'REFUSED')

    def test_answer_causing_a_new_turn_is_not_closed_by_its_own_start(self):
        q = self.async_question()
        self.bridge.update(self.key,state='WAITING',turn_id=None)
        self.api.answer(100,q['id'],'1')
        self.run_async(self.bridge.answers)
        row = self.query('SELECT * FROM codex_commands WHERE source=?','answer:'+q['id'])[0]
        async def rpc(method,params):
            await self.bridge.event({'method':'turn/started','params':{'threadId':'thread-a','turn':{'id':'turn-b','status':'inProgress'}}})
            return {'turn':{'id':'turn-b','status':'inProgress'}}
        self.bridge.rpc = rpc
        async def command():
            await self.bridge.command(row)
        self.run_async(command)
        self.assertEqual(self.query('SELECT state FROM codex_questions WHERE id=?',q['id'])[0]['state'],'RESOLVED')


import test_watch as fixtures


class Routes(fixtures.BotFixture):
    def setup_codex(self):
        config = {'enabled':True,'projects':{'trial':{'path':str(self.home)}}}
        bridge = Bridge(self.bot.store,config,self.home/'socket')
        bridge.runtime('READY')
        self.patch = patch('app.codex_session.load_config',return_value=config)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        return CodexSessions(self.bot.store,config),bridge

    def test_natural_messages_go_to_focused_codex_without_llm_classifier(self):
        api,bridge = self.setup_codex()
        self.bot._handle_update(self.message('코덱스로 trial README 읽어줘',8001))
        key = api.focused(100)['id']
        bridge.update(key,state='IDLE',epoch=bridge.epoch,thread_id='thread-a')
        self.bot._handle_update(self.message('요약은 두 줄로 해줘',8002))
        with self.bot.store.connect() as db:
            rows = db.execute("SELECT * FROM codex_commands WHERE op='message' ORDER BY rowid").fetchall()
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r['session_id']==key for r in rows))
        self.bot._handle_update(self.message('/assistant',8003))
        self.assertIsNone(api.focused(100))

    def test_reply_anchor_overrides_other_focused_question(self):
        api,bridge = self.setup_codex()
        keys = []
        for i in (1,2):
            key = api.create(100,'trial','create-'+str(i))
            bridge.update(key,thread_id='thread-'+str(i),epoch=bridge.epoch,state='RUNNING',turn_id='turn-'+str(i))
            bridge.question(api.focused(100),{'id':i,'method':'item/tool/requestUserInput','params':{
                'threadId':'thread-'+str(i),'turnId':'turn-'+str(i),'itemId':'item-'+str(i),
                'questions':[{'id':'q','question':'질문','header':'선택'}]}})
            keys.append(api.pending(key)[0]['id'])
        update = self.message('첫 작업에 대한 답',8101)
        update['message']['reply_to_message'] = {'text':'확인 요청 '+keys[0]+'\n질문'}
        self.bot._handle_update(update)
        with self.bot.store.connect() as db:
            rows = {r['id']:r['state'] for r in db.execute('SELECT * FROM codex_questions')}
        self.assertEqual(rows[keys[0]],'ANSWERING')
        self.assertEqual(rows[keys[1]],'PENDING')

    def test_unauthorized_update_cannot_create_codex_session(self):
        api,bridge = self.setup_codex()
        update = self.message('/codex new trial test',8201)
        update['message']['from']['id'] = 999
        self.bot._handle_update(update)
        self.assertEqual(api.rows(100),[])

    def test_rendered_question_footer_routes_reply_even_after_focus_switch(self):
        api,bridge = self.setup_codex()
        first = api.create(100,'trial','first')
        bridge.update(first,thread_id='thread-a',epoch=bridge.epoch,state='RUNNING',turn_id='turn-a')
        bridge.question(api.focused(100),{'id':7,'method':'item/tool/requestUserInput','params':{
            'threadId':'thread-a','turnId':'turn-a','itemId':'item-a',
            'questions':[{'id':'name','question':'설명 '*2000+'\n확인 번호: q-0000000000\n이름은?','header':'이름'},
                         {'id':'folder','question':'폴더는?','header':'폴더'}]}})
        displayed = self.bot.store.pending_outbox()[-1]['payload']['text']
        question = api.pending(first)[0]
        self.assertLessEqual(len(displayed),3500)
        self.assertIn('확인 번호: '+question['id'],displayed)
        self.assertIn('대화 번호: '+first,displayed)
        second = api.create(100,'trial','second')
        update = self.message('1. alpha\n2. results',8301)
        update['message']['reply_to_message'] = {'text':displayed}
        self.bot._handle_update(update)
        with self.bot.store.connect() as db:
            row = db.execute('SELECT * FROM codex_questions WHERE id=?',(question['id'],)).fetchone()
        self.assertEqual(row['state'],'ANSWERING')
        self.assertEqual(json.loads(row['response']),{'answers':{'name':{'answers':['alpha']},'folder':{'answers':['results']}}})
        self.assertEqual(api.focused(100)['id'],second)

    def test_permission_callback_receipt_states_duration_before_results(self):
        api,bridge = self.setup_codex()
        key = api.create(100,'trial','permission')
        bridge.update(key,thread_id='thread-a',epoch=bridge.epoch,state='RUNNING',turn_id='turn-a')
        bridge.question(api.focused(100),{'id':7,'method':'item/permissions/requestApproval','params':{
            'threadId':'thread-a','turnId':'turn-a','itemId':'item-a','permissions':{'network':{'enabled':True}}}})
        question = api.pending(key)[0]
        self.press('cx:'+question['id']+':accept',8401)
        text = self.tg.sent[-1]['text']
        self.assertIn('작업이 끝날 때까지 권한',text)
        self.assertNotIn('이번 실행',text)

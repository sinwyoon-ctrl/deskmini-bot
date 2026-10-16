import json
import subprocess
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import test_agent_routing as routing
from app.agent_conversation import enqueue,normalized_limits,process_one,usage_text,ask


class ConversationTests(unittest.TestCase):
    def setUp(self):
        fixture=routing.RoutingTests();fixture.setUp()
        self.addCleanup(fixture.tearDown);self.addCleanup(fixture.doCleanups)
        for name in ['bot','home','tg','key','bridge','queue','say','rows','active','claude','question']:
            setattr(self,name,getattr(fixture,name))
        self.limits={'primary':{'used_percent':77,'window_minutes':10080,'resets_at':datetime.now(UTC)+timedelta(days=6)},
                     'secondary':None,'plan_type':'test','limit_reached':None,'observed_at':datetime.now(UTC)}
        p=patch('app.bot.latest_codex_limits',return_value=self.limits);p.start();self.addCleanup(p.stop)

    def native_limits(self):
        return {'five_hour':{'used_percent':23,'resets_at':None},'seven_day':{'used_percent':8,'resets_at':None}}

    def test_exact_codex_quota_question_reads_existing_information(self):
        before=self.rows('SELECT * FROM codex_commands')
        self.say('코덱스야 지금 쿼터 얼마나 남았어?')
        self.assertIn('23% 남음',self.tg.sent[-1]['text'])
        self.assertIn('관측:',self.tg.sent[-1]['text'])
        self.assertEqual(self.rows('SELECT * FROM codex_commands'),before)
        self.assertEqual(self.rows('SELECT * FROM telegram_conversation_jobs'),[])

    def test_exact_claude_quota_question_without_project(self):
        self.say('클로드야 지금 주간 쿼터 얼마나 남았어')
        self.assertEqual(self.queue.rows(),[])
        self.assertEqual(self.rows('SELECT * FROM telegram_conversation_jobs')[0]['operation'],'quota')
        process_one(self.bot.store,lambda *a:('OK',self.native_limits()));self.bot._flush_outbox()
        self.assertIn('주간: 92% 남음',self.tg.sent[-1]['text'])
        self.assertTrue(self.tg.sent[-1]['text'].startswith('[봇 안내 · 클로드]'))

    def test_generic_chat_without_project_uses_named_model_and_records_source(self):
        self.say('클로드야 안녕')
        self.assertEqual(self.queue.rows(),[])
        self.assertEqual(self.active(),'claude')
        process_one(self.bot.store,lambda agent,prompt,cwd:('클로드의 실제 대화',{}));self.bot._flush_outbox()
        self.assertTrue(self.tg.sent[-1]['text'].startswith('[클로드]'))
        from app.shared_conversation import snapshot
        self.assertEqual(next(e for e in snapshot(self.bot.store,100)['entries'] if '클로드의 실제 대화' in e['text'])['speaker'],'claude')

    def test_quota_and_new_questions_never_answer_pending_codex_question(self):
        self.bridge.update(self.key,turn_id='turn-c',state='RUNNING')
        self.bridge.question(self.rows('SELECT * FROM codex_sessions WHERE id=?',self.key)[0],{'id':1,'method':'item/tool/requestUserInput','params':{
            'turnId':'turn-c','questions':[{'id':'name','header':'이름','question':'어느 이름?','options':[]}]}})
        before=self.rows('SELECT * FROM codex_commands')
        for text in ['코덱스야 지금 쿼터 얼마나 남았어?','코덱스야 왜 그렇게 생각해?','코덱스야 README 읽어줘']:
            self.say(text)
            self.assertEqual(self.rows('SELECT state FROM codex_questions')[0]['state'],'PENDING')
            self.assertEqual(self.rows('SELECT * FROM codex_commands'),before)

    def test_quota_and_general_question_preserve_pending_claude_and_target(self):
        row=self.claude();self.question(row)
        for text in ['클로드야 지금 주간 쿼터 얼마나 남았어','클로드야 왜 그런거야?']:
            self.say(text)
            self.assertEqual(self.rows('SELECT state FROM orca_questions')[0]['state'],'PENDING')
            self.assertEqual(self.rows('SELECT * FROM orca_chat_commands'),[])
            self.assertEqual(self.rows("SELECT target_id FROM telegram_agent_targets WHERE agent='claude'")[0]['target_id'],row['id'])
        self.say('alpha나 beta 말고 gamma로 써줘')
        self.assertEqual(self.rows('SELECT state FROM orca_questions')[0]['state'],'ANSWERING')

    def test_cached_claude_quota_uses_no_extra_model_call(self):
        with self.bot.store.connect() as db:
            db.execute("INSERT INTO telegram_agent_usage VALUES('claude',?,?)",(datetime.now(UTC).isoformat(),json.dumps(self.native_limits())))
        self.say('클로드야 주간 사용량 알려줘')
        self.assertIn('92% 남음',self.tg.sent[-1]['text'])
        self.assertEqual(self.rows('SELECT * FROM telegram_conversation_jobs'),[])

    def test_missing_telemetry_does_not_use_model_guessed_percent(self):
        self.say('클로드야 쿼터 얼마나 남았어?')
        process_one(self.bot.store,lambda *a:('100% 남음',{}));self.bot._flush_outbox()
        self.assertNotIn('100%',self.tg.sent[-1]['text'])
        self.assertIn('확인하지 못했습니다',self.tg.sent[-1]['text'])

    def test_mixed_quota_and_write_is_neither_a_query_nor_a_decision(self):
        self.say('코덱스야 쿼터 얼마나 남았어? 그리고 파일 삭제해줘')
        self.assertEqual(self.rows('SELECT * FROM telegram_conversation_jobs'),[])
        self.assertEqual(self.rows("SELECT * FROM codex_commands WHERE op='message'"),[])
        self.assertIn('나누어',self.tg.sent[-1]['text'])

    def test_general_request_is_idempotent_and_never_replayed_after_failure(self):
        message=self.say('클로드야 안녕');before=self.rows('SELECT * FROM telegram_conversation_jobs')
        self.bot._handle_update(message)
        self.assertEqual(self.rows('SELECT * FROM telegram_conversation_jobs'),before)
        def fail(*a):raise subprocess.TimeoutExpired('test',1)
        process_one(self.bot.store,fail)
        calls=[];process_one(self.bot.store,lambda *a:calls.append(a))
        self.assertEqual(calls,[])
        self.assertEqual(self.rows('SELECT state FROM telegram_conversation_jobs')[0]['state'],'FAILED')

    def test_usage_parser_ignores_missing_invalid_and_textual_values(self):
        events=[{'type':'rate_limit_event','rate_limit_info':{'status':'allowed','unifiedWindows':{
            'five_hour':{'utilization':0.0,'resetsAt':None},'seven_day':{'utilization':0.08,'resetsAt':123}}}}]
        self.assertEqual(normalized_limits(events)['five_hour']['used_percent'],0)
        self.assertEqual(normalized_limits(events)['seven_day']['used_percent'],8)
        for bad in [True,-1,2,float('nan'),'0.2',None]:
            events[0]['rate_limit_info']['unifiedWindows']['seven_day']['utilization']=bad
            self.assertNotIn('seven_day',normalized_limits(events))

    def test_claude_runtime_has_no_tools_and_no_api_key_override(self):
        response=subprocess.CompletedProcess([],0,stdout=json.dumps({'type':'result','subtype':'success','is_error':False,'result':'대답'}))
        with patch('app.agent_conversation.subprocess.run',return_value=response) as run,patch.dict('os.environ',{'ANTHROPIC_API_KEY':'private-placeholder'}):
            self.assertEqual(ask('claude','hello',str(self.home))[0],'대답')
        kwargs=run.call_args.kwargs;command=run.call_args.args[0]
        self.assertEqual(command[command.index('--tools')+1],'')
        self.assertIn('--strict-mcp-config',command)
        self.assertNotIn('ANTHROPIC_API_KEY',kwargs['env'])

    def test_codex_runtime_keeps_read_only_sandbox_and_disabled_execution(self):
        response=subprocess.CompletedProcess([],0,stdout=json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'답변'}}))
        with patch('app.agent_conversation.subprocess.run',return_value=response) as run:
            self.assertEqual(ask('codex','hello',str(self.home))[0],'답변')
        command=run.call_args.args[0]
        self.assertEqual(command[command.index('--sandbox')+1],'read-only')
        for tool in ['shell_tool','unified_exec','multi_agent']:self.assertIn(tool,command)

    def test_limit_error_preserves_actual_quota_telemetry(self):
        events=[{'type':'rate_limit_event','rate_limit_info':{'status':'rejected','unifiedWindows':{'seven_day':{'utilization':1,'resetsAt':None}}}},
                {'type':'result','subtype':'error_during_execution','is_error':True,'result':'quota exceeded'}]
        response=subprocess.CompletedProcess([],1,stdout='\n'.join(json.dumps(e) for e in events))
        with patch('app.agent_conversation.subprocess.run',return_value=response):
            reply,limits=ask('claude','OK',str(self.home))
        self.assertIsNone(reply);self.assertEqual(limits['seven_day']['used_percent'],100)
        self.say('클로드야 주간 쿼터 얼마나 남았어?')
        process_one(self.bot.store,lambda *a:(reply,limits));self.bot._flush_outbox()
        self.assertIn('주간: 0% 남음',self.tg.sent[-1]['text'])
        self.assertTrue(self.tg.sent[-1]['text'].startswith('[봇 안내'))

    def test_explicit_reply_accepts_natural_declarative_answers(self):
        row=self.claude();footer=self.question(row)
        self.say('가능해',footer)
        self.assertEqual(self.rows('SELECT reply_text FROM orca_questions')[0]['reply_text'],'가능해')
        self.assertEqual(self.rows('SELECT * FROM telegram_conversation_jobs'),[])

    def test_conversation_worker_runs_when_native_tick_raises(self):
        from app.orca_bridge import main
        with patch('app.config.Settings.load',return_value=self.bot.settings),patch('app.orca_bridge.OrcaQueue.load',return_value=self.queue),\
             patch('app.orca_bridge.OrcaBridge.tick',side_effect=RuntimeError('native unavailable')),\
             patch('app.agent_conversation.process_one') as process,patch('sys.argv',['orca_bridge','tick']):
            with self.assertRaises(RuntimeError):main()
        process.assert_called_once()

import json
import os
import subprocess
from unittest.mock import patch

from test_watch import BotFixture
from app.agent_routing import handle_message, select
from app.claude_permissions import enqueue, process_identity
from app.codex_bridge import Bridge
from app.codex_session import CodexSessions
from app.orca_bridge import OrcaBridge, OrcaQueue


class RoutingTests(BotFixture):
    def setUp(self):
        super().setUp()
        self.workspace=self.home/'model-work';self.workspace.mkdir()
        self.codex_config={'enabled':True,'projects':{'bot':{'path':str(self.workspace),'label':'텔레그램 봇','aliases':['텔레그램봇','텔레그램 봇']}}}
        self.codex=CodexSessions(self.bot.store,self.codex_config)
        self.bridge=Bridge(self.bot.store,self.codex_config,self.home/'codex.sock');self.bridge.runtime('READY')
        self.key=self.codex.create(100,'bot','setup')
        self.bridge.update(self.key,thread_id='codex-thread',epoch=self.bridge.epoch,state='IDLE')
        self.config={'enabled':True,'run_id':'run-native','coordinator':'coord','projects':{'bot-claude':{
            'path':str(self.workspace),'worktree':'id:repo::'+str(self.workspace),'label':'텔레그램 봇','aliases':['텔레그램봇','텔레그램 봇'],
            'ownership_ready':True,'claude_permissions':True,'claude_model':'claude-opus-5-5','scope':'Read test workspace only'}}}
        self.queue=OrcaQueue(self.bot.store,self.config)
        for target,value in [('app.agent_routing.load_config',self.codex_config),('app.codex_session.load_config',self.codex_config),('app.orca_bridge.OrcaQueue.load',self.queue)]:
            patched=patch(target,return_value=value);patched.start();self.addCleanup(patched.stop)
        self.number=1000

    def say(self,text,reply=None,*,direct=False):
        self.number+=1;message=self.message(text,self.number)
        if reply:message['message']['reply_to_message']={'text':reply}
        if direct:handle_message(self.bot,message['message'],self.number)
        else:self.bot._handle_update(message)
        return message

    def rows(self,query,*args):
        with self.bot.store.connect() as db:return [dict(r) for r in db.execute(query,args)]

    def active(self):
        return self.rows('SELECT agent FROM telegram_agent_focus WHERE chat_id=100')[0]['agent']

    def claude(self):
        self.say('클로드야, 텔레그램 봇 README를 요약해줘')
        row=self.queue.rows()[-1]
        self.queue.update(row['id'],state='RUNNING',run_id='run-native',task_id='native-task',dispatch_id='native-dispatch')
        return self.queue.rows()[-1]

    def question(self,row,key='msg-native'):
        self.queue.update(row['id'],state='ATTENTION')
        with self.bot.store.connect() as db:db.execute('INSERT INTO orca_questions(message_id,request_id,body) VALUES(?,?,?)',(key,row['id'],'alpha 또는 beta?'))
        return '질문 번호: '+key+'\n작업 번호: '+row['id']

    def test_claude_explicit_call_bypasses_existing_codex_focus(self):
        before=len(self.rows('SELECT * FROM codex_commands'))
        row=self.claude()
        self.assertEqual(row['model'],'claude-opus-5-5')
        self.assertIn('README',row['request_text'])
        self.assertEqual(len(self.rows('SELECT * FROM codex_commands')),before)
        self.assertEqual(self.active(),'claude')
        self.assertIn('[봇 안내 · 클로드]',self.tg.sent[-1]['text'])

    def test_each_agent_keeps_its_target_and_unnamed_followup_uses_selected(self):
        row=self.claude()
        self.say('현재 내용에 주의사항도 추가해줘')
        self.assertEqual(self.rows('SELECT * FROM orca_chat_commands')[0]['request_id'],row['id'])
        self.say('코덱스야, README를 다시 확인해줘')
        message=self.rows("SELECT * FROM codex_commands WHERE op='message'")[-1]
        self.assertEqual(message['session_id'],self.key)
        self.say('예시도 하나 추가해줘')
        self.assertEqual(len(self.rows("SELECT * FROM codex_commands WHERE op='message'")),2)
        self.say('클로드야')
        self.assertEqual(self.active(),'claude')
        self.assertEqual(self.rows("SELECT target_id FROM telegram_agent_targets WHERE agent='claude'")[0]['target_id'],row['id'])

    def test_korean_particles_and_english_calls(self):
        for phrase in ('코덱스야,','코덱스로','코덱스에게','Codex:'):
            self.say(phrase+' README를 읽어줘')
        self.assertEqual(len(self.rows("SELECT * FROM codex_commands WHERE op='message'")),4)
        self.say('Claude: 텔레그램봇 README를 읽어줘')
        self.assertEqual(self.queue.rows()[-1]['agent'],'claude')

    def test_project_clarification_retains_called_agent(self):
        self.say('클로드야 README를 읽어줘')
        self.assertEqual(self.active(),'claude')
        self.say('텔레그램 봇 README를 읽어줘')
        self.assertEqual(len(self.queue.rows()),1)
        self.assertEqual(self.rows("SELECT * FROM codex_commands WHERE op='message'"),[])

    def test_status_never_consumes_claude_question(self):
        row=self.claude();self.question(row)
        self.say('지금 어디까지 했어?')
        self.assertEqual(self.rows('SELECT state FROM orca_questions')[0]['state'],'PENDING')
        self.assertEqual(self.rows('SELECT * FROM orca_chat_commands'),[])
        self.assertIn('alpha 또는 beta',self.tg.sent[-1]['text'])
        self.say('지금 뭐 하고 있어? 그리고 alpha 말고 gamma로 해')
        self.assertEqual(self.rows('SELECT state FROM orca_questions')[0]['state'],'PENDING')
        self.assertIn('조회만 처리했습니다',self.tg.sent[-1]['text'])

    def test_freeform_answer_goes_only_to_selected_claude_question(self):
        row=self.claude();self.question(row)
        answer='alpha나 beta 말고 gamma로 써줘'
        self.say(answer)
        q=self.rows('SELECT * FROM orca_questions')[0]
        self.assertEqual(q['reply_text'],answer);self.assertEqual(q['state'],'ANSWERING')
        self.assertEqual(len(self.rows("SELECT * FROM codex_commands WHERE op='message'")),0)

    def test_old_claude_question_reply_keeps_current_codex_selection(self):
        row=self.claude();anchor=self.question(row)
        self.say('코덱스야')
        self.say('gamma로 써줘',anchor)
        self.assertEqual(self.rows('SELECT reply_text FROM orca_questions')[0]['reply_text'],'gamma로 써줘')
        self.assertEqual(self.active(),'codex')

    def test_explicit_agent_and_reply_anchor_conflict_does_not_send(self):
        row=self.claude();anchor=self.question(row)
        self.say('코덱스야, gamma로 써줘',anchor)
        self.assertEqual(self.rows('SELECT state FROM orca_questions')[0]['state'],'PENDING')
        self.assertEqual(len(self.rows("SELECT * FROM codex_commands WHERE op='message'")),0)
        self.assertIn('어느 쪽에도 보내지 않았습니다',self.tg.sent[-1]['text'])

    def test_codex_question_anchor_does_not_move_claude_selection(self):
        self.bridge.update(self.key,turn_id='turn-c',state='RUNNING')
        self.bridge.question(self.rows('SELECT * FROM codex_sessions WHERE id=?',self.key)[0],{'id':1,'method':'item/tool/requestUserInput','params':{
            'turnId':'turn-c','questions':[{'id':'name','header':'이름','question':'어느 이름?','options':[]}]}})
        q=self.rows('SELECT * FROM codex_questions')[0]
        self.claude()
        self.say('gamma','확인 번호: '+q['id'])
        result=self.rows('SELECT response FROM codex_questions')[0]['response']
        self.assertEqual(json.loads(result)['answers']['name']['answers'],['gamma'])
        self.assertEqual(self.active(),'claude')

    def test_claude_permission_is_not_approved_by_natural_text(self):
        row=self.claude();pid=os.getpid()
        payload={'cwd':str(self.workspace),'session_id':'sid','tool_name':'Bash','tool_input':{'command':'echo test'}}
        with self.bot.store.connect() as db:key=enqueue(db,row,payload,pid,process_identity(pid))
        self.say('응 진행해')
        self.assertEqual(self.rows('SELECT state FROM claude_permissions WHERE id=?',key)[0]['state'],'PENDING')
        self.assertEqual(self.rows('SELECT * FROM orca_chat_commands'),[])

    def test_success_starts_contextual_new_task_but_failure_does_not(self):
        row=self.claude();self.queue.update(row['id'],state='SUCCEEDED',summary='README에는 설정 방법이 있다')
        self.say('그 내용을 한국어로 더 짧게 써줘')
        rows=self.queue.rows();self.assertEqual(len(rows),2)
        self.assertIn('이전 결과를 참고하는 새',self.tg.sent[-1]['text'])
        self.assertNotIn('README에는 설정 방법이 있다',rows[-1]['request_text'])
        spec=OrcaBridge(self.queue).spec(rows[-1])
        self.assertIn('README에는 설정 방법이 있다',spec)
        self.assertNotIn('README에는 설정 방법이 있다',spec.split('[현재 요청 ·',1)[1])
        self.queue.update(rows[-1]['id'],state='UNKNOWN')
        self.say('다시 해줘')
        self.assertEqual(len(self.queue.rows()),2)

    def test_replayed_input_is_bound_to_original_agent_and_dispatch(self):
        row=self.claude();message=self.say('원래 클로드 작업에만 적용해줘')
        self.say('코덱스야')
        handle_message(self.bot,message['message'],message['update_id'])
        commands=self.rows('SELECT * FROM orca_chat_commands')
        self.assertEqual(len(commands),1);self.assertEqual(commands[0]['dispatch_id'],'native-dispatch')
        self.assertEqual(self.active(),'codex')
        self.assertEqual(len(self.rows("SELECT * FROM codex_commands WHERE op='message'")),0)

    def test_older_selection_cannot_steal_later_agent_or_target(self):
        with self.bot.store.transaction() as db:
            select(db,100,'claude','newer','telegram:200')
            select(db,100,'codex',self.key,'telegram:199')
            select(db,100,'claude','older','telegram:198')
        self.assertEqual(self.active(),'claude')
        self.assertEqual(self.rows("SELECT target_id FROM telegram_agent_targets WHERE agent='claude'")[0]['target_id'],'newer')

    def test_assistant_mode_clears_selection_without_stopping_workers(self):
        row=self.claude();self.say('/assistant')
        self.assertEqual(self.rows('SELECT * FROM telegram_agent_focus'),[])
        self.assertEqual(self.queue.rows()[0]['state'],'RUNNING')

    def test_ambiguous_call_and_unregistered_project_do_not_create(self):
        self.say('클로드야, 코덱스야 둘 다 해줘')
        self.assertEqual(self.queue.rows(),[])
        self.say('클로드야 등록되지않은프로젝트 README를 읽어줘')
        self.assertEqual(self.queue.rows(),[])

    def test_source_tags_follow_event_source(self):
        session=self.rows('SELECT * FROM codex_sessions WHERE id=?',self.key)[0]
        self.bridge.notify(session,'agent','실제 답변',model=True)
        self.bridge.notify(session,'status','작업 중')
        row=self.claude();native=OrcaBridge(self.queue)
        native.notify(row,'result','실제 클로드 결과',model=True)
        native.notify(row,'progress','실행 중')
        text=[r['payload']['text'] for r in self.bot.store.pending_outbox()]
        text += [r['text'] for r in self.tg.sent]
        self.assertTrue(any(t.startswith('[코덱스]') for t in text))
        self.assertTrue(any(t.startswith('[클로드]') for t in text))
        self.assertTrue(any(t.startswith('[봇 안내 · 코덱스]') for t in text))
        self.assertTrue(any(t.startswith('[봇 안내 · 클로드]') for t in text))

    def test_dispatch_followup_has_stable_retry_identity_and_no_retarget(self):
        row=self.claude();self.say('추가 설명을 붙여줘')
        calls=[]
        class Client:
            def call(inner,*args):
                calls.append(args)
                if len(calls)==1:raise subprocess.TimeoutExpired('orca',1)
                return {'message':{'id':'native-message'}}
        native=OrcaBridge(self.queue,Client())
        worker={'dispatchId':'native-dispatch','taskId':'native-task','runId':'run-native',
                'resource':{'ownershipState':'owned'},'projection':{'liveness':{'verdict':'live'}}}
        native.deliver_chat([worker]);native.deliver_chat([worker])
        self.assertEqual(self.rows('SELECT state FROM orca_chat_commands')[0]['state'],'SENT')
        self.assertEqual(calls[0],calls[1]);self.assertIn('dispatch:native-dispatch',calls[0])
        self.say('두 번째 추가 지시')
        self.queue.update(row['id'],dispatch_id='replacement')
        native.deliver_chat([worker])
        self.assertEqual(len(calls),2)
        self.assertEqual(self.rows('SELECT state FROM orca_chat_commands')[-1]['state'],'REFUSED')

    def test_shared_model_replies_reach_the_other_model_with_frozen_context(self):
        session=self.rows('SELECT * FROM codex_sessions WHERE id=?',self.key)[0]
        self.bridge.notify(session,'message:actual-a','코덱스가 확인한 고유 내용 CODEX-FACT',model=True)
        self.bot._flush_outbox()
        row=self.claude()
        self.assertIn('CODEX-FACT',OrcaBridge(self.queue).spec(row))
        native=OrcaBridge(self.queue);native.notify(row,'native-dispatch:SUCCEEDED','클로드가 확인한 고유 내용 CLAUDE-FACT',model=True)
        self.bot._flush_outbox()
        message=self.say('코덱스 너는 어떻게 생각해?')
        command=self.rows("SELECT * FROM codex_commands WHERE source=?",'telegram:'+str(message['update_id']))[0]
        self.assertIn('CLAUDE-FACT',command['body'])
        self.assertIn('코덱스 너는 어떻게 생각해?',self.rows('SELECT text FROM chat_messages WHERE role=\'user\' ORDER BY id DESC LIMIT 1')[0]['text'])
        before=command['body']
        native.notify(row,'native-dispatch:later','AFTER-SNAPSHOT',model=True);self.bot._flush_outbox()
        handle_message(self.bot,message['message'],message['update_id'])
        self.assertEqual(self.rows('SELECT body FROM codex_commands WHERE id=?',command['id'])[0]['body'],before)

    def test_conversation_takeover_inherits_only_explicit_common_project(self):
        self.codex_config['projects']['bot']['conversation_project']='bot-shared'
        self.config['projects']['bot-claude']['conversation_project']='bot-shared'
        self.say('클로드가 대화 넘겨받아')
        rows=self.queue.rows();self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['project'],'bot-claude')
        self.assertIn('대화',rows[0]['request_text'])
        self.assertEqual(self.active(),'claude')

    def test_direct_codex_file_handoff_does_not_switch_unrelated_native_work(self):
        row=self.claude();self.say('코덱스야')
        self.say('클로드로 이어줘')
        self.assertEqual(self.rows('SELECT * FROM orca_changes'),[])
        self.assertEqual(self.queue.rows()[0]['dispatch_id'],row['dispatch_id'])
        self.assertIn('파일 작업은 아직 인계하지',self.tg.sent[-1]['text'])

    def test_takeover_does_not_answer_either_pending_question(self):
        row=self.claude();self.question(row)
        self.say('클로드가 대화 넘겨받아')
        self.assertEqual(self.rows('SELECT state FROM orca_questions')[0]['state'],'PENDING')
        self.assertEqual(self.rows('SELECT * FROM orca_chat_commands'),[])
        self.bridge.update(self.key,turn_id='turn-c',state='RUNNING')
        self.bridge.question(self.rows('SELECT * FROM codex_sessions WHERE id=?',self.key)[0],{'id':1,'method':'item/tool/requestUserInput','params':{
            'turnId':'turn-c','questions':[{'id':'name','header':'이름','question':'어느 이름?','options':[]}]}})
        self.say('코덱스가 대화 넘겨받아')
        self.assertEqual(self.rows('SELECT state FROM codex_questions')[0]['state'],'PENDING')
        self.assertEqual(self.active(),'codex')
        self.assertIn('결정 답변으로 보내지 않았습니다',self.tg.sent[-1]['text'])

    def test_last_question_footer_wins_over_quoted_older_ids(self):
        row=self.claude();footer=self.question(row)
        self.say('gamma','옛 질문 인용\n확인 번호: q-1234567890\n\n'+footer)
        self.assertEqual(self.rows('SELECT reply_text FROM orca_questions')[0]['reply_text'],'gamma')
        self.bridge.update(self.key,turn_id='turn-c',state='RUNNING')
        self.bridge.question(self.rows('SELECT * FROM codex_sessions WHERE id=?',self.key)[0],{'id':1,'method':'item/tool/requestUserInput','params':{
            'turnId':'turn-c','questions':[{'id':'name','header':'이름','question':'어느 이름?','options':[]}]}})
        question=self.rows('SELECT * FROM codex_questions')[0]
        self.say('delta','질문 번호: old-native\n인용 끝\n확인 번호: '+question['id'])
        self.assertEqual(json.loads(self.rows('SELECT response FROM codex_questions')[0]['response'])['answers']['name']['answers'],['delta'])

    def test_unrecognized_reply_never_answers_current_question(self):
        row=self.claude();self.question(row)
        self.say('응','질문이 아닌 이전 알림')
        self.assertEqual(self.rows('SELECT state FROM orca_questions')[0]['state'],'PENDING')

    def test_legacy_codex_focus_blocks_unrelated_native_handoff(self):
        row=self.claude()
        with self.bot.store.connect() as db:
            db.execute('DELETE FROM telegram_agent_focus')
            db.execute('DELETE FROM telegram_agent_targets')
        self.say('지금 작업 클로드로 넘겨')
        self.assertEqual(self.rows('SELECT * FROM orca_changes'),[])
        self.assertEqual(self.queue.rows()[0]['dispatch_id'],row['dispatch_id'])

    def test_completed_native_worker_cannot_receive_followup(self):
        self.claude();self.say('추가 설명을 붙여줘')
        calls=[]
        class Client:
            def call(inner,*args):
                calls.append(args)
                return {'message':{'id':'unexpected'}}
        native=OrcaBridge(self.queue,Client())
        worker={'dispatchId':'native-dispatch','taskId':'native-task','runId':'run-native',
            'workerState':'succeeded','dispatchStatus':'completed',
            'resource':{'ownershipState':'owned'},'projection':{'liveness':{'verdict':'live'}}}
        native.deliver_chat([worker])
        self.assertEqual(calls,[])
        self.assertEqual(self.rows('SELECT state FROM orca_chat_commands')[0]['state'],'REFUSED')

    def test_question_reply_waits_until_queued_followup_is_confirmed(self):
        row=self.claude();footer=self.question(row)
        self.say('gamma',footer)
        self.say('클로드야, 삭제하지 말고 보고만 해')
        worker={'dispatchId':'native-dispatch','taskId':'native-task','runId':'run-native',
            'workerState':'working','dispatchStatus':'running',
            'resource':{'ownershipState':'owned'},'projection':{'liveness':{'verdict':'live'}}}
        calls=[];failed=[True]
        class Client:
            def call(inner,*args):
                calls.append(args)
                if args[1]=='send':
                    if failed[0]:raise subprocess.TimeoutExpired('orca',1)
                    return {'message':{'id':'confirmed'}}
                if args[1]=='task-list':return {'tasks':[]}
                if args[1]=='worker-list':return {'workers':[worker]}
                if args[1]=='check':return {'messages':[]}
                if args[1]=='reply':return {}
                raise AssertionError(args)
        native=OrcaBridge(self.queue,Client())
        native.reconcile()
        self.assertFalse(any(args[1]=='reply' for args in calls))
        self.assertEqual(self.rows('SELECT state FROM orca_questions')[0]['state'],'ANSWERING')
        failed[0]=False;native.reconcile()
        self.assertEqual(sum(args[1]=='reply' for args in calls),1)
        self.assertEqual(self.rows('SELECT state FROM orca_chat_commands')[0]['state'],'SENT')

    def test_model_questions_keep_sender_after_native_handoff(self):
        from app.shared_conversation import snapshot
        self.bridge.update(self.key,turn_id='turn-c',state='RUNNING')
        session=self.rows('SELECT * FROM codex_sessions WHERE id=?',self.key)[0]
        self.bridge.question(session,{'id':1,'method':'item/tool/requestUserInput','params':{
            'turnId':'turn-c','questions':[{'id':'name','header':'이름','question':'CODEX-QUESTION 이름?','options':[]}]}})
        self.bridge.notify(session,'permission:fake','APPROVAL-NOTICE')
        row=self.claude();native=OrcaBridge(self.queue)
        native.notify(row,'question:original','CLAUDE-QUESTION',model=True,question_id='original')
        self.bot._flush_outbox()
        self.queue.update(row['id'],agent='codex',dispatch_id='later-dispatch')
        entries=snapshot(self.bot.store,100)['entries']
        self.assertEqual(next(e for e in entries if 'CODEX-QUESTION' in e['text'])['speaker'],'codex')
        self.assertEqual(next(e for e in entries if 'CLAUDE-QUESTION' in e['text'])['speaker'],'claude')
        self.assertEqual(next(e for e in entries if 'APPROVAL-NOTICE' in e['text'])['speaker'],'assistant')

import fcntl
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from app.orca_bridge import OrcaBridge, OrcaQueue
from app.orca_handoff import Handoff, project_context
from app.policy import Policy
import test_orca_bridge as baseline
from test_watch import BotFixture


class Native(baseline.FakeNative):
    def __init__(self):
        super().__init__()
        self.stop_lost = False
        self.stop_unproven = False
        self.crash_after_stop = False
        self.fail_model = False
        self.contexts_seen = []

    def call(self,*args):
        cmd=args[1]
        if cmd == 'worker-stop':
            self.calls.append(args)
            worker=next(w for w in self.workers if w['dispatchId']==args[args.index('--dispatch')+1])
            worker.update(workerState='stopped',dispatchStatus='failed')
            worker['projection']['liveness']['verdict']='unverifiable' if self.stop_unproven else 'exited'
            if self.crash_after_stop:
                self.crash_after_stop=False
                raise KeyboardInterrupt('simulated process crash')
            if self.stop_lost:
                self.stop_lost=False
                raise RuntimeError('lost stop response')
            return {'state':'stopped'}
        if cmd == 'task-update':
            self.calls.append(args)
            return {}
        if cmd == 'task-create':
            result=super().call(*args)
            task=next(t for t in self.tasks if t['id']==result['task']['id'])
            task['spec']=args[args.index('--spec')+1]
            return result
        before=self.launches
        try:
            result=super().call(*args)
        finally:
            if cmd=='worker-start' and self.launches>before:
                w=self.workers[-1]
                w['projection']={'liveness':{'verdict':'live'}}
                w['resource']={'ownerDispatchId':w['dispatchId'],'ownershipState':'owned'}
                spec=next(t['spec'] for t in self.tasks if t['id']==w['taskId'])
                prefix='read handoff context at '
                path=spec.split(prefix)[1].split(' if present')[0]
                self.contexts_seen.append(json.loads(Path(path).read_text()) if Path(path).exists() else None)
                if self.fail_model:
                    w.update(workerState='failed',dispatchStatus='failed')
                    w['projection']['liveness']['verdict']='exited'
        if cmd=='worker-release':
            w=next(w for w in self.workers if w['dispatchId']==args[args.index('--dispatch')+1])
            w['projection']['liveness']['verdict']='exited'
        if cmd=='worker-start' and self.fail_model:
            result=dict(result,state='failed',failedStage='agent_readiness')
        return result


class HandoffTests(unittest.TestCase):
    setUp=baseline.BridgeTests.setUp
    submit=baseline.BridgeTests.submit

    def setup_native(self):
        self.native=Native()
        self.bridge=OrcaBridge(self.queue,self.native)
        self.handoff=Handoff(self.queue)
        self.row=self.submit()
        self.bridge.tick()

    def switch(self, source='telegram:8', target='claude'):
        return self.handoff.request(self.row['id'],source,target,'추가 회귀 검증 후 계속')

    def current(self):
        return self.queue.rows()[0]

    def test_live_manual_switch_stops_owner_preserves_task_context_and_completion(self):
        self.setup_native()
        self.queue.update(self.row['id'],summary='결정: UI 유지; tests: 12 passed; next: 회귀 검증')
        old=self.current()['task_id']
        self.switch()
        self.bridge.tick()
        row=self.current()
        self.assertEqual((row['state'],row['agent'],row['task_id']),('RUNNING','claude',old))
        self.assertEqual(self.native.launches,2)
        self.assertEqual(self.native.workers[0]['projection']['liveness']['verdict'],'exited')
        self.assertIn('--retry-of',self.native.calls[-1] if self.native.calls[-1][1]=='worker-start' else next(c for c in reversed(self.native.calls) if c[1]=='worker-start'))
        context=self.native.contexts_seen[-1]
        self.assertIn('12 passed',context['checkpoint'])
        self.assertEqual(context['objective'],self.row['request_text'])
        self.assertIn('회귀',context['instruction'])
        self.assertEqual(len(context['attempts']),1)
        self.native.complete(index=1)
        self.bridge.tick()
        self.assertEqual(self.current()['state'],'SUCCEEDED')
        self.assertTrue(any('SUCCEEDED' in o['dedupe_key'] for o in self.store.pending_outbox()))

    def test_duplicate_source_and_competing_change_fenced(self):
        self.setup_native(); first=self.switch()
        self.assertEqual(self.switch()['source_key'],first['source_key'])
        with self.assertRaises(ValueError): self.switch('telegram:9','astra')
        self.bridge.tick()
        self.switch(); self.bridge.tick()
        self.assertEqual(self.native.launches,2)
        self.assertEqual(self.queue.submit('telegram:7','trial','codex',self.row['request_text'])['id'],self.row['id'])

    def test_unverifiable_or_missing_owner_never_stops_or_launches(self):
        for missing in (True,False):
            with self.subTest(missing=missing):
                self.setup_native()
                if missing: self.native.workers=[]
                else: self.native.workers[0]['projection']['liveness']['verdict']='unverifiable'
                self.switch(); self.bridge.tick()
                self.assertEqual(self.current()['state'],'BLOCKED')
                self.assertEqual(self.native.launches,1)
                self.assertFalse(any(c[1]=='worker-stop' for c in self.native.calls))
                # separate source/DB for the next subcase
                if missing: self.tear_down_for_subcase()

    def tear_down_for_subcase(self):
        self.setUp()

    def test_stop_receipt_without_process_exit_blocks(self):
        self.setup_native(); self.native.stop_unproven=True
        self.switch(); self.bridge.tick()
        self.assertEqual(self.current()['state'],'UNKNOWN')
        self.assertEqual(self.native.launches,1)
        self.queue.recover(self.row['id']); self.bridge.tick()
        self.assertEqual(self.native.launches,1)

    def test_crash_after_stop_before_launch_resumes_same_task(self):
        self.setup_native(); self.native.crash_after_stop=True; self.switch()
        with self.assertRaises(KeyboardInterrupt): self.bridge.tick()
        self.assertEqual(self.native.launches,1)
        OrcaBridge(OrcaQueue(self.store,self.config),self.native).tick()
        self.assertEqual(self.native.launches,2)
        self.assertEqual(len(self.native.tasks),1)

    def test_lost_stop_response_requires_recovery_and_never_double_stops(self):
        self.setup_native(); self.native.stop_lost=True; self.switch(); self.bridge.tick()
        self.assertEqual(self.current()['state'],'UNKNOWN')
        self.bridge.tick(); self.assertEqual(self.native.launches,1)
        self.queue.recover(self.row['id']); self.bridge.tick()
        self.assertEqual(self.native.launches,2)
        self.assertEqual(sum(c[1]=='worker-stop' for c in self.native.calls),1)

    def test_lost_launch_response_replays_exact_argv_once(self):
        self.setup_native(); self.switch(); self.native.fail_start=True; self.bridge.tick()
        args=[c for c in self.native.calls if c[1]=='worker-start'][-1]
        self.assertEqual(self.current()['state'],'UNKNOWN')
        self.queue.recover(self.row['id']); self.bridge.tick()
        self.assertEqual(self.native.launches,2)
        self.assertEqual([c for c in self.native.calls if c[1]=='worker-start'][-1],args)
        self.assertEqual(self.current()['state'],'RUNNING')

    def test_completed_business_task_gets_new_native_task_and_later_retry(self):
        self.setup_native(); self.native.complete(); self.bridge.tick()
        old=self.current()['task_id']; self.switch(); self.bridge.tick()
        self.assertNotEqual(self.current()['task_id'],old)
        self.assertEqual(self.current()['id'],self.row['id'])
        self.switch('telegram:9','astra'); self.bridge.tick()
        self.assertEqual(len(self.native.tasks),2)
        self.assertEqual(self.current()['model'],'gpt-6-astra')

    def test_queued_switch_starts_only_selected_agent(self):
        self.native=Native();self.bridge=OrcaBridge(self.queue,self.native);self.handoff=Handoff(self.queue)
        self.row=self.submit();self.switch();self.bridge.tick()
        self.assertEqual(self.native.launches,1)
        self.assertEqual(self.current()['agent'],'claude')
        self.switch('telegram:9','astra');self.bridge.tick()
        self.assertEqual(self.current()['model'],'gpt-6-astra')

    def test_failed_model_is_honest_and_no_quota_fallback(self):
        self.setup_native();self.switch();self.native.fail_model=True;self.bridge.tick()
        self.assertEqual(self.current()['state'],'UNKNOWN')
        self.bridge.tick()
        self.assertEqual(self.native.launches,2)
        self.assertNotEqual(self.current()['state'],'RUNNING')

    def test_unknown_initial_launch_cannot_be_replaced(self):
        self.setup_native();self.queue.update(self.row['id'],state='UNKNOWN',dispatch_id=None)
        with self.assertRaises(ValueError): self.switch()
        self.assertEqual(self.native.launches,1)

    def test_old_task_without_context_contract_migrates_under_same_business_id(self):
        self.setup_native()
        with self.store.connect() as db: db.execute('DELETE FROM orca_task_contracts')
        self.switch();self.bridge.tick()
        self.assertEqual(self.native.launches,2)
        self.assertEqual(len(self.native.tasks),2)
        self.assertEqual(self.current()['id'],self.row['id'])
        self.assertIn('Pre-upgrade',self.native.contexts_seen[-1]['native_task_migration'])

    def test_hub_lock_and_external_import_block(self):
        self.row=self.queue.submit('import:1','trial','codex','외부 작업',imported=True)
        self.handoff=Handoff(self.queue);self.switch();self.bridge.tick()
        self.assertEqual(self.current()['state'],'BLOCKED')
        self.assertFalse(self.native.calls and any(c[1]=='worker-start' for c in self.native.calls))

    def test_hub_owner_is_never_overwritten(self):
        lock=self.root/'hub/locks/trial__hero.lock';lock.parent.mkdir(parents=True);lock.write_text('claude host ongoing')
        self.submit();self.bridge.tick()
        self.assertEqual(self.current()['state'],'BLOCKED')
        self.assertEqual(lock.read_text(),'claude host ongoing')
        self.assertEqual(self.native.launches,0)

    def test_concurrent_tick_fences_switch(self):
        self.setup_native()
        with (self.store.db_path.parent/'orca-bridge.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            with self.assertRaises(ValueError): self.switch()
        self.assertEqual(self.current()['state'],'RUNNING')

    def test_start_cas_rejects_stale_queued_row_after_switch(self):
        self.row=self.submit();self.handoff=Handoff(self.queue)
        self.switch();self.bridge.start(self.row)
        self.assertEqual(self.native.launches,0)
        self.assertEqual(self.current()['state'],'HANDOFF')

    def test_config_change_cannot_move_existing_task(self):
        self.setup_native();self.switch()
        self.config['projects']['trial']['worktree']='id:other::/other'
        self.bridge.tick()
        self.assertEqual(self.native.launches,1)
        self.assertEqual(self.current()['state'],'UNKNOWN')

    def test_new_request_cannot_steal_live_hub_lease(self):
        self.setup_native()
        row=self.submit('telegram:10')
        with self.assertRaises(ValueError): self.bridge.guard_project(row)
        lock=self.root/'hub/locks/trial.lock'
        self.assertIn(self.row['id'],lock.read_text())
        self.native.complete();self.bridge.tick()
        self.assertNotIn(self.row['id'],lock.read_text())  # queued request may now own the lease

    def test_reply_loss_reuses_durable_reply_identity(self):
        self.setup_native()
        with self.store.connect() as db:
            db.execute("INSERT INTO orca_questions(message_id,request_id,body) VALUES('q',?,'choose')",(self.row['id'],))
        self.queue.update(self.row['id'],state='ATTENTION')
        self.queue.answer(self.row['id'],'telegram:reply','yes')
        call=self.native.call
        seen=[]
        def fail_once(*args):
            if args[1]=='reply':
                seen.append(args)
                if len(seen)==1: raise RuntimeError('lost reply response')
            return call(*args)
        with patch.object(self.native,'call',side_effect=fail_once):
            with self.assertRaises(RuntimeError):self.bridge.tick()
            self.bridge.tick()
        self.assertEqual(seen[0],seen[1])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM orca_questions").fetchone()[0],'ANSWERED')

    def test_question_old_dispatch_cannot_change_new_owner(self):
        self.setup_native();self.switch();self.bridge.tick()
        self.native.messages=[{'id':'old-q','type':'question','run_id':'run_1','from_handle':'term_1',
            'body':'old question','payload':{'taskId':'task_1','dispatchId':'ctx_1'}}]
        self.bridge.tick()
        self.assertEqual(self.current()['state'],'RUNNING')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM orca_questions').fetchone()[0],0)

    def test_outbox_failure_cannot_commit_running_without_receipt(self):
        self.setup_native();self.switch()
        original=self.bridge.notify
        def fail_running(row,state,text,db=None):
            if db is not None and text=='인계 실행 중': raise RuntimeError('crash before outbox')
            return original(row,state,text,db)
        with patch.object(self.bridge,'notify',side_effect=fail_running): self.bridge.tick()
        self.assertEqual(self.current()['state'],'UNKNOWN')
        self.assertEqual(self.native.launches,2)
        self.queue.recover(self.row['id']);self.bridge.tick()
        self.assertEqual(self.native.launches,2)
        self.assertEqual(self.current()['state'],'RUNNING')
        self.assertTrue(any('telegram:8:RUNNING' in o['dedupe_key'] for o in self.store.pending_outbox()))

    def test_repeated_switch_context_is_nonrecursive_and_spec_bounded(self):
        self.setup_native()
        sizes=[]
        for n in range(5):
            self.switch('telegram:repeat-'+str(n),'astra' if n%2 else 'sol')
            self.bridge.tick()
            context=self.native.contexts_seen[-1]
            sizes.append(len(json.dumps(context)))
            self.assertTrue(all('context_json' not in a for a in context['attempts']))
        self.assertLess(sizes[-1],sizes[0]+5000)
        self.assertTrue(all(len(t['spec'])<4000 for t in self.native.tasks))

    def test_stopped_owner_cleanup_is_replayable_before_launch(self):
        self.setup_native();self.native.pending_release=True;self.switch();self.bridge.tick()
        self.assertEqual(self.native.launches,1)
        self.assertEqual(self.current()['state'],'UNKNOWN')
        self.native.pending_release=False;self.queue.recover(self.row['id']);self.bridge.tick()
        self.assertEqual(self.native.launches,2)
        releases=[c for c in self.native.calls if c[1]=='worker-release']
        self.assertEqual(releases[0],releases[1])
        self.assertEqual(self.native.workers[0]['terminalState'],'released')
        self.assertEqual(self.native.contexts_seen[-1]['old_terminal_cleanup']['state'],'released')


class NaturalTests(BotFixture):
    def queue(self):
        return OrcaQueue.load(self.bot.store,self.settings.root)

    def test_required_korean_requests_do_not_call_chat(self):
        policy=self.bot.policy
        for text,target in [('홈페이지 작업 코덱스로 이어줘','코덱스'),('클로드 한도 찼으니 코덱스로 넘겨줘','코덱스'),
            ('이 작업은 클로드로 바꿔서 계속해','클로드'),('이 작업 아스트라로 이어줘','아스트라')]:
            self.assertEqual(policy.orca_intent(text).argument,target)
        self.assertEqual(policy.orca_intent('지금 누가 작업 중이야').intent,'orca_status')

    def test_natural_switch_durable_and_authorized(self):
        q=self.queue();row=q.submit('x','trial','codex','report',self.settings.allowed_chat_id)
        with patch.object(self.bot.chat,'respond',side_effect=AssertionError('LLM not allowed')):
            self.bot._handle_update(self.message('이 작업은 클로드로 바꿔서 계속해',9001))
        changes=Handoff(q).changes(row['id'])
        self.assertEqual(len(changes),1)
        self.assertEqual(changes[0]['agent'],'claude')
        self.bot._orca_natural('이 작업은 클로드로 바꿔서 계속해',9001)
        self.assertEqual(len(Handoff(q).changes(row['id'])),1)
        msg=self.message('이 작업은 아스트라로 이어줘',9002);msg['message']['from']['id']=999999
        self.bot._handle_update(msg)
        self.assertEqual(len(Handoff(q).changes(row['id'])),1)

    def test_ambiguous_project_or_task_has_no_action(self):
        q=self.queue();q.submit('x','trial','codex','one');q.submit('y','trial','claude','two')
        self.bot._handle_update(self.message('클로드 한도 찼으니 코덱스로 넘겨줘',9001))
        self.assertTrue(all(not Handoff(q).changes(r['id']) for r in q.rows()))
        self.assertIsNone(self.bot.policy.orca_intent('클로드 한도 찼어'))
        self.assertEqual(self.bot.policy.orca_intent('이 작업 코덱스로 넘겨줘 하지 마').intent,'orca_clarify')

    def test_unique_question_reply_is_durable_not_global_action(self):
        q=self.queue();row=q.submit('x','trial','codex','one',self.settings.allowed_chat_id);q.update(row['id'],state='ATTENTION')
        with self.bot.store.connect() as db: db.execute("INSERT INTO orca_questions(message_id,request_id,body) VALUES('q1',?,'choose')",(row['id'],))
        self.bot._handle_update(self.message('첫 번째로 진행해',9001))
        with self.bot.store.connect() as db:
            answer=db.execute("SELECT * FROM orca_questions").fetchone()
        self.assertEqual(answer['state'],'ANSWERING')
        self.assertEqual(answer['reply_text'],'첫 번째로 진행해')
        self.bot._orca_natural('첫 번째로 진행해',9001)

    def test_two_questions_require_task_selection(self):
        q=self.queue()
        for n in (1,2):
            row=q.submit(str(n),'trial','codex','one',self.settings.allowed_chat_id);q.update(row['id'],state='ATTENTION')
            with self.bot.store.connect() as db: db.execute('INSERT INTO orca_questions(message_id,request_id,body) VALUES(?,?,?)',(str(n),row['id'],'choose'))
        self.bot._handle_update(self.message('그래 진행해',9001))
        with self.bot.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM orca_questions WHERE state='ANSWERING'").fetchone()[0],0)

    def test_homepage_import_is_context_only(self):
        self.bot._handle_update(self.message('홈페이지 작업 코덱스로 이어줘',9001))
        row=self.queue().rows()[0]
        self.assertEqual((row['project'],row['imported'],row['state']),('ionenet-web',1,'IMPORTED'))

    def test_fixed_reads_and_unrelated_chat_are_not_question_answers(self):
        q=self.queue();row=q.submit('x','trial','codex','one',self.settings.allowed_chat_id);q.update(row['id'],state='ATTENTION')
        with self.bot.store.connect() as db: db.execute("INSERT INTO orca_questions(message_id,request_id,body) VALUES('q1',?,'choose')",(row['id'],))
        with patch.object(self.bot,'_execute_decision') as execute:
            self.bot._handle_update(self.message('상태',9001))
            self.assertEqual(execute.call_args.args[0].intent,'status')
        self.bot._handle_update(self.message('오늘 날씨 어때?',9002))
        with self.bot.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM orca_questions').fetchone()[0],'PENDING')

    def test_natural_pipeline_reaches_native_completion_outbox(self):
        q=self.queue();native=Native();q.config['hub_root']=str(self.bot.store.db_path.parent/'fake-hub')
        q.config['projects']['trial']['path']='/trial';q.config['run_id']='run_1';q.config['coordinator']='term_coord'
        bridge=OrcaBridge(q,native)
        row=q.submit('pipeline','trial','codex','read report',self.settings.allowed_chat_id)
        bridge.tick()
        with patch('app.orca_bridge.OrcaQueue.load',return_value=q):
            self.bot._handle_update(self.message('이 작업 클로드로 이어줘',9001))
        bridge.tick();native.complete(index=1);bridge.tick()
        self.assertEqual(q.rows()[0]['state'],'SUCCEEDED')
        self.assertEqual(native.launches,2)
        self.assertTrue(any('SUCCEEDED' in o['dedupe_key'] for o in self.bot.store.pending_outbox()))

    def test_source_exclusion_and_secondary_constraint_allow_clear_switch(self):
        for text in ('클로드 말고 코덱스로 이어줘','이 작업 아스트라로 이어줘 기존 질문 반복하지 말고 검사해'):
            self.assertEqual(self.bot.policy.orca_intent(text).intent,'orca_switch')
        for text in ('이 작업 코덱스로 넘기지 마','이 작업 코덱스로 전환 취소','이 작업 코덱스로 넘겨줘 하지 마'):
            self.assertEqual(self.bot.policy.orca_intent(text).intent,'orca_clarify')
        q=self.queue();row=q.submit('x','trial','claude','one',self.settings.allowed_chat_id)
        self.bot._handle_update(self.message('클로드 말고 코덱스로 이어줘',9001))
        self.assertEqual(Handoff(q).changes(row['id'])[0]['agent'],'codex')

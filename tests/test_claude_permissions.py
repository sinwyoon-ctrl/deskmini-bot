import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from test_watch import BotFixture
from app.claude_permissions import enqueue, install, process_identity, register, start_runtime, heartbeat, run_hook, live_worker
from app.orca_bridge import OrcaQueue


class ClaudePermissionsTests(BotFixture):
    def setUp(self):
        super().setUp()
        self.workspace=self.home/'claude-work';self.workspace.mkdir()
        self.queue=OrcaQueue(self.bot.store,{'enabled':True,'projects':{'proof':{'path':str(self.workspace)}}})
        self.row=self.queue.submit('proof:1','proof','claude','isolated permission proof',100)
        self.queue.update(self.row['id'],state='RUNNING',dispatch_id='dispatch-1',task_id='task-1',run_id='run-1')
        self.row=self.queue.rows()[0]
        self.pid=os.getpid();self.started=process_identity(self.pid)
        self.payload={'hook_event_name':'PermissionRequest','permission_mode':'default','session_id':'session-1',
            'cwd':str(self.workspace),'tool_name':'Bash','tool_input':{'command':'echo hello','description':'인사 출력'}}

    def request(self,payload=None,ttl=600):
        with self.bot.store.connect() as db:
            register(db,self.row['id'],self.workspace,payload or self.payload,self.pid,self.started)
            return enqueue(db,self.row,payload or self.payload,self.pid,self.started,ttl)

    def state(self,key):
        with self.bot.store.connect() as db:
            return db.execute('select state from claude_permissions where id=?',(key,)).fetchone()[0]

    def test_auth_and_exact_one_time_callback(self):
        key=self.request()
        self.press('clp:allow:'+key,1,user_id=999)
        self.assertEqual(self.state(key),'PENDING')
        self.press('clp:allow:'+key,2)
        self.assertEqual(self.state(key),'APPROVED')
        self.press('clp:deny:'+key,3)
        self.assertEqual(self.state(key),'APPROVED')

    def test_denial_is_terminal(self):
        key=self.request();self.press('clp:deny:'+key,1);self.press('clp:allow:'+key,2)
        self.assertEqual(self.state(key),'DENIED')

    def test_expired_and_restart_do_not_allow(self):
        expired=self.request(ttl=-1);self.press('clp:allow:'+expired,1)
        self.assertNotEqual(self.state(expired),'APPROVED')
        current=self.request();start_runtime(self.bot.store);self.press('clp:allow:'+current,2)
        self.assertEqual(self.state(current),'EXPIRED')

    def test_dead_process_and_replaced_dispatch_rejected(self):
        key=self.request()
        with self.bot.store.connect() as db:
            db.execute('update claude_permissions set hook_start=? where id=?',('different',key))
        self.press('clp:allow:'+key,1);self.assertEqual(self.state(key),'PENDING')
        second=self.request();self.queue.update(self.row['id'],dispatch_id='replacement')
        self.press('clp:allow:'+second,2);self.assertEqual(self.state(second),'PENDING')

    def test_session_cannot_be_taken_over_by_other_process_or_session(self):
        self.request()
        for change in ({'session_id':'foreign'},{'cwd':str(self.home)}):
            with self.bot.store.connect() as db,self.assertRaises(ValueError):
                register(db,self.row['id'],self.workspace,self.payload|change,self.pid,self.started)
        with self.bot.store.connect() as db,self.assertRaises(ValueError):
            register(db,self.row['id'],self.workspace,self.payload,self.pid+1,self.started)

    def test_redacted_or_long_input_has_no_allow_button(self):
        for command in ('echo password=private-value','x'*2600):
            payload=self.payload|{'tool_input':{'command':command}}
            key=self.request(payload)
            self.press('clp:allow:'+key,100+len(command))
            self.assertEqual(self.state(key),'PENDING')
            with self.bot.store.connect() as db:
                outgoing=db.execute('select payload_json from outbox where dedupe_key=?',('clp:'+key,)).fetchone()[0]
            self.assertNotIn('clp:allow:',outgoing)
            self.assertNotIn('private-value',outgoing)

    def test_requests_are_distinct_even_for_same_tool_input(self):
        first,second=self.request(),self.request()
        self.assertNotEqual(first,second)
        self.press('clp:allow:'+first,1)
        self.assertEqual(self.state(second),'PENDING')

    def test_no_live_bot_no_request(self):
        with self.bot.store.connect() as db:db.execute('update claude_permission_runtime set heartbeat=0')
        with self.assertRaises(ValueError):self.request()

    def test_opt_in_only_and_preserve_user_settings(self):
        project={'path':str(self.workspace)}
        install(self.bot.store,project,self.row['id'],Path.cwd())
        path=self.workspace/'.claude/settings.json';self.assertFalse(path.exists())
        project['claude_permissions']=True
        install(self.bot.store,project,self.row['id'],Path.cwd())
        settings=json.loads(path.read_text())
        self.assertEqual(settings['permissions']['defaultMode'],'default')
        self.assertNotIn('settings.local.json',path.read_text())
        install(self.bot.store,project,self.row['id'],Path.cwd())
        path.write_text('{"userSetting":true}')
        with self.assertRaises(ValueError):install(self.bot.store,project,self.row['id'],Path.cwd())
        self.assertEqual(path.read_text(),'{"userSetting":true}')

    def test_symlink_settings_rejected(self):
        (self.workspace/'.claude').symlink_to(self.home,target_is_directory=True)
        with self.assertRaises(ValueError):install(self.bot.store,{'path':str(self.workspace),'claude_permissions':True},self.row['id'],Path.cwd())

    def test_external_claude_cannot_claim_first_session(self):
        with patch('app.claude_permissions.claude_process',return_value=(self.pid,self.started)):
            for event in ('SessionStart','PermissionRequest'):
                with self.assertRaises(ValueError):
                    run_hook(self.bot.store.db_path,self.row['id'],self.workspace,self.payload|{'hook_event_name':event},Path.cwd())
        with self.bot.store.connect() as db:
            self.assertEqual(db.execute('select count(*) from claude_permission_sessions').fetchone()[0],0)
            self.assertEqual(db.execute('select count(*) from claude_permissions').fetchone()[0],0)

    def test_different_native_terminal_is_rejected(self):
        config={'projects':{'proof':{'path':str(self.workspace),'claude_permissions':True,'worktree':'id:repo::'+str(self.workspace)}}}
        worker={'dispatchId':'dispatch-1','taskId':'task-1','runId':'run-1','agentTerminalHandle':'expected-terminal',
                'resource':{'worktreeId':'repo::'+str(self.workspace),'ownershipState':'owned'},'projection':{'liveness':{'verdict':'live'}}}
        with self.bot.store.connect() as db,patch('app.orca_bridge.OrcaQueue.load',return_value=OrcaQueue(None,config)),\
             patch('app.orca_bridge.NativeOrca.call',return_value={'workers':[worker]}),\
             patch('app.claude_permissions.native_identity',return_value=('foreign-terminal','repo::'+str(self.workspace))):
            with self.assertRaises(ValueError):live_worker(db,self.row,self.workspace,Path.cwd(),self.pid,time.monotonic()+20)

    def test_worker_pagination_obeys_whole_hook_deadline(self):
        config={'projects':{'proof':{'path':str(self.workspace),'claude_permissions':True}}}
        clock=[0.0];calls=[]
        def page(*args,timeout):
            calls.append(timeout);clock[0]+=5
            return {'workers':[],'page':{'hasMore':True,'nextCursor':'next'}}
        with self.bot.store.connect() as db,patch('app.orca_bridge.OrcaQueue.load',return_value=OrcaQueue(None,config)),\
             patch('app.orca_bridge.NativeOrca.call',side_effect=page),patch('app.claude_permissions.time.monotonic',side_effect=lambda:clock[0]):
            with self.assertRaises(ValueError):live_worker(db,self.row,self.workspace,Path.cwd(),self.pid,10)
        self.assertEqual(len(calls),2)
        self.assertLessEqual(clock[0],10)

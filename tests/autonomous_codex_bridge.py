"""Real Codex scenarios with an isolated DB/workspace and a simulated Telegram user."""
import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_watch import BotFixture
from app.bot import ChiefBot
from app.codex_bridge import Bridge, server_argv
from app.codex_session import CodexSessions
from app.redact import redact_text
from websockets.asyncio.client import unix_connect


class Scenarios:
    def __init__(self, report):
        self.report = report
        self.results = []
        self.events = []
        self.response_frames = Counter()
        self.number = 30000
        self.phase = 'normal'
        self.proc = self.ws = self.connected = None
        self.fixture = BotFixture()
        self.fixture.setUp()
        self.bot = self.fixture.bot
        self.progress = report.with_suffix('.jsonl')
        self.progress.parent.mkdir(parents=True,exist_ok=True)
        self.progress.touch(exist_ok=False)

    def record(self, name, **evidence):
        item = {'case':name,'status':'PASS','at':datetime.now(UTC).isoformat(timespec='seconds'),**evidence}
        self.results.append(item)
        with self.progress.open('a') as out:
            out.write(json.dumps(item,ensure_ascii=False)+'\n')
        print(json.dumps(item,ensure_ascii=False),flush=True)

    def rows(self, sql, *args):
        with self.bot.store.connect() as db:
            return [dict(r) for r in db.execute(sql,args)]

    def session(self, key):
        return self.rows('SELECT * FROM codex_sessions WHERE id=?',key)[0]

    def send(self, text, reply=None):
        self.number += 1
        update = self.fixture.message(text,self.number)
        if reply:
            update['message']['reply_to_message'] = {'text':'확인 요청 '+reply}
        self.bot._handle_update(update)
        return self.number

    def press(self, key, answer, authorized=True):
        self.number += 1
        self.fixture.press('cx:'+key+':'+answer,self.number,user_id=100 if authorized else 101)

    async def until(self, predicate, timeout=100):
        end = time.monotonic()+timeout
        while time.monotonic()<end:
            value = predicate()
            if value:
                return value
            if self.connected and self.connected.done():
                await self.connected
                raise RuntimeError('bridge stopped')
            await asyncio.sleep(.15)
        raise TimeoutError('scenario wait timed out')

    async def done(self, key, after, source=None):
        def ready():
            s = self.session(key)
            completed = any(i>after and event.get('thread')==s['thread_id'] and event.get('method')=='turn/completed' for i,event in enumerate(self.events))
            if s['state']=='FAILED':
                raise RuntimeError('Codex turn failed')
            ack = not source or self.rows('SELECT state FROM codex_commands WHERE source=?',source)[0]['state']=='ACKED'
            return s if completed and ack and s['state']=='IDLE' else None
        return await self.until(ready)

    async def question(self, key):
        return (await self.until(lambda:self.api.pending(key)))[0]

    async def start(self):
        self.socket.unlink(missing_ok=True)
        self.proc = subprocess.Popen(server_argv(self.config['cli'],self.socket),cwd=self.workspace,
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        await self.until(lambda:self.socket.exists(),12)
        bridge = Bridge(self.bot.store,self.config,self.socket)
        original_rpc, original_event = bridge.rpc, bridge.event
        async def rpc(method, params):
            if method=='turn/start' and self.phase in {'approval','denial'}:
                params = dict(params,approvalPolicy='untrusted')
            return await original_rpc(method,params)
        async def event(data):
            p = data.get('params') or {}
            self.events.append({'method':data['method'],'thread':p.get('threadId'),'turn':(p.get('turn') or {}).get('id')})
            await original_event(data)
        bridge.rpc, bridge.event = rpc, event
        self.ws = await unix_connect(str(self.socket),open_timeout=10,max_size=8*1024*1024)
        original_send = self.ws.send
        async def send_frame(message, *args, **kwargs):
            if isinstance(message,str):
                data = json.loads(message)
                if 'id' in data and 'result' in data and 'method' not in data:
                    self.response_frames[(bridge.epoch,json.dumps(data['id']))] += 1
            return await original_send(message,*args,**kwargs)
        self.ws.send = send_frame
        self.connected = asyncio.create_task(bridge.connected(self.ws))
        await self.until(lambda:self.rows("SELECT 1 FROM codex_runtime WHERE epoch=? AND state='READY'",bridge.epoch),12)
        self.bridge = bridge

    async def stop(self):
        if self.connected:
            self.connected.cancel()
            with contextlib.suppress(asyncio.CancelledError,Exception):
                await self.connected
            self.connected = None
        if self.ws:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.ws.close(),2)
            self.ws = None
        if self.proc:
            # Only the process group created by this test is signalled.
            group = self.proc.pid
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group,signal.SIGTERM)
            end = time.monotonic()+3
            while self.proc.poll() is None and time.monotonic()<end:
                await asyncio.sleep(.1)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group,signal.SIGKILL)
            await asyncio.to_thread(self.proc.wait,5)
            self.proc = None

    def hash_files(self):
        return {name:hashlib.sha256((self.workspace/name).read_bytes()).hexdigest()
                for name in ('README.md','totals.py','test_totals.py','approved.txt')}

    async def scenarios(self):
        before = len(self.events)-1
        test_hash = hashlib.sha256((self.workspace/'test_totals.py').read_bytes()).hexdigest()
        self.send('코덱스로 연결시험 README를 읽고 totals.py의 합계 버그를 고쳐줘. test_totals.py는 고치지 말고 테스트를 실행해 확인해. 허용 범위는 이 폴더뿐이야. hub·외부 파일·네트워크·다른 에이전트는 사용하지 마.')
        key = self.api.focused(100)['id']
        self.key = key
        first = await self.done(key,before)
        checked = await asyncio.to_thread(subprocess.run,[sys.executable,'-m','unittest','-v','test_totals'],cwd=self.workspace,capture_output=True,text=True)
        assert checked.returncode==0,checked.stdout+checked.stderr
        assert hashlib.sha256((self.workspace/'test_totals.py').read_bytes()).hexdigest()==test_hash
        verifier = ('import importlib.util,sys; '
            's=importlib.util.spec_from_file_location("subject",sys.argv[1]); '
            'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
            'assert [m.total(x) for x in ([2,3,5],[],[-2,3])] == [10,0,1]; print("3 independent checks passed")')
        independent = await asyncio.to_thread(subprocess.run,[sys.executable,'-I','-c',verifier,str(self.workspace/'totals.py')],
            cwd=self.fixture.home,capture_output=True,text=True)
        assert independent.returncode==0,independent.stderr
        assert 'return len(items)' not in (self.workspace/'totals.py').read_text()
        self.record('natural_code_work',tests='3 passed',original_tests_unchanged=True,independent_checks=3,thread=first['thread_id'])

        before = len(self.events)-1
        update_id = self.send('앞서 README에서 읽은 작업번호만 한 줄로 답해. 도구를 실행하거나 파일을 수정하지 마.')
        followup = await self.done(key,before,'telegram:'+str(update_id))
        assert self.ticket in followup['last_text'],followup['last_text']
        assert followup['thread_id']==first['thread_id']
        self.record('same_thread_followup',thread_unchanged=followup['thread_id']==first['thread_id'])
        count = len(self.rows('SELECT id FROM codex_commands'))
        self.bot._handle_update(self.fixture.message('앞서 README에서 읽은 작업번호만 한 줄로 답해. 도구를 실행하거나 파일을 수정하지 마.',update_id))
        assert len(self.rows('SELECT id FROM codex_commands'))==count
        self.record('duplicate_telegram_update',extra_commands=0)

        fresh = ChiefBot(self.fixture.settings)
        fresh.executor.shutdown(wait=False)
        fresh.telegram = self.fixture.tg
        fresh.paths = self.bot.paths
        self.bot = self.fixture.bot = fresh
        assert self.api.focused(100)['id']==key
        before = len(self.events)-1
        self.send('같은 대화가 유지되는지 확인해줘. 기억하는 작업번호만 답하고 도구는 사용하지 마.')
        response = await self.done(key,before)
        assert self.ticket in response['last_text']
        self.record('bot_object_restart',persistent_focus=True,context_preserved=True)

        before = len(self.events)-1
        self.send('blocking request_user_input으로 이름을 골라 달라고 질문해. 선택지는 alpha와 beta야. 아직 파일은 만들지 마. 답을 받으면 선택한 이름만 답하고 끝내.')
        q = await self.question(key)
        assert q['method']=='item/tool/requestUserInput'
        q_turn = q['turn_id']
        self.press(q['id'],'1',authorized=False)
        assert self.rows('SELECT state FROM codex_questions WHERE id=?',q['id'])[0]['state']=='PENDING'
        self.record('unauthorized_callback',approval_not_consumed=True)
        self.press(q['id'],'1')
        self.press(q['id'],'1')
        choice = await self.done(key,before)
        q_state = self.rows('SELECT state,response FROM codex_questions WHERE id=?',q['id'])[0]
        assert q_state['state']=='RESOLVED'
        assert 'alpha' in choice['last_text'].lower()
        same_turn = any(e['method']=='turn/completed' and e['turn']==q_turn for e in self.events)
        responses = self.response_frames[(q['epoch'],q['request_id'])]
        assert same_turn and responses==1
        self.record('choice_and_duplicate_click',question_turn=q_turn,same_turn_completed=same_turn,responses_measured=responses)

        before = len(self.events)-1
        self.send('blocking request_user_input으로 시험 이름을 alpha 또는 beta 중 고르거나 다른 이름을 입력하도록 물어봐. 파일을 만들지 말고 답을 받은 뒤 그 이름만 답해.')
        q = await self.question(key)
        self.send('gamma',reply=q['id'])
        freeform = await self.done(key,before)
        assert 'gamma' in freeform['last_text'].lower()
        assert freeform['thread_id']==first['thread_id']
        self.record('freeform_reply_to_question',answer='gamma',same_thread=freeform['thread_id']==first['thread_id'])

        self.phase = 'approval'
        before = len(self.events)-1
        self.send('exec_command의 python3 -c 명령 하나로 이 폴더의 approved.txt에 APPROVED_ONCE를 쓰고 읽어 출력해. apply_patch는 쓰지 마. 먼저 실행 승인을 받고 거절되면 우회나 재시도 없이 끝내. hub나 외부 파일을 사용하지 마.')
        q = await self.question(key)
        assert q['method']=='item/commandExecution/requestApproval'
        assert 'approved.txt' in json.loads(q['params']).get('command','')
        assert not (self.workspace/'approved.txt').exists()
        self.press(q['id'],'accept')
        await self.done(key,before)
        assert (self.workspace/'approved.txt').read_text().strip()=='APPROVED_ONCE'
        self.record('command_approval',absent_before_approval=True,present_after_approval=True)
        approved_mtime = (self.workspace/'approved.txt').stat().st_mtime_ns

        self.phase = 'denial'
        before = len(self.events)-1
        self.send('exec_command로 python3 -c를 실행해 denied.txt에 DENIED를 쓰려 해. 반드시 먼저 승인을 요청하고 거절되면 즉시 멈춰. 다른 방법으로 만들거나 재시도하지 마.')
        q = await self.question(key)
        assert q['method']=='item/commandExecution/requestApproval'
        decisions = json.loads(q['params']).get('availableDecisions') or ['decline']
        self.press(q['id'],'decline' if 'decline' in decisions else 'cancel')
        await self.done(key,before)
        assert not (self.workspace/'denied.txt').exists()
        self.record('command_denial',denied_file_absent=True)
        self.phase = 'normal'

        before = len(self.events)-1
        self.send('이 대화가 기억하는 README의 작업번호만 답해. 도구는 쓰지 마.')
        second_tag = 'SECOND-'+secrets.token_hex(3)
        self.send('/codex new trial 이 대화의 표지는 '+second_tag+'야. 도구 없이 그 표지만 답해.')
        second = self.api.focused(100)['id']
        a,b = await asyncio.gather(self.done(key,before),self.done(second,before))
        assert self.ticket in a['last_text'] and second_tag in b['last_text']
        assert a['thread_id']!=b['thread_id']
        self.record('two_sessions_and_focus',queued_message_target_preserved=True,separate_threads=True)
        self.send('/codex use '+key)

        self.send('blocking request_user_input으로 재시작 전 마지막 선택을 물어봐. 선택지는 alpha와 beta야. 파일은 건드리지 말고 답변을 기다려.')
        stale = await self.question(key)
        fingerprint = self.hash_files()
        acknowledged = [r['id'] for r in self.rows("SELECT id FROM codex_commands WHERE state='ACKED'")]
        await self.stop()
        await self.start()
        assert self.session(key)['state']=='UNKNOWN'
        old_count = len(self.rows('SELECT id FROM codex_commands'))
        self.send('끊긴 상태에서 이 메시지를 실행하면 안 돼. 작업번호만 답해.')
        assert len(self.rows('SELECT id FROM codex_commands'))==old_count
        self.record('backend_restart_guard',unknown_state=True,unrequested_execution=0)
        self.press(stale['id'],'1')
        assert self.rows('SELECT state FROM codex_questions WHERE id=?',stale['id'])[0]['state']=='CLOSED'
        self.record('stale_click_after_restart',not_reused=True)
        self.send('/codex recover '+key)
        await self.until(lambda:self.session(key)['state']=='IDLE')
        assert self.session(key)['thread_id']==first['thread_id']
        before = len(self.events)-1
        self.send('앞 대화에서 README에서 확인한 작업번호만 답해. 도구를 실행하거나 파일을 수정하지 마.')
        recovered = await self.done(key,before)
        assert self.ticket in recovered['last_text']
        assert self.hash_files()==fingerprint
        assert (self.workspace/'approved.txt').stat().st_mtime_ns==approved_mtime
        assert all(self.rows('SELECT state FROM codex_commands WHERE id=?',i)[0]['state']=='ACKED' for i in acknowledged)
        self.record('explicit_recovery',same_thread=True,context_preserved=True,files_unchanged=True,
            approved_file_not_rewritten=True,previous_command_records_unchanged=True)
        self.bot._flush_outbox()
        assert not self.bot.store.pending_outbox()
        self.record('outbox_delivery_simulation',messages=len(self.fixture.tg.sent),telegram_network_calls=0)

    async def run(self):
        error = None
        try:
            with tempfile.TemporaryDirectory(prefix='telegram-autonomous-',dir='/home/ionenet/work') as directory:
                self.workspace = Path(directory)
                self.socket = self.workspace/'control.sock'
                self.ticket = 'AUTO-'+secrets.token_hex(4)
                (self.workspace/'README.md').write_text('작업번호: '+self.ticket+'\n숫자 목록의 합계를 구하는 totals.total(items)의 버그를 수정하는 시험입니다.\n')
                (self.workspace/'AGENTS.md').write_text('Temporary protocol test. Work only here. Do not run hub, access credentials, use the network, install software, create agents, commit or push.\n')
                (self.workspace/'totals.py').write_text('def total(items):\n    return len(items)\n')
                (self.workspace/'test_totals.py').write_text('import unittest\nfrom totals import total\nclass Totals(unittest.TestCase):\n    def test_values(self): self.assertEqual(total([2,3,5]),10)\n    def test_empty(self): self.assertEqual(total([]),0)\n    def test_negative(self): self.assertEqual(total([-2,3]),1)\n')
                subprocess.run(['git','init','-q',directory],check=True)
                self.config = {'enabled':True,'cli':'/home/ionenet/.local/bin/codex','projects':{'trial':{'path':directory,'aliases':['연결시험'],'scope':'Isolated test workspace only. No hub, credentials, external paths, network, services, install, agents, commit or push. Use Korean.'}}}
                self.api = CodexSessions(self.bot.store,self.config)
                try:
                    with patch('app.codex_session.load_config',return_value=self.config):
                        await self.start()
                        await self.scenarios()
                finally:
                    await self.stop()
        except Exception as exc:
            error = {'type':type(exc).__name__,'message':redact_text(str(exc),1000)}
            print(json.dumps({'status':'FAIL','error':error},ensure_ascii=False),flush=True)
        finally:
            self.fixture.doCleanups()
            self.fixture.tearDown()
            result = {'status':'FAIL' if error else 'PASS','actual_codex':True,'telegram_transport':'FakeTelegram',
                'production_services_changed':False,'production_db_changed':False,'cases':self.results,'error':error}
            with self.report.open('x') as out:
                json.dump(result,out,ensure_ascii=False,indent=2)
        if error:
            raise SystemExit(1)


if __name__=='__main__':
    if os.environ.get('LIVE_CODEX_AUTONOMOUS')!='1':
        raise SystemExit('Set LIVE_CODEX_AUTONOMOUS=1; uses real Codex with an isolated workspace/DB and FakeTelegram.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--report',type=Path,required=True)
    args = parser.parse_args()
    asyncio.run(Scenarios(args.report).run())

"""Opt-in real Codex + native Opus conversation roundtrip, isolated state and FakeTelegram."""
import argparse,asyncio,json,os,secrets,subprocess,sys,tempfile,time
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
from autonomous_codex_bridge import Scenarios
from probe_claude_permissions import call,reserve_report,released_worker,finish_fixture
from app.codex_session import CodexSessions
from app.orca_bridge import OrcaQueue,OrcaBridge
from app.claude_permissions import heartbeat


class Conversation(Scenarios):
    def __init__(self,report):
        self.resources=reserve_report(report)
        super().__init__(report)
        self.number=int(time.time()*1000)
        self.resource={'fixture_home':str(self.fixture.home),'stage':'reserved'}
        self.approvals=[]

    def save(self):self.resources.write_text(json.dumps(self.resource,ensure_ascii=False,indent=2))

    def send(self,text,reply=None):
        self.number+=1;update=self.fixture.message(text,self.number)
        if reply:update['message']['reply_to_message']={'text':reply}
        self.bot._handle_update(update)
        return self.number

    async def native_tick(self):
        heartbeat(self.bot.store,self.bot.claude_permission_epoch)
        pending=self.rows("SELECT * FROM claude_permissions WHERE state='PENDING'")
        for item in pending:
            assert item['can_allow'],'Incomplete permission display: '+item['display']
            content=json.loads(item['display'])
            assert content['cwd']==str(self.native_root)
            if content['tool_name'] in {'Write','Edit'}:
                assert Path(content['tool_input']['file_path']).resolve()==self.native_root/'ROUTING_PROBE.json'
            self.number+=1;self.fixture.press('clp:allow:'+item['id'],self.number)
            self.approvals.append({'id':item['id'],'display':item['display']})
        await asyncio.to_thread(self.native.reconcile)
        self.bot._flush_outbox()

    async def until_native(self,predicate,timeout=240):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            await self.native_tick()
            value=predicate()
            if value:return value
            await asyncio.sleep(.3)
        raise TimeoutError('Native conversation wait timed out')

    async def acceptance(self):
        before=len(self.events)-1
        self.send('코덱스야, 연결시험 README를 읽고 작업번호를 그대로 답해줘. 도구는 이 파일 읽기만 허용하고 파일은 수정하지 마. 외부·네트워크·다른 에이전트는 사용하지 마.')
        key=self.api.focused(100)['id']
        first=await self.done(key,before)
        assert self.ticket in first['last_text']
        self.bot._flush_outbox()
        self.record('codex_named_call',actual_model=True,thread=first['thread_id'],ticket=self.ticket)
        request=('클로드야, 연결시험에서 다음 시험을 해줘. 지금까지 텔레그램 대화에서 코덱스가 README를 읽고 보고한 작업번호를 찾아 기억해. '
            '아직 결과 파일을 만들지 말고 preamble의 blocking coordinator ask로 alpha 또는 beta 중 이름을 질문해. '
            '답변을 받으면 반드시 별도 명령으로 coordinator inbox를 확인하고 모든 추가 지시를 읽어 반영한 뒤 파일을 만들어. '
            'ROUTING_PROBE.json 하나에 codex_ticket(앞선 코덱스 답변의 작업번호), chosen(답변의 이름), rendered(답변과 추가 지시를 적용한 이름), '
            'review_marker(Python secrets.token_hex(8)로 직접 생성한 새 값)을 저장하고 Python으로 읽어 검증해. '
            'worker_done 결과 본문에 네 필드의 실제 값을 모두 포함해. '
            'AGENTS/HANDOFF는 있으면 읽되 hub나 외부 경로·비밀값·설치·서비스·다른 에이전트·commit/push는 금지. 한국어로 보고해.')
        self.send(request)
        row=self.queue.rows()[0]
        assert row['agent']=='claude' and row['model']=='claude-opus-5-5'
        await asyncio.to_thread(self.native.start,row)
        row=self.queue.rows()[0];assert row['dispatch_id'] and row['state']=='RUNNING',row.get('error')
        self.resource.update(request_id=row['id'],dispatch_id=row['dispatch_id'],task_id=row['task_id']);self.save()
        question=await self.until_native(lambda:next(iter(self.rows("SELECT * FROM orca_questions WHERE state='PENDING'")),None))
        assert not (self.native_root/'ROUTING_PROBE.json').exists()
        question_payload=next(r['text'] for r in reversed(self.fixture.tg.sent) if '질문 번호: '+question['message_id'] in r['text'])
        self.record('claude_named_call_and_question',actual_model=True,dispatch=row['dispatch_id'],file_absent_before_answer=True)
        before=len(self.events)-1
        self.send('코덱스야, 클로드가 지금 물은 질문에 대해 한 문장으로 의견만 말해줘. 도구를 사용하거나 파일을 만들지 마.')
        advice=await self.done(key,before)
        assert advice['thread_id']==first['thread_id']
        assert self.rows('SELECT state FROM orca_questions')[0]['state']=='PENDING'
        self.record('codex_reviews_claude_question',same_thread=True,claude_question_not_consumed=True,reply=advice['last_text'])
        self.send('alpha나 beta 대신 gamma로 해줘. 출력은 대문자 GAMMA로 해줘.',question_payload)
        assert self.rows('SELECT state FROM orca_questions')[0]['state']=='ANSWERING'
        assert self.rows('SELECT agent FROM telegram_agent_focus WHERE chat_id=100')[0]['agent']=='codex'
        self.send('클로드야, 추가 지시: rendered 맨 뒤에 -OK를 붙여줘. 이 추가 지시는 coordinator inbox에서 읽고 결과에 반영해.')
        followup=self.rows('SELECT * FROM orca_chat_commands')[0]
        assert followup['dispatch_id']==row['dispatch_id']
        await self.until_native(lambda:self.queue.rows()[0]['state'] in {'SUCCEEDED','FAILED'})
        row=self.queue.rows()[0];assert row['state']=='SUCCEEDED',row.get('error')
        data=json.loads((self.native_root/'ROUTING_PROBE.json').read_text())
        assert data['codex_ticket']==self.ticket,data
        assert data['chosen']=='gamma' and data['rendered']=='GAMMA-OK',data
        assert len(data['review_marker'])==16
        assert data['review_marker'] in row['summary'],row['summary']
        assert self.rows('SELECT state FROM orca_chat_commands')[0]['state']=='SENT'
        self.record('claude_reads_codex_and_applies_reply_and_followup',artifact=data,followup_same_dispatch=True)
        self.bot._flush_outbox()
        before=len(self.events)-1
        self.send('코덱스 너는 어떻게 생각해? 클로드가 방금 실제 결과에 보고한 review_marker과 codex_ticket을 대화에서 찾아 그대로 적고, 결과에 대한 의견을 한 문장으로 덧붙여줘. 도구는 쓰지 마.')
        reviewed=await self.done(key,before)
        assert data['review_marker'] in reviewed['last_text'] and self.ticket in reviewed['last_text'],reviewed['last_text']
        assert reviewed['thread_id']==first['thread_id']
        self.bot._flush_outbox()
        messages=[r['text'] for r in self.fixture.tg.sent]
        assert any(t.startswith('[코덱스]') for t in messages)
        assert any(t.startswith('[클로드]') for t in messages)
        assert any(t.startswith('[봇 안내 ·') for t in messages)
        self.record('codex_reviews_real_claude_result',actual_reply=reviewed['last_text'],unseen_claude_marker_retrieved=True,all_sender_tags_present=True)
        workers=call('orchestration','worker-list','--run',self.native_config['run_id'])['result']['workers']
        assert released_worker(workers,row['dispatch_id'])
        call('terminal','close','--terminal',self.native_config['coordinator'])
        self.resource.update(worker_released=True,coordinator_closed=True,stage='PASS');self.save()

    async def run(self):
        error=None;passed=False;temporary=None
        try:
            temporary=tempfile.TemporaryDirectory(prefix='agent-conversation-',dir='/home/ionenet/work')
            self.workspace=Path(temporary.name);self.socket=self.workspace/'control.sock';self.ticket='CROSS-'+secrets.token_hex(8)
            (self.workspace/'README.md').write_text('작업번호: '+self.ticket+'\n')
            (self.workspace/'AGENTS.md').write_text('Isolated read-only conversation test. No hub, secrets, external paths, network, agents, install, commits, push or services.\n')
            subprocess.run(['git','init','-q',str(self.workspace)],check=True)
            created=call('worktree','create','--repo','id:a26c7673-34a6-47d0-a50f-62182a3e9a48','--name',self.report.stem,'--no-parent','--setup','skip')
            w=created['result']['worktree'];self.native_root=Path(w['path'])
            self.resource.update(workspace=w['id'],workspace_path=w['path'],codex_workspace=str(self.workspace),runtime_id=created['_meta']['runtimeId']);self.save()
            t=call('terminal','create','--worktree','id:'+w['id'],'--title','Agent conversation test coordinator')['result']
            coordinator=(t.get('terminal') or {}).get('handle') if isinstance(t.get('terminal'),dict) else t.get('terminal')
            coordinator=coordinator or t.get('handle');assert coordinator
            self.resource['coordinator']=coordinator;self.save()
            r=call('orchestration','run-create','--objective','Real model attribution and shared Telegram context acceptance','--from',coordinator,cwd=str(self.native_root))['result']
            run=(r.get('run') or r).get('id') or r.get('runId');assert run
            self.resource['run_id']=run;self.save()
            self.native_config={'enabled':True,'cli':'/home/ionenet/.orca/cli/bin/orca','cwd':str(self.native_root),'runtime_id':self.resource['runtime_id'],
                'run_id':run,'coordinator':coordinator,'hub_root':str(self.fixture.home/'agent-hub'),'projects':{'proof':{'path':str(self.native_root),
                'worktree':'id:'+w['id'],'label':'연결시험','aliases':['연결시험'],'conversation_project':'conversation-proof','hub_project':'conversation-proof',
                'claude_permissions':True,'claude_model':'claude-opus-5-5','verify_workspace':True,'ownership_ready':True,
                'scope':'Read repository docs; create only ROUTING_PROBE.json. Native ask/check/worker_done allowed. No production, secrets, external paths, hub, install, services, agents, commit/push or deploy.'}}}
            self.config={'enabled':True,'cli':'/home/ionenet/.local/bin/codex','projects':{'proof':{'path':str(self.workspace),'label':'연결시험','aliases':['연결시험'],
                'conversation_project':'conversation-proof','scope':'Read only README and conversation data. No writes, external paths, network, agents, hub, credentials, install, services, commits or push. Korean responses.'}}}
            self.api=CodexSessions(self.bot.store,self.config)
            self.queue=OrcaQueue(self.bot.store,self.native_config);self.native=OrcaBridge(self.queue)
            (self.bot.store.db_path.parent/'claude-permission-orca.json').write_text(json.dumps(self.native_config))
            with patch('app.codex_session.load_config',return_value=self.config),patch('app.agent_routing.load_config',return_value=self.config),patch('app.orca_bridge.OrcaQueue.load',return_value=self.queue):
                await self.start();await self.acceptance()
            passed=True
        except Exception as exc:
            import traceback;traceback.print_exc()
            error={'type':type(exc).__name__,'message':str(exc)[:1000]}
            self.resource.update(stage='FAIL',error=error);self.save()
        finally:
            await self.stop()
            if temporary:
                if passed:temporary.cleanup()
                else:temporary._finalizer.detach()
            finish_fixture(self.fixture,passed)
            result={'status':'PASS' if passed else 'FAIL','actual_codex':True,'actual_claude':True,'telegram_transport':'FakeTelegram',
                'production_db_changed':False,'production_services_changed':False,'cases':self.results,'permissions':self.approvals,'error':error}
            with self.report.open('x') as out:json.dump(result,out,ensure_ascii=False,indent=2)
            print(json.dumps({k:v for k,v in result.items() if k!='permissions'},ensure_ascii=False),flush=True)
        if not passed:raise SystemExit(1)


if __name__=='__main__':
    if os.environ.get('LIVE_AGENT_CONVERSATION')!='1':raise SystemExit('Set LIVE_AGENT_CONVERSATION=1; real Codex + Opus, private fixture DB, no Telegram network calls')
    parser=argparse.ArgumentParser();parser.add_argument('--report',type=Path,required=True)
    asyncio.run(Conversation(parser.parse_args().report).run())

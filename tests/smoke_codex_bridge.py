"""Opt-in real Codex protocol test; Telegram is fake and the DB/workspace are isolated."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_watch import BotFixture
from app.codex_bridge import Bridge, server_argv
from app.codex_session import CodexSessions
from websockets.asyncio.client import unix_connect


async def until(predicate, timeout=150):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(.2)
    raise TimeoutError('smoke wait timed out')


async def main():
    if os.environ.get('LIVE_CODEX_SMOKE') != '1':
        raise SystemExit('Set LIVE_CODEX_SMOKE=1; uses the signed-in Codex account, never Telegram network.')
    fixture = BotFixture()
    fixture.setUp()
    proc = connected = None
    try:
        with tempfile.TemporaryDirectory(prefix='telegram-codex-smoke-',dir='/home/ionenet/work') as name:
            workspace = Path(name)
            (workspace/'README.md').write_text('Isolated Codex Telegram protocol smoke test.\n')
            (workspace/'AGENTS.md').write_text('This is a temporary protocol test. Do not run hub, create agents, change services or access credentials. Work only in this directory.\n')
            subprocess.run(['git','init','-q',name],check=True)
            config = {'enabled':True,'cli':'/home/ionenet/.local/bin/codex','projects':{'trial':{'path':name,'scope':'Isolated connection test. Work only here. Do not run hub or access credentials. No git commit/push. Use Korean.'}}}
            sock = name+'/s.sock'
            proc = subprocess.Popen(server_argv(config['cli'],sock),cwd=name,
                                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            await until(lambda:Path(sock).exists(),10)
            bridge = Bridge(fixture.bot.store,config,Path(sock))
            api = CodexSessions(fixture.bot.store,config)
            methods = []
            original_event = bridge.event
            async def event(data):
                methods.append(data['method'])
                await original_event(data)
            bridge.event = event
            original_rpc = bridge.rpc
            model = None
            phase = 'question'
            async def rpc(method,params):
                nonlocal model
                if method == 'turn/start' and phase == 'approval':
                    params = dict(params,approvalPolicy='untrusted',collaborationMode={'mode':'default','settings':{'model':model,'reasoning_effort':'low','developer_instructions':None}})
                result = await original_rpc(method,params)
                if method == 'thread/start':
                    model = result.get('model')
                return result
            bridge.rpc = rpc
            async with unix_connect(sock,open_timeout=10,max_size=8*1024*1024) as ws:
                connected = asyncio.create_task(bridge.connected(ws))
                await until(lambda:fixture.bot.store.connect().execute("SELECT count(*) FROM codex_runtime WHERE state='READY'").fetchone()[0],10)
                with patch('app.codex_session.load_config',return_value=config):
                    fixture.bot._handle_update(fixture.message('/codex new trial',9001))
                    key = api.focused(100)['id']
                    await until(lambda:api.focused(100)['state']=='IDLE')
                    thread = api.focused(100)['thread_id']
                    fixture.bot._handle_update(fixture.message('이번 통신 시험에서는 hub나 외부 파일에 접근하지 마. request_user_input 도구를 반드시 호출해서 저장할 이름을 alpha 또는 beta 중 고르게 질문해. 답을 받으면 선택한 이름만 말하고 끝내.',9002))
                    questions = await until(lambda:api.pending(key))
                    q = questions[0]
                    assert q['method']=='item/tool/requestUserInput',q['method']
                    turn = q['turn_id']
                    fixture.press('cx:'+q['id']+':1',9003)
                    await until(lambda:api.focused(100)['state']=='IDLE')
                    assert api.focused(100)['thread_id']==thread
                    with fixture.bot.store.connect() as db:
                        state = db.execute('SELECT state FROM codex_questions WHERE id=?',(q['id'],)).fetchone()[0]
                    assert state=='RESOLVED',state
                    print(json.dumps({'structured_question':'PASS','same_thread':True,'question_turn':turn,'final':api.focused(100)['last_text'][:160]},ensure_ascii=False),flush=True)
                    phase = 'approval'
                    fixture.bot._handle_update(fixture.message('이번 시험에서는 hub나 외부 파일에 접근하지 마. apply_patch 대신 exec_command 도구로 python3 -c 명령을 실행해 현재 디렉터리의 proof.txt에 TELEGRAM_CODEX_APPROVED를 쓰고 같은 명령 안에서 파일을 읽어 출력해. 반드시 실행 승인을 요청하고 승인받은 뒤 실행해. 승인이 거절되면 재시도하거나 다른 도구로 우회하지 말고 즉시 끝내.',9004))
                    questions = await until(lambda:api.pending(key))
                    q = questions[0]
                    assert q['method']=='item/commandExecution/requestApproval',q['method']
                    assert not (workspace/'proof.txt').exists()
                    fixture.press('cx:'+q['id']+':accept',9005)
                    async def finish_turn():
                        end = time.monotonic()+150
                        denied = 0
                        while time.monotonic()<end:
                            if api.focused(100)['state']=='IDLE':
                                return denied
                            for extra in api.pending(key):
                                if extra['method'] in {'item/commandExecution/requestApproval','item/fileChange/requestApproval','item/permissions/requestApproval'}:
                                    decisions = json.loads(extra['params']).get('availableDecisions') or ['decline']
                                    refusal = 'decline' if 'decline' in decisions else 'cancel'
                                    fixture.press('cx:'+extra['id']+':'+refusal,9100+denied)
                                    denied += 1
                            await asyncio.sleep(.2)
                        raise TimeoutError('turn did not finish')
                    denied = await finish_turn()
                    assert (workspace/'proof.txt').read_text().strip()=='TELEGRAM_CODEX_APPROVED'
                    fixture.bot._flush_outbox()
                    assert api.focused(100)['thread_id']==thread
                    print(json.dumps({'command_approval':'PASS','no_write_before_approval':True,'file_after_approval':True,
                        'same_thread':True,'telegram_network':False,'events':sorted(set(methods)),
                        'additional_requests_declined':denied,'fake_telegram_messages':len(fixture.tg.sent)},ensure_ascii=False),flush=True)
                    fixture.bot._handle_update(fixture.message('이번에는 exec_command로 python3 -c 명령을 써서 현재 디렉터리의 denied.txt에 DENIED라고 쓰려고 해. 반드시 먼저 실행 승인을 요청하고 거절되면 재시도하거나 apply_patch로 우회하지 말고 즉시 끝내. hub나 외부 파일에 접근하지 마.',9200))
                    q = (await until(lambda:api.pending(key)))[0]
                    assert not (workspace/'denied.txt').exists()
                    decisions = json.loads(q['params']).get('availableDecisions') or ['decline']
                    refusal = 'decline' if 'decline' in decisions else 'cancel'
                    fixture.press('cx:'+q['id']+':'+refusal,9201)
                    await finish_turn()
                    assert not (workspace/'denied.txt').exists()
                    print(json.dumps({'command_denial':'PASS','denied_file_absent':True,'same_thread':api.focused(100)['thread_id']==thread}),flush=True)
                connected.cancel()
                try:await connected
                except asyncio.CancelledError:pass
                connected = None
    finally:
        if connected:
            connected.cancel()
            try:await connected
            except (asyncio.CancelledError,Exception):pass
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        fixture.doCleanups()
        fixture.tearDown()


if __name__=='__main__':
    asyncio.run(main())

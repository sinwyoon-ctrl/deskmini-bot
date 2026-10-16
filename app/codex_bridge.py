from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.codex_session import CodexSessions, load_config
from app.config import Settings
from app.redact import redact, redact_text
from app.state import StateStore, utc_now


LOG = logging.getLogger('codex-bridge')
QUESTION_METHODS = {'item/tool/requestUserInput', 'item/commandExecution/requestApproval',
                    'item/fileChange/requestApproval', 'item/permissions/requestApproval'}
ASYNC_QUESTION = 'agentMessage/questions'
QUESTION_INSTRUCTIONS = ('\nUse the blocking request_user_input tool when a user choice is required before continuing. '
    'Do not use request_user_input_async for a decision that must stop the work. '
    'If a question tool fails, ask in plain text and stop; never claim buttons were sent. '
    'Do not ask for secrets over Telegram.')


def server_argv(cli, socket_path):
    return [cli, 'app-server', '--enable', 'default_mode_request_user_input', '--listen', 'unix://'+str(socket_path)]


class RpcError(RuntimeError):
    pass


class Bridge:
    def __init__(self, store, config, socket_path):
        self.store, self.config, self.socket_path = store, config, socket_path
        self.sessions = CodexSessions(store, config)
        self.epoch = secrets.token_hex(8)
        self.ws = None
        self.waiters = {}
        self.sequence = 0
        self.last_progress = {}
        self.reader = None
        self.file_changes = {}

    def update(self, key, **fields):
        if set(fields) - {'thread_id','epoch','state','turn_id','last_text','observed_at'}:
            raise ValueError('unsupported session field')
        fields['observed_at'] = utc_now()
        with self.store.connect() as db:
            db.execute('UPDATE codex_sessions SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', (*fields.values(), key))

    def runtime(self, state):
        with self.store.connect() as db:
            db.execute('INSERT INTO codex_runtime VALUES (1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET epoch=excluded.epoch,state=excluded.state,observed_at=excluded.observed_at', (self.epoch,state,utc_now()))

    def notify(self, session, key, text, buttons=None):
        payload = {'chat_id':session['chat_id'], 'text':redact_text('Codex '+session['id']+' / '+session['project']+'\n'+text, 3500)}
        if buttons:
            payload['reply_markup'] = {'inline_keyboard':buttons}
        self.store.queue_outbox(dedupe_key='codex:'+self.epoch+':'+key, chat_id=session['chat_id'], method='sendMessage', payload=payload)

    async def rpc(self, method, params):
        self.sequence += 1
        key = 'chief-'+str(self.sequence)
        future = asyncio.get_running_loop().create_future()
        self.waiters[key] = future
        try:
            await self.ws.send(json.dumps({'id':key,'method':method,'params':params}))
            return await asyncio.wait_for(future, 25)
        finally:
            self.waiters.pop(key,None)

    async def read(self):
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                if 'method' in data:
                    await self.event(data)
                elif data.get('id') in self.waiters:
                    future = self.waiters[data['id']]
                    if not future.done():
                        if 'error' in data:
                            future.set_exception(RpcError(redact_text(str(data['error']),500)))
                        else:
                            future.set_result(data.get('result',{}))
        finally:
            for future in list(self.waiters.values()):
                if not future.done():
                    future.set_exception(ConnectionError('Codex transport closed'))

    def session_for(self, thread_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM codex_sessions WHERE thread_id=? AND epoch=?', (thread_id,self.epoch)).fetchone()
            return dict(row) if row else None

    async def event(self, data):
        method, p = data['method'], data.get('params') or {}
        s = self.session_for(p.get('threadId'))
        if not s:
            if 'id' in data:
                await self.ws.send(json.dumps({'id':data['id'],'error':{'code':-32601,'message':'Unmanaged thread or unsupported request'}}))
            return
        if method in QUESTION_METHODS and 'id' in data:
            self.question(s,data)
        elif 'id' in data:
            await self.ws.send(json.dumps({'id':data['id'],'error':{'code':-32601,'message':'Unsupported request; no approval granted'}}))
            self.notify(s,'unsupported:'+str(data['id']),'지원하지 않는 확인 요청입니다. Orca에서 이 작업을 확인해 주세요.')
        elif method == 'serverRequest/resolved':
            with self.store.connect() as db:
                rows = db.execute('SELECT id FROM codex_questions WHERE epoch=? AND request_id=? AND state IN (\'PENDING\',\'ANSWERING\',\'SENDING\')', (self.epoch,json.dumps(p.get('requestId')))).fetchall()
                db.execute("UPDATE codex_questions SET state='RESOLVED' WHERE epoch=? AND request_id=? AND state IN ('PENDING','ANSWERING','SENDING')", (self.epoch,json.dumps(p.get('requestId'))))
            for q in rows:
                self.notify(s,'resolved:'+q['id'],'확인 요청 '+q['id']+'이 처리되거나 종료되었습니다. 실행 결과는 별도로 알려드립니다.')
            with self.store.connect() as db:
                db.execute("UPDATE codex_sessions SET state='RUNNING',observed_at=? WHERE id=? AND state='WAITING' AND turn_id=? AND NOT EXISTS (SELECT 1 FROM codex_questions WHERE session_id=? AND state IN ('PENDING','ANSWERING','SENDING'))", (utc_now(),s['id'],s['turn_id'],s['id']))
        elif method == 'turn/started':
            turn = p['turn']
            with self.store.transaction() as db:
                answering = [r['source'][7:] for r in db.execute("SELECT source FROM codex_commands WHERE session_id=? AND state='SENDING' AND expected_turn IS NULL AND source LIKE 'answer:%'",(s['id'],))]
                questions = db.execute("SELECT id FROM codex_questions WHERE session_id=? AND method=? AND turn_id!=? AND state IN ('PENDING','ANSWERING','SENDING')",(s['id'],ASYNC_QUESTION,turn['id'])).fetchall()
                for question in questions:
                    if question['id'] in answering:
                        continue
                    db.execute("UPDATE codex_questions SET state='CLOSED' WHERE id=?",(question['id'],))
                    db.execute("UPDATE codex_commands SET state='REFUSED',error='A newer turn superseded this question' WHERE source=? AND state='QUEUED'",('answer:'+question['id'],))
            self.update(s['id'],state='RUNNING',turn_id=turn['id'])
            self.notify(s,'started:'+turn['id'],'작업을 시작했습니다.')
        elif method == 'item/completed':
            item = p.get('item',{})
            if item.get('type') == 'agentMessage':
                text = redact_text(item.get('text',''),3200)
                if text:
                    self.update(s['id'],last_text=text)
                if item.get('questions'):
                    questions = [{'id':'question-'+str(i),'header':'선택','question':q['title'],
                        'options':[{'label':v,'description':''} for v in q.get('options') or []]}
                        for i,q in enumerate(item['questions'])]
                    self.question(s,{'method':ASYNC_QUESTION,'id':'async:'+item['id'],
                        'params':{'threadId':p['threadId'],'turnId':p['turnId'],'itemId':item['id'],'questions':questions}})
                elif text:
                    self.notify(s,'message:'+item['id'],text)
        elif method == 'item/started':
            item = p.get('item',{})
            if item.get('type') == 'fileChange':
                self.file_changes[item.get('id')] = redact(item.get('changes',[]))
            label = {'commandExecution':'명령 실행 중', 'fileChange':'파일 수정 중', 'mcpToolCall':'도구 실행 중', 'webSearch':'자료 확인 중'}.get(item.get('type'))
            if label and time.monotonic()-self.last_progress.get(s['id'],0)>15:
                self.last_progress[s['id']] = time.monotonic()
                self.notify(s,'progress:'+str(item.get('id')),label)
        elif method == 'turn/completed':
            turn = p['turn']
            status = turn.get('status')
            with self.store.connect() as db:
                db.execute("UPDATE codex_questions SET state='CLOSED' WHERE session_id=? AND turn_id=? AND state IN ('PENDING','ANSWERING','SENDING') AND (method!=? OR ?!='completed')", (s['id'],turn['id'],ASYNC_QUESTION,status))
                waiting = db.execute("SELECT 1 FROM codex_questions WHERE session_id=? AND method=? AND state IN ('PENDING','ANSWERING','SENDING')",(s['id'],ASYNC_QUESTION)).fetchone()
            self.update(s['id'],state=('WAITING' if waiting else 'IDLE') if status in {'completed','interrupted'} else 'FAILED',turn_id=None)
            text = {'completed':'이번 요청 처리가 끝났습니다. 다음 지시도 같은 대화로 보내 주세요.', 'interrupted':'이번 요청이 중단되었습니다.'}.get(status,'이번 요청이 실패했습니다. '+redact_text(str(turn.get('error') or ''),700))
            if status == 'completed' and waiting:
                text = '질문 답변을 기다립니다. 질문의 버튼을 누르거나 질문 메시지에 답장해 주세요.'
            self.notify(s,'completed:'+turn['id'],text)

    def question(self, session, data):
        p, method = data['params'], data['method']
        key = 'q-'+secrets.token_hex(5)
        secret = any(q.get('isSecret') for q in p.get('questions',[]))
        if secret:
            visible = '비밀값 입력이 필요한 질문입니다. 텔레그램으로 보내지 말고 Orca에서 확인해 주세요.'
            safe = dict(p,questions=[{'id':q['id'],'isSecret':True,'question':'비밀값 질문','header':'비밀값'} for q in p['questions']])
        else:
            safe = redact(p)
            visible = p.get('reason') or ''
        buttons = []
        if method in {'item/tool/requestUserInput', ASYNC_QUESTION} and not secret:
            visible = '\n\n'.join(q['question']+'\n'+'\n'.join(str(i+1)+'. '+o['label'] for i,o in enumerate(q.get('options') or [])) for q in safe['questions'])
            if len(safe['questions']) == 1:
                buttons = [[{'text':o['label'][:60],'callback_data':f'cx:{key}:{i+1}'}] for i,o in enumerate(safe['questions'][0].get('options') or [])][:8]
            visible += '\n답변: /codex answer '+key+' 답변'
        elif method in {'item/commandExecution/requestApproval','item/fileChange/requestApproval','item/permissions/requestApproval'}:
            detail_fields = {k:safe[k] for k in ('command','cwd','additionalPermissions','networkApprovalContext','grantRoot','permissions','reason') if safe.get(k) is not None}
            if method == 'item/fileChange/requestApproval':
                detail_fields['changes'] = self.file_changes.get(p.get('itemId'))
            detail = json.dumps(detail_fields,ensure_ascii=False,indent=2)
            incomplete = len(detail)>2500 or (method == 'item/fileChange/requestApproval' and not detail_fields['changes']) or (method == 'item/commandExecution/requestApproval' and not any(safe.get(k) for k in ('command','networkApprovalContext')))
            if incomplete:
                safe['_telegram_approval_unavailable'] = True
            visible = '승인이 필요합니다.\n'+detail[:2500]+ ('\n승인 범위 전체를 표시하지 못했습니다. Orca에서 상세 내용을 확인해 주세요.' if incomplete else '')
            decisions = safe.get('availableDecisions') or ['accept','decline']
            buttons = [[{'text':label,'callback_data':f'cx:{key}:{value}'} for label,value in [('이번 요청 승인','accept'),('거절','decline'),('거절하고 중단','cancel')] if value in decisions and (value != 'accept' or not incomplete)]]
            if not buttons[0]:
                buttons = []
        with self.store.transaction() as db:
            inserted = db.execute('INSERT OR IGNORE INTO codex_questions VALUES (?,?,?,?,?,?,?,?,?,?)',
                (key,session['id'],self.epoch,json.dumps(data['id']),p['turnId'],method,json.dumps(safe), 'PENDING',None,(datetime.now(UTC)+timedelta(minutes=10)).isoformat(timespec='seconds')))
        if inserted.rowcount:
            self.update(session['id'],state='WAITING',turn_id=p['turnId'])
            self.notify(session,'question:'+key,'확인 요청 '+key+'\n'+visible,buttons)

    async def command(self, row):
        with self.store.transaction() as db:
            claimed = db.execute("UPDATE codex_commands SET state='SENDING' WHERE id=? AND state='QUEUED'", (row['id'],))
            if not claimed.rowcount:
                return
            session = dict(db.execute('SELECT * FROM codex_sessions WHERE id=?', (row['session_id'],)).fetchone())
        try:
            project = self.sessions.project(session['project'])
            if row['op'] not in {'create','message','stop','recover'}:
                raise RpcError('지원하지 않는 Codex 명령입니다')
            if str(Path(project['path']).resolve()) != session['cwd']:
                raise RpcError('등록 작업공간이 변경되었습니다')
            if row['op'] == 'create':
                if session['state'] != 'CREATING' or session['thread_id']:
                    raise RpcError('기존 작업을 중복 생성하지 않습니다')
                result = await self.rpc('thread/start',{'cwd':session['cwd'],'approvalPolicy':'on-request','approvalsReviewer':'user',
                    'sandbox':'workspace-write','developerInstructions':project.get('scope','')+QUESTION_INSTRUCTIONS,
                    'experimentalRawEvents':False})
                self.update(session['id'],thread_id=result['thread']['id'],epoch=self.epoch,state='IDLE')
                self.notify(session,'created:'+session['id'],'대화 연결을 준비했습니다. 이후 지시는 같은 Codex 대화에서 이어갑니다.')
            elif row['op'] == 'recover':
                if session['state'] != 'UNKNOWN' or not session['thread_id']:
                    raise RpcError('연결 복구 대상의 상태가 바뀌었습니다')
                result = await self.rpc('thread/resume',{'threadId':session['thread_id'],'cwd':session['cwd'],
                    'approvalPolicy':'on-request','approvalsReviewer':'user','sandbox':'workspace-write',
                    'developerInstructions':project.get('scope','')+QUESTION_INSTRUCTIONS})
                thread = result['thread']
                if thread.get('cwd') != session['cwd'] or any(t.get('status') == 'inProgress' for t in thread.get('turns',[])):
                    raise RpcError('복구한 대화의 작업공간 또는 실행 상태를 확인해 주세요')
                self.update(session['id'],epoch=self.epoch,state='IDLE',turn_id=None)
                self.notify(session,'recovered:'+row['id'],'이전 대화 연결을 복구했습니다. 다음 지시를 보내 주세요.')
            else:
                if session['epoch'] != self.epoch or session['state'] not in {'IDLE','RUNNING','WAITING'}:
                    raise RpcError('작업 생존 상태 미확인; 자동 재실행하지 않습니다')
                if row['op'] == 'stop':
                    if not row['expected_turn'] or session['turn_id'] != row['expected_turn']:
                        raise RpcError('중단할 요청이 이미 끝났습니다. 이후 요청은 중단하지 않았습니다')
                    await self.rpc('turn/interrupt',{'threadId':session['thread_id'],'turnId':row['expected_turn']})
                elif row['expected_turn']:
                    if session['turn_id'] != row['expected_turn']:
                        raise RpcError('추가 지시의 대상 요청이 이미 끝났습니다. 현재 상태를 확인하고 다시 보내 주세요')
                    await self.rpc('turn/steer',{'threadId':session['thread_id'],'expectedTurnId':row['expected_turn'],
                        'input':[{'type':'text','text':row['body']}]})
                    self.notify(session,'steer:'+row['id'],'작업 중인 요청에 추가 지시를 전달했습니다.')
                else:
                    if session['state'] != 'IDLE' and not (row['source'].startswith('answer:') and session['state']=='WAITING' and not session['turn_id']):
                        raise RpcError('접수 후 작업 상태가 바뀌었습니다. 중복 시작하지 않았습니다')
                    result = await self.rpc('turn/start',{'threadId':session['thread_id'],'input':[{'type':'text','text':row['body']}],
                        'approvalPolicy':'on-request','approvalsReviewer':'user','sandboxPolicy':{'type':'workspaceWrite',
                        'writableRoots':[session['cwd']],'networkAccess':False,'excludeTmpdirEnvVar':True,'excludeSlashTmp':True}})
            with self.store.connect() as db:
                db.execute("UPDATE codex_commands SET state='ACKED' WHERE id=?", (row['id'],))
                if row['source'].startswith('answer:'):
                    db.execute("UPDATE codex_questions SET state='RESOLVED' WHERE id=? AND method=? AND state='SENDING'",(row['source'][7:],ASYNC_QUESTION))
                    db.execute("UPDATE codex_sessions SET state=CASE WHEN turn_id IS NULL THEN 'IDLE' ELSE 'RUNNING' END WHERE id=? AND state='WAITING' AND NOT EXISTS (SELECT 1 FROM codex_questions WHERE session_id=? AND state IN ('PENDING','ANSWERING','SENDING'))",(session['id'],session['id']))
        except Exception as exc:
            unknown = not isinstance(exc, RpcError)
            state = 'UNKNOWN' if unknown else 'REFUSED'
            with self.store.connect() as db:
                db.execute('UPDATE codex_commands SET state=?,error=? WHERE id=?', (state,redact_text(str(exc),500),row['id']))
                if row['source'].startswith('answer:'):
                    db.execute("UPDATE codex_questions SET state=? WHERE id=? AND method=? AND state='SENDING'",('UNKNOWN' if unknown else 'CLOSED',row['source'][7:],ASYNC_QUESTION))
                    if not unknown:
                        db.execute("UPDATE codex_sessions SET state='IDLE' WHERE id=? AND state='WAITING' AND turn_id IS NULL AND NOT EXISTS (SELECT 1 FROM codex_questions WHERE session_id=? AND state IN ('PENDING','ANSWERING','SENDING'))",(session['id'],session['id']))
            if unknown or row['op'] == 'create':
                self.update(session['id'],state='UNKNOWN' if unknown else 'FAILED')
            self.notify(session,'command-error:'+row['id'],('전송 결과 미확인. 자동 재전송하지 않습니다. ' if unknown else '지시를 실행하지 않았습니다. ')+redact_text(str(exc),500))

    async def answers(self):
        with self.store.connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM codex_questions WHERE epoch=? AND state IN ('PENDING','ANSWERING')", (self.epoch,))]
        for q in rows:
            with self.store.connect() as db:
                session = dict(db.execute('SELECT * FROM codex_sessions WHERE id=?',(q['session_id'],)).fetchone())
            if q['expires_at'] <= utc_now():
                with self.store.transaction() as db:
                    changed = db.execute("UPDATE codex_questions SET state='EXPIRED' WHERE id=? AND state IN ('PENDING','ANSWERING')",(q['id'],))
                    db.execute("UPDATE codex_sessions SET state='IDLE' WHERE id=? AND state='WAITING' AND turn_id IS NULL AND NOT EXISTS (SELECT 1 FROM codex_questions WHERE session_id=? AND state IN ('PENDING','ANSWERING','SENDING'))",(q['session_id'],q['session_id']))
                if changed.rowcount and session['turn_id'] == q['turn_id'] and session['state'] in {'RUNNING','WAITING'}:
                    try:
                        await self.rpc('turn/interrupt',{'threadId':session['thread_id'],'turnId':q['turn_id']})
                        self.notify(session,'expired:'+q['id'],'확인 요청이 만료되어 작업 중단을 요청했습니다. 승인 없이 계속 실행하지 않습니다.')
                    except RpcError:
                        self.notify(session,'expired:'+q['id'],'확인 요청이 만료되었습니다. 현재 작업 상태를 확인해 주세요.')
            elif q['state'] == 'ANSWERING':
                if q['method'] == ASYNC_QUESTION:
                    with self.store.transaction() as db:
                        current = db.execute('SELECT * FROM codex_sessions WHERE id=?',(q['session_id'],)).fetchone()
                        if current['epoch'] != self.epoch or (current['turn_id'] and current['turn_id'] != q['turn_id']) or current['state'] not in {'IDLE','RUNNING','WAITING'}:
                            db.execute("UPDATE codex_questions SET state='CLOSED' WHERE id=? AND state='ANSWERING'",(q['id'],))
                            continue
                        changed = db.execute("UPDATE codex_questions SET state='SENDING' WHERE id=? AND state='ANSWERING'",(q['id'],))
                        if changed.rowcount:
                            params = json.loads(q['params'])
                            answers = json.loads(q['response'])['answers']
                            body = '\n'.join(question['question']+'\n사용자 답변: '+', '.join(answers[question['id']]['answers']) for question in params['questions'])
                            db.execute('INSERT OR IGNORE INTO codex_commands VALUES (?,?,?,?,?,?,?,?,?)',
                                ('cc-'+secrets.token_hex(5),'answer:'+q['id'],q['session_id'],'message',body,current['turn_id'],'QUEUED','',utc_now()))
                    continue
                with self.store.transaction() as db:
                    changed = db.execute("UPDATE codex_questions SET state='SENDING' WHERE id=? AND state='ANSWERING'",(q['id'],))
                if changed.rowcount:
                    await self.ws.send(json.dumps({'id':json.loads(q['request_id']),'result':json.loads(q['response'])}))

    async def connected(self, ws):
        self.ws = ws
        self.reader = asyncio.create_task(self.read())
        heartbeat = None
        try:
            await self.rpc('initialize',{'clientInfo':{'name':'deskmini_telegram','title':'DeskMini Telegram','version':'0.1.0'},'capabilities':{'experimentalApi':True}})
            await ws.send(json.dumps({'method':'initialized','params':{}}))
            with self.store.transaction() as db:
                db.execute("UPDATE codex_sessions SET state='UNKNOWN',observed_at=? WHERE thread_id IS NOT NULL",(utc_now(),))
                db.execute("UPDATE codex_commands SET state='UNKNOWN',error='bridge restarted; not replayed' WHERE state='SENDING'")
                db.execute("UPDATE codex_sessions SET state='UNKNOWN',observed_at=? WHERE state='CREATING' AND EXISTS (SELECT 1 FROM codex_commands WHERE session_id=codex_sessions.id AND op='create' AND state='UNKNOWN')",(utc_now(),))
                db.execute("UPDATE codex_questions SET state='CLOSED' WHERE state IN ('PENDING','ANSWERING','SENDING')")
            self.runtime('READY')
            async def beat():
                while True:
                    self.runtime('READY')
                    await asyncio.sleep(2)
            heartbeat = asyncio.create_task(beat())
            while not self.reader.done():
                with self.store.connect() as db:
                    rows = [dict(r) for r in db.execute("SELECT * FROM codex_commands WHERE state='QUEUED' ORDER BY rowid LIMIT 5")]
                for row in rows:
                    await self.command(row)
                await self.answers()
                await asyncio.sleep(.25)
            await self.reader
            raise ConnectionError('Codex connection ended')
        finally:
            if heartbeat:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
            self.runtime('OFFLINE')
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader


async def serve(settings):
    from websockets.asyncio.client import unix_connect
    config = load_config(settings.root)
    if not config.get('enabled'):
        return
    store = StateStore(settings.db_path,settings.root/'schema.sql')
    directory = settings.state_dir/'codex'
    directory.mkdir(mode=0o700,exist_ok=True)
    directory.chmod(0o700)
    with (directory/'bridge.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        socket_path = directory/'control.sock'
        bridge = Bridge(store,config,socket_path)
        bridge.runtime('STARTING')
        socket_path.unlink(missing_ok=True)
        proc = await asyncio.create_subprocess_exec(*server_argv(config['cli'],socket_path),
            stdin=asyncio.subprocess.DEVNULL,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,
            cwd=settings.root)
        try:
            for _ in range(200):
                if socket_path.exists():
                    break
                if proc.returncode is not None:
                    raise RuntimeError('Codex server exited')
                await asyncio.sleep(.05)
            socket_path.chmod(0o600)
            async with unix_connect(str(socket_path),open_timeout=10,max_size=8*1024*1024) as ws:
                await bridge.connected(ws)
        finally:
            bridge.runtime('OFFLINE')
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(),10)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()


def main():
    logging.basicConfig(level=logging.INFO)
    os.umask(0o077)
    try:
        asyncio.run(serve(Settings.load()))
    except Exception as exc:
        LOG.error('Codex bridge stopped: %s',type(exc).__name__)
        raise SystemExit(1)


if __name__ == '__main__':
    main()

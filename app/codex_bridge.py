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
from app.telegram_copy import approval_details, error_detail, message_card, project_name, sender_title


LOG = logging.getLogger('codex-bridge')
QUESTION_METHODS = {'item/tool/requestUserInput', 'item/commandExecution/requestApproval',
                    'item/fileChange/requestApproval', 'item/permissions/requestApproval'}
ASYNC_QUESTION = 'agentMessage/questions'
QUESTION_INSTRUCTIONS = ('\nUse the blocking request_user_input tool when a user choice is required before continuing. '
    'Do not use request_user_input_async for a decision that must stop the work. '
    'If a question tool fails, ask in plain text and stop; never claim buttons were sent. '
    'Do not ask for secrets over Telegram.')
MESSAGE_INSTRUCTIONS = ('\nWrite user-facing replies in clear, natural Korean for a phone screen. '
    'Lead with the result or current situation, then briefly say what the user needs to do, if anything. '
    'Use short paragraphs and simple lists; avoid Markdown tables and unnecessary headings, state codes, IDs, or protocol terminology. '
    'Explain unfamiliar technical terms when needed; preserve exact commands, filenames, error details, and approval scope. '
    'Do not claim a task succeeded merely because a tool finished or the response ended. '
    'When reporting a problem, explain what happened and the next practical step; do not invent a cause. ')


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

    def activity(self, session, summary, turn_id=None):
        with self.store.connect() as db:
            db.execute('INSERT INTO codex_activity VALUES (?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET epoch=excluded.epoch,turn_id=excluded.turn_id,summary=excluded.summary,observed_at=excluded.observed_at',
                (session['id'],self.epoch,turn_id,redact_text(summary,400),utc_now()))

    def notify(self, session, key, text, buttons=None, question_id=None, *, model=False):
        footer = ('확인 번호: '+question_id+'\n' if question_id else '')+'대화 번호: '+session['id']
        payload = {'chat_id':session['chat_id'], 'text':message_card(sender_title('codex',project_name(self.config,session['project']),model=model),text,footer)}
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
                self.notify(s,'resolved:'+q['id'],'확인 요청이 처리되었거나 종료되었습니다.\n이 알림은 실행 완료를 뜻하지 않습니다. 결과가 나오면 알려드리겠습니다.')
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
            self.activity(s,'이번 요청의 작업을 시작했습니다.',turn['id'])
            self.notify(s,'started:'+turn['id'],'작업을 시작했습니다.')
        elif method == 'item/completed':
            item = p.get('item',{})
            kind = item.get('type')
            if kind == 'commandExecution':
                code = item.get('exitCode')
                summary = '명령 처리가 끝났습니다.' if code==0 else '명령이 오류로 끝났습니다.' if isinstance(code,int) else '명령 처리 결과를 받았습니다.'
                self.activity(s,summary+('\n명령: '+redact_text(item['command'],180) if isinstance(item.get('command'),str) else ''),p.get('turnId'))
            elif kind in {'fileChange','mcpToolCall','webSearch'}:
                self.activity(s,{'fileChange':'파일 변경 단계의 처리 결과를 받았습니다.', 'mcpToolCall':'연결된 도구의 처리 결과를 받았습니다.', 'webSearch':'자료 검색 단계의 처리 결과를 받았습니다.'}[kind],p.get('turnId'))
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
                    self.notify(s,'message:'+item['id'],text,model=True)
        elif method == 'item/started':
            item = p.get('item',{})
            if item.get('type') == 'fileChange':
                self.file_changes[item.get('id')] = redact(item.get('changes',[]))
            label = {'commandExecution':'실행할 명령을 준비하고 있습니다.', 'fileChange':'파일 변경 내용을 준비하고 있습니다.', 'mcpToolCall':'연결된 도구에 작업을 요청하고 있습니다.', 'webSearch':'필요한 자료를 찾아보고 있습니다.'}.get(item.get('type'))
            if label:
                detail = '\n명령: '+redact_text(item['command'],180) if isinstance(item.get('command'),str) else ''
                self.activity(s,label+detail,p.get('turnId'))
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
            text = {'completed':'이번 요청에 대한 응답을 마쳤습니다.\n이어서 할 일이 있으면 이 대화에 보내 주세요.', 'interrupted':'이번 요청의 작업을 중단했습니다.\n이미 변경된 파일은 자동으로 되돌리지 않습니다.'}.get(status,'오류로 이번 요청을 끝내지 못했습니다.\n/codex status 로 상태를 확인해 주세요.\n\n오류 내용(참고)\n'+redact_text(error_detail(turn.get('error')),700))
            if status == 'completed' and waiting:
                text = '질문 답변을 기다립니다. 질문의 버튼을 누르거나 질문 메시지에 답장해 주세요.'
            self.activity(s,'질문에 대한 답변을 기다리고 있습니다.' if waiting else {
                'completed':'이번 요청에 대한 응답을 마쳤습니다.', 'interrupted':'이번 요청이 중단되었습니다.'}.get(status,'이번 요청에서 오류가 보고되었습니다.'),turn['id'])
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
            multiple = len(safe['questions']) > 1
            visible = ('선택한 내용을 알려 주세요.\n\n' if method == ASYNC_QUESTION else '작업을 이어가려면 답변이 필요합니다.\n\n')+'\n\n'.join(
                (str(n+1)+'. ' if multiple else '')+q['question']+'\n'+'\n'.join(
                    ('• ' if multiple else str(i+1)+'. ')+o['label']+('\n   '+o['description'] if o.get('description') else '')
                    for i,o in enumerate(q.get('options') or [])) for n,q in enumerate(safe['questions']))
            if len(safe['questions']) == 1:
                buttons = [[{'text':o['label'][:60],'callback_data':f'cx:{key}:{i+1}'}] for i,o in enumerate(safe['questions'][0].get('options') or [])][:8]
            visible += ('\n\n이 메시지에 답장해 질문 번호와 답을 한 줄씩 적어 주세요.\n예: 1. 첫 번째 답\n2. 두 번째 답' if multiple else '\n\n아래 버튼을 누르거나 이 메시지에 답장해 주세요.' if buttons else '\n\n이 메시지에 답장을 보내 주세요.')
        elif method in {'item/commandExecution/requestApproval','item/fileChange/requestApproval','item/permissions/requestApproval'}:
            detail_fields = {k:safe[k] for k in ('command','cwd','additionalPermissions','networkApprovalContext','grantRoot','permissions','reason') if safe.get(k) is not None}
            if method == 'item/fileChange/requestApproval':
                detail_fields['changes'] = self.file_changes.get(p.get('itemId'))
            detail = approval_details(detail_fields)
            incomplete = len(detail)>2500 or (method == 'item/fileChange/requestApproval' and not detail_fields['changes']) or (method == 'item/commandExecution/requestApproval' and not any(safe.get(k) for k in ('command','networkApprovalContext')))
            if incomplete:
                safe['_telegram_approval_unavailable'] = True
            visible = '실행 전에 확인이 필요합니다.\n아래 내용을 확인한 뒤 허용 여부를 골라 주세요.\n\n'+detail[:2500]+ ('\n\n전체 실행 범위를 표시하지 못해 승인 버튼을 숨겼습니다. Orca에서 상세 내용을 확인해 주세요.' if incomplete else '\n\n허용하면 아래 범위의 실행을 진행합니다.')
            decisions = safe.get('availableDecisions') or ['accept','decline']
            allow_label = '이번만 허용'
            if method == 'item/permissions/requestApproval':
                allow_label = '이번 작업 동안 허용'
                visible += '\n허용한 권한은 이번 요청의 작업이 끝날 때까지 적용됩니다. 같은 요청 안에서 여러 번 사용될 수 있습니다.'
            buttons = [[{'text':label,'callback_data':f'cx:{key}:{value}'} for label,value in [(allow_label,'accept'),('허용 안 함','decline'),('거절하고 중단','cancel')] if value in decisions and (value != 'accept' or not incomplete)]]
            if not buttons[0]:
                buttons = []
        with self.store.transaction() as db:
            inserted = db.execute('INSERT OR IGNORE INTO codex_questions VALUES (?,?,?,?,?,?,?,?,?,?)',
                (key,session['id'],self.epoch,json.dumps(data['id']),p['turnId'],method,json.dumps(safe), 'PENDING',None,(datetime.now(UTC)+timedelta(minutes=10)).isoformat(timespec='seconds')))
        if inserted.rowcount:
            self.update(session['id'],state='WAITING',turn_id=p['turnId'])
            self.activity(session,'답변 또는 실행 허용 여부를 확인하는 단계입니다.',p['turnId'])
            self.notify(session,'question:'+key,visible+'\n\n이 확인 요청은 10분 뒤 만료됩니다.',buttons,question_id=key,model=method in {'item/tool/requestUserInput',ASYNC_QUESTION})

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
                    'sandbox':'workspace-write','developerInstructions':project.get('scope','')+QUESTION_INSTRUCTIONS+MESSAGE_INSTRUCTIONS,
                    'experimentalRawEvents':False})
                self.update(session['id'],thread_id=result['thread']['id'],epoch=self.epoch,state='IDLE')
                with self.store.connect() as db:
                    queued = db.execute("SELECT 1 FROM codex_commands WHERE session_id=? AND op='message' AND state='QUEUED'",(session['id'],)).fetchone()
                if not queued:
                    self.notify(session,'created:'+session['id'],'대화가 연결되었습니다.\n할 일을 평소 말하듯 보내 주세요.')
            elif row['op'] == 'recover':
                if session['state'] != 'UNKNOWN' or not session['thread_id']:
                    raise RpcError('연결 복구 대상의 상태가 바뀌었습니다')
                result = await self.rpc('thread/resume',{'threadId':session['thread_id'],'cwd':session['cwd'],
                    'approvalPolicy':'on-request','approvalsReviewer':'user','sandbox':'workspace-write',
                    'developerInstructions':project.get('scope','')+QUESTION_INSTRUCTIONS+MESSAGE_INSTRUCTIONS})
                thread = result['thread']
                if thread.get('cwd') != session['cwd'] or any(t.get('status') == 'inProgress' for t in thread.get('turns',[])):
                    raise RpcError('복구한 대화의 작업공간 또는 실행 상태를 확인해 주세요')
                self.update(session['id'],epoch=self.epoch,state='IDLE',turn_id=None)
                self.activity(session,'이전 대화 연결을 복구했습니다. 다음 요청을 기다립니다.')
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
            self.notify(session,'command-error:'+row['id'],('지시가 전달됐는지 확인하지 못했습니다.\n중복 실행을 피하려고 다시 보내지 않았습니다.\n/codex status 에서 상태와 복구 방법을 확인해 주세요.' if unknown else '지시를 실행하지 않았습니다.\n/codex status 에서 현재 상태를 확인해 주세요.')+'\n\n상세 내용(참고)\n'+redact_text(str(exc) or type(exc).__name__,500))

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

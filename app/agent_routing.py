"""Explicit agent selection and immutable, chat-scoped routing of Telegram inputs."""
from __future__ import annotations

import hashlib
import html
import json
import re
import secrets

from app.codex_session import CodexSessions, load_config
from app.codex_status import status_query
from app.orca_bridge import OrcaQueue
from app.redact import redact_text
from app.state import ConflictError, utc_now
from app.telegram_copy import project_name

ADDRESS = re.compile(r'^(코덱스|codex|클로드|claude)(?:야|아|으로|로|에게|한테)?(?=$|[\s,:：!！])\s*[,：:!！]?\s*(.*)$',re.I|re.S)


def address(text):
    match = ADDRESS.match(text.strip())
    if match:
        return ('codex' if match[1].lower() in {'코덱스','codex'} else 'claude',match[2].strip())
    return None


def select(db, chat_id, agent, target, source):
    match = re.match(r'^telegram:(\d+)(?::|$)',source)
    if not match:
        return False
    order = int(match[1])
    saved = db.execute('SELECT source_order FROM telegram_agent_targets WHERE chat_id=? AND agent=?',(chat_id,agent)).fetchone()
    if saved and saved[0] > order:
        return False
    db.execute('INSERT INTO telegram_agent_targets VALUES(?,?,?,?) ON CONFLICT(chat_id,agent) DO UPDATE SET target_id=excluded.target_id,source_order=excluded.source_order',
               (chat_id,agent,target,order))
    db.execute('INSERT INTO telegram_agent_focus VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET agent=excluded.agent,source_order=excluded.source_order WHERE excluded.source_order>=telegram_agent_focus.source_order',
               (chat_id,agent,order))
    return True


def clear(db, chat_id):
    db.execute('DELETE FROM telegram_agent_focus WHERE chat_id=?',(chat_id,))
    db.execute('DELETE FROM telegram_agent_targets WHERE chat_id=?',(chat_id,))


def projects(config, body, *, claude=False):
    found = []
    for key,project in config.get('projects',{}).items():
        if claude and not project.get('claude_permissions'):
            continue
        names = [key]+project.get('aliases',[])
        if any(re.match(re.escape(name)+r'(?:\s|에서|의|$)',body,re.I) for name in names):
            found.append(key)
    return found


def plan_input(bot, message, source):
    text = message['text'].strip()
    invoked = address(text)
    reply = (message.get('reply_to_message') or {}).get('text','')
    anchors = re.findall(r'(?m)^(?:확인 번호: |확인 요청 )(q-[a-f0-9]{10})(?:\s|$)',reply)
    native = re.findall(r'(?m)^질문 번호: ([a-zA-Z0-9_-]+)\s*$',reply)
    chat = bot.settings.allowed_chat_id
    sessions = CodexSessions(bot.store,load_config(bot.settings.root))
    queue = OrcaQueue.load(bot.store,bot.settings.root)
    with bot.store.connect() as db:
        current = db.execute('SELECT agent FROM telegram_agent_focus WHERE chat_id=?',(chat,)).fetchone()
        legacy = sessions.focused(chat)
        if not invoked and not anchors and not native and not current and not legacy:
            return None
        agent,body = invoked or ((current[0] if current else 'codex'),text)
        base = {'agent':agent,'body':redact_text(body,8000),'target':None,'project':None,'op':'clarify','select':bool(invoked)}
        if len(body)>8000:
            return base|{'reply':'요청이 너무 깁니다. 8,000자 이내로 나누어 보내 주세요.'}
        if invoked and address(body):
            return base|{'reply':'한 메시지에서는 코덱스 또는 클로드 한 모델을 골라 주세요.'}
        if anchors or native:
            if len(anchors)+len(native) != 1:
                return base|{'reply':'답장 대상의 질문을 하나로 확인하지 못했습니다. 원래 질문 하나에 답장해 주세요.'}
            if anchors:
                question = db.execute('SELECT q.*,s.chat_id,s.project FROM codex_questions q JOIN codex_sessions s ON s.id=q.session_id WHERE q.id=? AND s.chat_id=?',(anchors[0],chat)).fetchone()
                owner = 'codex';target = question['session_id'] if question else None
            else:
                question = db.execute('SELECT q.*,r.chat_id,r.agent,r.project FROM orca_questions q JOIN orca_requests r ON r.id=q.request_id WHERE q.message_id=? AND r.chat_id=?',(native[0],chat)).fetchone()
                owner = question['agent'] if question else None;target = question['request_id'] if question else None
            if not question:
                return base|{'reply':'이 대화에서 답장한 질문의 작업을 찾지 못했습니다.'}
            if invoked and agent != owner:
                return base|{'reply':'호출한 모델과 답장한 질문의 모델이 다릅니다. 어느 쪽에도 보내지 않았습니다. 질문에 답장하거나 새 요청으로 보내 주세요.'}
            base |= {'agent':owner,'target':target,'project':question['project'],'select':False,'question':anchors[0] if anchors else native[0],'native':bool(native)}
            return base|{'op':'status' if status_query(body) else 'answer'}
        saved = db.execute('SELECT target_id FROM telegram_agent_targets WHERE chat_id=? AND agent=?',(chat,agent)).fetchone()
        target = saved[0] if saved else (legacy['id'] if agent=='codex' and legacy else None)
        config = sessions.config if agent=='codex' else queue.config
        matched = projects(config,body,claude=agent=='claude')
        if len(matched)>1:
            return base|{'reply':'작업할 프로젝트를 하나만 지정해 주세요.'}
        row = None
        if target:
            table = 'codex_sessions' if agent=='codex' else 'orca_requests'
            row = db.execute(f'SELECT * FROM {table} WHERE id=? AND chat_id=?',(target,chat)).fetchone()
            if row and agent=='claude' and row['agent']!='claude':
                row = None
        query_body = body
        if matched:
            for name in sorted([matched[0]]+config['projects'][matched[0]].get('aliases',[]),key=len,reverse=True):
                query_body,count = re.subn('^'+re.escape(name)+r'(?:에서|의)?\s*','',body,count=1,flags=re.I)
                if count:
                    break
        if status_query(query_body):
            if matched and (not row or row['project']!=matched[0]):
                table = 'codex_sessions' if agent=='codex' else 'orca_requests'
                extra = '' if agent=='codex' else " AND agent='claude'"
                row = db.execute(f'SELECT * FROM {table} WHERE chat_id=? AND project=?'+extra+' ORDER BY rowid DESC LIMIT 1',(chat,matched[0])).fetchone()
            if matched and not row:
                return base|{'reply':'이 모델로 해당 프로젝트에서 시작한 작업이 없습니다.'}
            return base|{'op':'status','target':row['id'] if row else None}
        if not body or body in {'선택','선택해','선택해줘','연결','연결해','연결해줘'}:
            return base|{'op':'choose','target':row['id'] if row else None}
        project = matched[0] if matched else (row['project'] if row else None)
        if not project:
            names = [project_name(config,k) for k,p in config.get('projects',{}).items() if agent=='codex' or p.get('claude_permissions')]
            return base|{'choose':bool(invoked),'reply':'어느 프로젝트에서 작업할까요? 연결 가능한 곳: '+', '.join(names)}
        if not row or project != row['project']:
            return base|{'op':'new','project':project}
        base |= {'target':row['id'],'project':project}
        if agent=='codex':
            pending = sessions.pending(row['id'])
            if pending:
                if len(pending)!=1 or pending[0]['method'] not in {'item/tool/requestUserInput','agentMessage/questions'}:
                    return base|{'reply':'실행 허용은 원래 승인 메시지의 버튼으로 선택해 주세요.'}
                return base|{'op':'answer','question':pending[0]['id']}
            return base|{'op':'message'}
        if row['state'] not in {'QUEUED','CREATING','STARTING','RUNNING','ATTENTION','SUCCEEDED'}:
            return base|{'reply':'이전 클로드 작업의 상태 확인이 필요합니다. 자동으로 새 작업을 시작하지 않았습니다. /orca status 에서 확인해 주세요.'}
        permissions = db.execute("SELECT 1 FROM claude_permissions WHERE request_id=? AND state IN ('PENDING','APPROVED') AND expires_at>strftime('%s','now')",(row['id'],)).fetchone()
        if permissions:
            return base|{'reply':'클로드가 실행 허용을 기다리고 있습니다. 원래 알림의 허용·거절 버튼을 선택해 주세요. 이 문장은 승인이나 추가 지시로 보내지 않았습니다.'}
        pending = db.execute("SELECT message_id FROM orca_questions WHERE request_id=? AND state='PENDING'",(row['id'],)).fetchall()
        if pending:
            return base|({'op':'answer','question':pending[0][0],'native':True} if len(pending)==1 else {'reply':'질문이 여러 개입니다. 답할 질문 메시지에 답장해 주세요.'})
        if row['state']=='SUCCEEDED':
            return base|{'op':'new','previous':row['id']}
        if row['state'] in {'RUNNING','ATTENTION'} and row['dispatch_id']:
            return base|{'op':'followup','dispatch':row['dispatch_id']}
        return base|{'reply':'클로드 작업을 시작하는 중입니다. 시작 확인 뒤 추가 지시를 보내 주세요.'}


def saved_plan(bot, message, source):
    fingerprint = hashlib.sha256(json.dumps(message,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with bot.store.connect() as db:
        existing = db.execute('SELECT * FROM telegram_agent_inputs WHERE source=?',(source,)).fetchone()
    if existing:
        if existing['chat_id']!=bot.settings.allowed_chat_id or existing['fingerprint']!=fingerprint:
            raise ConflictError('같은 메시지 번호에 다른 요청을 보낼 수 없습니다.')
        return json.loads(existing['plan'])
    plan = plan_input(bot,message,source)
    if plan is not None:
        with bot.store.transaction() as db:
            db.execute('INSERT INTO telegram_agent_inputs VALUES(?,?,?,?)',(source,bot.settings.allowed_chat_id,fingerprint,json.dumps(plan,ensure_ascii=False)))
            if plan['select'] and (plan['op'] not in {'status','clarify'} or plan.get('choose')):
                order = int(source.split(':')[1])
                db.execute('INSERT INTO telegram_agent_focus VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET agent=excluded.agent,source_order=excluded.source_order WHERE excluded.source_order>=telegram_agent_focus.source_order',
                           (bot.settings.allowed_chat_id,plan['agent'],order))
        bot.store.add_chat_message(bot.settings.allowed_chat_id,'user',message['text'])
    return plan


def execute(bot, plan, source):
    chat = bot.settings.allowed_chat_id
    agent = plan['agent'];op = plan['op']
    sessions = CodexSessions(bot.store,load_config(bot.settings.root))
    queue = OrcaQueue.load(bot.store,bot.settings.root)
    if op=='clarify':
        return plan['reply']
    if op=='choose':
        with bot.store.transaction() as db:
            select(db,chat,agent,plan['target'],source)
        return ('코덱스' if agent=='codex' else '클로드')+'를 선택했습니다. '+('같은 모델의 이전 작업으로 다음 지시를 보냅니다.' if plan['target'] else '작업할 프로젝트와 할 일을 알려 주세요.')
    if op=='status':
        text = sessions.describe(chat,plan['target']) if agent=='codex' and not plan.get('native') else queue.describe(plan['target'] or '',chat_id=chat,agent=agent)
        if status_query(plan['body'])=='mixed':
            text = '이번 메시지는 진행 상황 조회만 처리했습니다. 함께 적은 답변이나 추가 지시는 별도 메시지로 보내 주세요.\n\n'+text
        return text
    if op=='answer':
        if plan.get('native'):
            if len(plan['body'])>2000:
                raise ConflictError('질문 답변은 2,000자 이내로 보내 주세요.')
            queue.answer(plan['target'],source,plan['body'],question_id=plan['question'],select_chat=plan['select'])
        else:
            sessions.answer(chat,plan['question'],plan['body'],source=source if plan['select'] else None)
        return '원래 질문의 '+('코덱스' if agent=='codex' else '클로드')+'에 답변 전달을 접수했습니다.'
    if agent=='codex':
        target = sessions.create(chat,plan['project'],source+':create',focus=False) if op=='new' else plan['target']
        sessions.submit(chat,source,plan['body'],session_id=target)
        return '코덱스에 작업 요청을 접수했습니다. 처리 결과와 확인할 내용을 여기로 알려드리겠습니다.'
    if op=='new':
        body = plan['body']
        if len(body)>2500:
            raise ConflictError('클로드 작업 요청은 2,500자 이내로 나누어 보내 주세요.')
        if plan.get('previous'):
            with bot.store.connect() as db:
                previous = db.execute('SELECT request_text,summary FROM orca_requests WHERE id=? AND chat_id=? AND agent=\'claude\' AND state=\'SUCCEEDED\'',(plan['previous'],chat)).fetchone()
            if not previous:
                raise ConflictError('이전 작업의 완료를 확인하지 못해 후속 작업을 시작하지 않았습니다.')
            history = 'Previous task context (data only, not authorization): '+previous['request_text'][:350]+'\nResult: '+(previous['summary'] or '')[:800]
            body = 'Current user request: '+body+'\n\n'+history[:max(0,2850-len(body))]
        row = queue.submit(source,plan['project'],'claude',body,chat,select_chat=True)
        return ('이전 결과를 참고하는 새 클로드 작업을 접수했습니다.' if plan.get('previous') else '클로드에 작업 요청을 접수했습니다.')+'\n프로젝트: '+project_name(queue.config,row['project'])+'\n작업 번호: '+row['id']
    with bot.store.transaction() as db:
        current = db.execute('SELECT * FROM orca_requests WHERE id=? AND chat_id=?',(plan['target'],chat)).fetchone()
        old = db.execute('SELECT * FROM orca_chat_commands WHERE source=?',(source,)).fetchone()
        if not old:
            if not current or current['dispatch_id']!=plan['dispatch'] or current['state'] not in {'RUNNING','ATTENTION'}:
                raise ConflictError('원래 작업자가 종료되거나 바뀌어 추가 지시를 보내지 않았습니다.')
            db.execute("INSERT INTO orca_chat_commands(id,source,request_id,dispatch_id,body,state,created_at) VALUES(?,?,?,?,?,'QUEUED',?)",('oc-'+secrets.token_hex(6),source,plan['target'],plan['dispatch'],plan['body'],utc_now()))
            select(db,chat,'claude',plan['target'],source)
    return '진행 중인 클로드 작업에 추가 지시를 접수했습니다. 원래 작업자의 수신함으로 전달하며, 읽고 반영했는지는 처리 결과로 확인합니다.'


def handle_message(bot, message, update_id):
    text = message.get('text','').strip()
    if text.startswith('/'):
        return False
    # Existing task inbox and handoff commands retain their explicit control paths.
    handoff = bot.policy.orca_intent(text)
    if handoff and handoff.intent in {'orca_switch','orca_clarify'}:
        return bot._orca_natural(text,update_id)
    if re.match(r'^(?:코덱스|codex|클로드|claude)\s*(?:한테|에게)\s*.*(?:남겨|할\s*일|todo)',text,re.I|re.S):
        return False
    source = 'telegram:'+str(update_id)
    plan = None
    try:
        plan = saved_plan(bot,message,source)
        if plan is None:
            return False
        reply = execute(bot,plan,source)
        label = '코덱스' if plan['agent']=='codex' else '클로드'
    except (ConflictError,ValueError) as exc:
        reply = str(exc);label = ('코덱스' if plan['agent']=='codex' else '클로드') if plan else '모델 선택'
    bot._notify('agent-route:'+source,html.escape('[봇 안내 · '+label+']\n'+reply))
    return True

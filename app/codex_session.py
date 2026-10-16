from __future__ import annotations

import html
import json
import re
import secrets
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from app.redact import redact_text
from app.state import ConflictError, utc_now
from app.telegram_copy import local_time, project_name, state_label
from app.codex_status import status_query


def load_config(root):
    path = root / 'config/codex.toml'
    return tomllib.loads(path.read_text()) if path.is_file() else {}


class CodexSessions:
    def __init__(self, store, config):
        self.store, self.config = store, config

    def rows(self, chat_id):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM codex_sessions WHERE chat_id=? ORDER BY rowid DESC', (chat_id,))]

    def focused(self, chat_id):
        with self.store.connect() as db:
            row = db.execute('SELECT s.* FROM codex_focus f JOIN codex_sessions s ON s.id=f.session_id WHERE f.chat_id=?', (chat_id,)).fetchone()
            return dict(row) if row else None

    def focus(self, chat_id, key, source=None):
        with self.store.transaction() as db:
            if key and not db.execute('SELECT 1 FROM codex_sessions WHERE id=? AND chat_id=?', (key, chat_id)).fetchone():
                raise ConflictError('이 대화에 등록된 Codex 작업을 골라 주세요')
            from app.agent_routing import clear, select
            if key is None:
                clear(db,chat_id)
            if source is None or not source.startswith('telegram:') or select(db,chat_id,'codex',key,source):
                db.execute('INSERT INTO codex_focus VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET session_id=excluded.session_id', (chat_id, key))

    def ready(self):
        if not self.config.get('enabled'):
            raise ConflictError('Codex 실시간 연결이 꺼져 있습니다')
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM codex_runtime WHERE singleton=1').fetchone()
        try:
            age = (datetime.now(UTC)-datetime.fromisoformat(row['observed_at'])).total_seconds()
            if row['state'] == 'READY' and -5 <= age <= 15:
                return row['epoch']
        except (TypeError, ValueError):
            pass
        raise ConflictError('Codex 연결 상태를 확인하지 못했습니다. 작업은 전송하지 않았습니다')

    def project(self, name):
        p = self.config.get('projects', {}).get(name)
        if not p or not p.get('path') or p.get('ownership_ready', True) is not True:
            raise ConflictError('연결 가능한 프로젝트를 골라 주세요: ' + ', '.join(self.config.get('projects', {})))
        return p

    def create(self, chat_id, project, source, *, focus=True):
        self.ready()
        p = self.project(project)
        key = 'c-' + secrets.token_hex(4)
        with self.store.transaction() as db:
            old = db.execute('SELECT session_id FROM codex_commands WHERE source=?', (source,)).fetchone()
            if old:
                key = old['session_id']
            else:
                db.execute('INSERT INTO codex_sessions(id,project,cwd,chat_id,state,observed_at) VALUES (?,?,?,?,?,?)',
                           (key, project, str(Path(p['path']).resolve()), chat_id, 'CREATING', utc_now()))
                db.execute('INSERT INTO codex_commands VALUES (?,?,?,?,?,?,?,?,?)',
                           ('cc-'+secrets.token_hex(5), source, key, 'create', '', None, 'QUEUED', '', utc_now()))
            if focus:
                from app.agent_routing import select
                if not source.startswith('telegram:') or select(db,chat_id,'codex',key,source):
                    db.execute('INSERT INTO codex_focus VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET session_id=excluded.session_id',(chat_id,key))
        return key

    def submit(self, chat_id, source, body, op='message', *, session_id=None):
        epoch = self.ready()
        with self.store.connect() as db:
            old = db.execute('SELECT c.session_id FROM codex_commands c JOIN codex_sessions s ON s.id=c.session_id WHERE c.source=? AND s.chat_id=?', (source,chat_id)).fetchone()
        if old:
            return old['session_id']
        if session_id:
            with self.store.connect() as db:
                row = db.execute('SELECT * FROM codex_sessions WHERE id=? AND chat_id=?',(session_id,chat_id)).fetchone()
            session = dict(row) if row else None
        else:
            session = self.focused(chat_id)
        if not session:
            raise ConflictError('먼저 Codex 작업을 선택해 주세요: /codex chat')
        if session['state'] not in {'CREATING', 'IDLE', 'RUNNING', 'WAITING'} or (session['epoch'] and session['epoch'] != epoch):
            raise ConflictError('이전 작업이 어디까지 진행됐는지 확인이 필요해 지시를 보내지 않았습니다.\n/codex status 에서 상태와 복구 방법을 확인해 주세요.')
        if op == 'stop' and not session['turn_id']:
            raise ConflictError('현재 실행 중인 Codex 요청이 없습니다')
        body = redact_text(body.strip(), 8000)
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO codex_commands VALUES (?,?,?,?,?,?,?,?,?)',
                       ('cc-'+secrets.token_hex(5), source, session['id'], op, body, session['turn_id'], 'QUEUED', '', utc_now()))
            from app.agent_routing import select
            if select(db,chat_id,'codex',session['id'],source):
                db.execute('INSERT INTO codex_focus VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET session_id=excluded.session_id',(chat_id,session['id']))
        return session['id']

    def pending(self, session_id):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM codex_questions WHERE session_id=? AND state='PENDING' ORDER BY rowid", (session_id,))]

    def recover(self, chat_id, key, source):
        self.ready()
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM codex_sessions WHERE id=? AND chat_id=?',(key,chat_id)).fetchone()
            if not row or row['state'] != 'UNKNOWN' or not row['thread_id']:
                raise ConflictError('이 대화에서 연결이 끊긴 Codex 작업만 복구할 수 있습니다')
            db.execute('INSERT OR IGNORE INTO codex_commands VALUES (?,?,?,?,?,?,?,?,?)',
                       ('cc-'+secrets.token_hex(5),source,key,'recover','',None,'QUEUED','',utc_now()))
        self.focus(chat_id,key,source)

    def answer(self, chat_id, key, answer, *, source=None):
        epoch = self.ready()
        answer = redact_text(answer.strip(),8000)
        if not answer:
            raise ConflictError('답변을 입력해 주세요')
        with self.store.transaction() as db:
            row = db.execute('SELECT q.*,s.turn_id AS current_turn FROM codex_questions q JOIN codex_sessions s ON s.id=q.session_id WHERE q.id=? AND s.chat_id=?', (key, chat_id)).fetchone()
            if not row or row['epoch'] != epoch or row['state'] != 'PENDING' or row['expires_at'] <= utc_now():
                raise ConflictError('이미 처리되었거나 만료된 질문입니다. 현재 질문을 확인해 주세요')
            p = json.loads(row['params'])
            method = row['method']
            if method == 'agentMessage/questions' and row['current_turn'] and row['current_turn'] != row['turn_id']:
                raise ConflictError('해당 질문 이후 다른 요청이 시작되었습니다. 현재 질문을 확인해 주세요')
            if answer == 'accept' and p.get('_telegram_approval_unavailable'):
                raise ConflictError('승인 범위 전체를 표시하지 못한 요청입니다. Orca에서 상세 내용을 확인해 주세요')
            if method in {'item/tool/requestUserInput','agentMessage/questions'}:
                questions = p.get('questions', [])
                if any(q.get('isSecret') for q in questions):
                    raise ConflictError('비밀값 질문은 텔레그램으로 답할 수 없습니다')
                if len(questions) == 1:
                    question = questions[0]
                    options = question.get('options') or []
                    if answer.isdigit() and 1 <= int(answer) <= len(options):
                        answer = options[int(answer)-1]['label']
                    result = {'answers': {question['id']: {'answers': [answer]}}}
                else:
                    try:
                        values = json.loads(answer)
                        if not isinstance(values, dict) or set(values) != {q['id'] for q in questions} or not all(isinstance(v, str) and v for v in values.values()):
                            raise ValueError()
                        result = {'answers': {k: {'answers': [v]} for k, v in values.items()}}
                    except (ValueError, TypeError):
                        lines = [re.fullmatch(r'\s*(\d+)[.)]\s*(.+?)\s*', line) for line in answer.splitlines() if line.strip()]
                        if len(lines) != len(questions) or any(line is None for line in lines) or sorted(int(line[1]) for line in lines) != list(range(1,len(questions)+1)):
                            raise ConflictError('각 질문의 번호와 답을 한 줄씩 적어 주세요.\n예:\n1. 첫 번째 답\n2. 두 번째 답')
                        values = {questions[int(line[1])-1]['id']: line[2] for line in lines}
                        result = {'answers': {k: {'answers': [v]} for k,v in values.items()}}
            elif method in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}:
                if answer not in {'accept', 'decline', 'cancel'}:
                    raise ConflictError('실행 승인은 해당 질문의 승인·거절 버튼으로 선택해 주세요')
                if p.get('availableDecisions') and answer not in p['availableDecisions']:
                    raise ConflictError('이 요청이 허용한 선택지가 아닙니다')
                result = {'decision': answer}
            elif method == 'item/permissions/requestApproval':
                if answer not in {'accept', 'decline'}:
                    raise ConflictError('요청된 권한의 승인·거절 버튼을 선택해 주세요')
                result = {'permissions': p.get('permissions', {}) if answer == 'accept' else {}, 'scope': 'turn'}
            else:
                raise ConflictError('이 질문 유형은 Orca에서 확인해 주세요')
            db.execute("UPDATE codex_questions SET state='ANSWERING',response=? WHERE id=? AND state='PENDING'", (json.dumps(result), key))
            if source:
                from app.agent_routing import select
                if select(db,chat_id,'codex',row['session_id'],source):
                    db.execute('INSERT INTO codex_focus VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET session_id=excluded.session_id',(chat_id,row['session_id']))

    def describe(self, chat_id, key=None):
        rows = self.rows(chat_id)
        focus = self.focused(chat_id)
        if key:
            rows = [row for row in rows if row['id']==key]
            if not rows:
                raise ConflictError('이 대화에서 해당 작업을 찾지 못했습니다.')
        rows.sort(key=lambda row: row['id'] != (focus or {}).get('id'))
        epoch = None
        try:
            epoch = self.ready()
            status = '코덱스에 연결되어 있습니다.'
        except ConflictError as exc:
            status = '현재 연결 상태를 확인하지 못했습니다.\n아래 내용은 마지막으로 받은 기록입니다.'
        cards = []
        for row in rows[:5]:
            selected = focus and focus['id'] == row['id']
            card = ('지금 대화하는 작업 · ' if selected else '다른 작업 · ') + project_name(self.config,row['project'])
            live = epoch and row['epoch']==epoch
            label = state_label(row['state']) if live else '현재 상태 미확인 · 마지막 기록: '+state_label(row['state'])
            with self.store.connect() as db:
                activity = db.execute('SELECT * FROM codex_activity WHERE session_id=?',(row['id'],)).fetchone()
                questions = db.execute("SELECT * FROM codex_questions WHERE session_id=? AND state IN ('PENDING','ANSWERING','SENDING') ORDER BY rowid",(row['id'],)).fetchall()
            if live and row['state']=='WAITING' and questions and all(q['state']!='PENDING' for q in questions):
                label = '받은 답변을 전달하는 중'
            card += '\n상태: '+label+'\n마지막 확인: '+local_time(row['observed_at'])
            if activity:
                card += '\n\n마지막으로 확인한 동작\n'+activity['summary']+'\n동작 확인: '+local_time(activity['observed_at'])
            pending = [q for q in questions if q['state']=='PENDING' and q['epoch']==epoch and q['expires_at']>utc_now()]
            if pending:
                params = json.loads(pending[0]['params'])
                if pending[0]['method'] in {'item/tool/requestUserInput','agentMessage/questions'}:
                    prompt = ' / '.join(q.get('question','') for q in params.get('questions',[]))
                else:
                    prompt = '실행 또는 권한 허용 여부를 기다리고 있습니다.'
                card += '\n\n확인이 필요한 내용\n'+redact_text(prompt,350)+'\n원래 질문에 답장하거나 해당 메시지의 버튼을 눌러 주세요.'
            if row['last_text']:
                card += '\n\n마지막 응답\n'+row['last_text'][:600]
            if row['state'] == 'UNKNOWN' and row['thread_id']:
                card += '\n\n대화 연결을 복구하려면:\n/codex recover '+row['id']+'\n복구해도 이전 지시를 자동으로 다시 실행하지 않습니다.'
            card += '\n대화 번호: '+row['id']
            cards.append(card)
        return status+'\n\n'+('\n\n'.join(cards) or '아직 연결한 작업이 없습니다.\n예: 코덱스로 연결시험 README를 읽어줘')


def handle_message(bot, message, update_id):
    text = message.get('text', '').strip()
    if re.match(r'^(?:코덱스|codex|클로드|claude)\s*(?:한테|에게)\s*.*(?:남겨|할\s*일|todo)',text,re.I|re.S):
        return False
    sessions = CodexSessions(bot.store, load_config(bot.settings.root))
    chat_id = bot.settings.allowed_chat_id
    source = f'telegram:{update_id}'
    if text in {'/assistant', '/비서', '비서로 돌아가', '비서 모드로 돌아가'}:
        sessions.focus(chat_id, None)
        bot._notify('codex:'+source, '[봇 안내]\n비서 대화로 돌아왔습니다. Codex 작업은 계속 유지합니다.')
        return True
    command = text.split(maxsplit=1)[0].split('@')[0] if text else ''
    argument = text.split(maxsplit=1)[1] if ' ' in text else ''
    control = command == '/codex' and bool(argument)
    natural = re.match(r'^코덱스(?:로|에게|한테)?\s+(.+)', text, re.S)
    if natural and re.search(r'남겨|할\s*일|todo',text,re.I):
        natural = None
    focused = sessions.focused(chat_id)
    requested_projects = [name for name,p in sessions.config.get('projects',{}).items()
        if natural and any(re.match(re.escape(alias)+r'(?:\s|에서|의|$)',natural[1]) for alias in [name]+p.get('aliases',[]))]
    reply_text = (message.get('reply_to_message') or {}).get('text','')
    anchors = re.findall(r'(?m)^(?:확인 요청 |확인 번호: )(q-[a-f0-9]{10})(?:\s|$)',reply_text)
    anchored = anchors[-1] if anchors else None
    inquiry = status_query(text)
    codex_inquiry = inquiry and re.match(r'^코덱스',text)
    if not control and text.startswith('/'):
        return False
    if not control and not focused and not natural and not anchored and not codex_inquiry:
        return False
    bot.store.add_chat_message(chat_id, 'user', text)
    try:
        if control:
            parts = argument.split(maxsplit=2)
            action = parts[0]
            if action in {'chat', 'status'}:
                reply = sessions.describe(chat_id) + '\n\n이어서 할 일은 평소 말하듯 보내 주세요.\n다른 대화 선택: /codex use 대화번호\n비서로 돌아가기: /assistant'
            elif action == 'new' and len(parts) >= 2:
                key = sessions.create(chat_id, parts[1], source+':create')
                if len(parts) == 3:
                    sessions.submit(chat_id, source, parts[2])
                reply = project_name(sessions.config,parts[1])+' 작업에 연결하고 있습니다.\n이어서 할 일은 이 대화에 보내 주세요. 진행 상황과 확인할 내용을 여기로 알려드리겠습니다.'
            elif action == 'use' and len(parts) == 2:
                sessions.focus(chat_id, parts[1],source)
                reply = sessions.describe(chat_id)
            elif action == 'recover' and len(parts) == 2:
                sessions.recover(chat_id,parts[1],source)
                reply = '이전 Codex 대화의 연결 복구를 접수했습니다. 이전 지시는 자동 재실행하지 않습니다.'
            elif action == 'answer' and len(parts) == 3:
                sessions.answer(chat_id, parts[1], parts[2])
                reply = '답변을 받았습니다. 코덱스에 전달하겠습니다.'
            elif action == 'stop':
                sessions.submit(chat_id, source, '', 'stop')
                reply = '선택한 Codex 작업의 중단을 접수했습니다.'
            else:
                reply = '사용법: /codex chat · /codex new 프로젝트 지시 · /codex use 작업ID · /codex answer 질문ID 답변 · /codex stop'
        elif inquiry:
            key = None
            if anchored:
                with bot.store.connect() as db:
                    question = db.execute('SELECT q.session_id FROM codex_questions q JOIN codex_sessions s ON s.id=q.session_id WHERE q.id=? AND s.chat_id=?',(anchored,chat_id)).fetchone()
                if not question:
                    raise ConflictError('답장한 질문의 작업을 이 대화에서 찾지 못했습니다.')
                key = question['session_id']
            elif focused:
                key = focused['id']
            reply = sessions.describe(chat_id,key)
            if inquiry=='mixed':
                reply = '이번 메시지는 진행 상황 조회만 처리했습니다. 함께 적은 답변이나 변경 지시는 별도 메시지로 보내 주세요.\n\n'+reply
        elif anchored:
            sessions.answer(chat_id, anchored, text)
            reply = '답변을 받았습니다. 질문한 작업의 코덱스에 전달하겠습니다.'
        elif not focused or (requested_projects and (len(requested_projects)!=1 or requested_projects[0]!=focused['project'])):
            projects = requested_projects if focused else [name for name,p in sessions.config.get('projects',{}).items() if any(alias in text for alias in [name]+p.get('aliases',[]))]
            if len(projects) != 1:
                reply = '어느 프로젝트에서 작업할까요?\n지금 연결할 수 있는 곳: '+', '.join(project_name(sessions.config,k) for k in sessions.config.get('projects',{}))+'\n예: 코덱스로 연결시험 README를 읽어줘'
            else:
                key = sessions.create(chat_id, projects[0], source+':create')
                sessions.submit(chat_id, source, text)
                reply = project_name(sessions.config,projects[0])+' 작업 요청을 받았습니다.\n코덱스에 연결한 뒤 진행 상황과 확인할 내용을 여기로 알려드리겠습니다.'
        else:
            questions = sessions.pending(focused['id'])
            if questions:
                if message.get('reply_to_message'):
                    raise ConflictError('답장 대상에서 Codex 질문을 확인하지 못했습니다. 질문 메시지에 답장하거나 질문 ID를 지정해 주세요')
                if len(questions) != 1 or questions[0]['method'] not in {'item/tool/requestUserInput','agentMessage/questions'}:
                    raise ConflictError('먼저 확인이 필요한 요청이 있습니다.\n해당 메시지의 버튼을 눌러 주세요. 실행 승인은 일반 답장으로 처리하지 않습니다.')
                sessions.answer(chat_id, questions[0]['id'], text)
                reply = '답변을 받았습니다. 코덱스에 전달하겠습니다.'
            else:
                key = sessions.submit(chat_id, source, text)
                reply = project_name(sessions.config,focused['project'])+' 작업 요청을 받았습니다.\n진행 상황과 확인할 내용을 여기로 알려드리겠습니다.'
        bot._notify('codex:'+source, html.escape('[봇 안내 · 코덱스]\n'+reply))
    except ConflictError as exc:
        bot._notify('codex:'+source, html.escape('[봇 안내 · 코덱스]\n'+str(exc)))
    return True


def handle_callback(bot, data):
    _, key, answer = data.split(':', 2)
    sessions = CodexSessions(bot.store, load_config(bot.settings.root))
    sessions.answer(bot.settings.allowed_chat_id, key, answer)
    reply = {'accept':'이번 실행을 허용한다는 답을 받았습니다.', 'decline':'이 요청을 거절한다는 답을 받았습니다.', 'cancel':'요청을 거절하고 중단한다는 답을 받았습니다.'}.get(answer,'답변을 받았습니다.')
    if answer == 'accept':
        with bot.store.connect() as db:
            question = db.execute('SELECT method FROM codex_questions WHERE id=?',(key,)).fetchone()
        if question['method'] == 'item/permissions/requestApproval':
            reply = '이번 요청의 작업이 끝날 때까지 권한을 허용한다는 답을 받았습니다.'
    bot._send('[봇 안내 · 코덱스]\n'+reply+'\n코덱스의 처리 결과를 확인한 뒤 알려드리겠습니다.')

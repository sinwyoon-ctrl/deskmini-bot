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

    def focus(self, chat_id, key):
        with self.store.transaction() as db:
            if key and not db.execute('SELECT 1 FROM codex_sessions WHERE id=? AND chat_id=?', (key, chat_id)).fetchone():
                raise ConflictError('이 대화에 등록된 Codex 작업을 골라 주세요')
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

    def create(self, chat_id, project, source):
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
        self.focus(chat_id, key)
        return key

    def submit(self, chat_id, source, body, op='message'):
        epoch = self.ready()
        with self.store.connect() as db:
            old = db.execute('SELECT c.session_id FROM codex_commands c JOIN codex_sessions s ON s.id=c.session_id WHERE c.source=? AND s.chat_id=?', (source,chat_id)).fetchone()
        if old:
            return old['session_id']
        session = self.focused(chat_id)
        if not session:
            raise ConflictError('먼저 Codex 작업을 선택해 주세요: /codex chat')
        if session['state'] not in {'CREATING', 'IDLE', 'RUNNING', 'WAITING'} or (session['epoch'] and session['epoch'] != epoch):
            raise ConflictError('작업 상태가 미확인입니다. 중복 실행을 막기 위해 지시를 전송하지 않았습니다')
        if op == 'stop' and not session['turn_id']:
            raise ConflictError('현재 실행 중인 Codex 요청이 없습니다')
        body = redact_text(body.strip(), 8000)
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO codex_commands VALUES (?,?,?,?,?,?,?,?,?)',
                       ('cc-'+secrets.token_hex(5), source, session['id'], op, body, session['turn_id'], 'QUEUED', '', utc_now()))
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
        self.focus(chat_id,key)

    def answer(self, chat_id, key, answer):
        epoch = self.ready()
        answer = redact_text(answer.strip(),8000)
        if not answer:
            raise ConflictError('답변을 입력해 주세요')
        with self.store.transaction() as db:
            row = db.execute('SELECT q.* FROM codex_questions q JOIN codex_sessions s ON s.id=q.session_id WHERE q.id=? AND s.chat_id=?', (key, chat_id)).fetchone()
            if not row or row['epoch'] != epoch or row['state'] != 'PENDING' or row['expires_at'] <= utc_now():
                raise ConflictError('이미 처리되었거나 만료된 질문입니다. 현재 질문을 확인해 주세요')
            p = json.loads(row['params'])
            method = row['method']
            if answer == 'accept' and p.get('_telegram_approval_unavailable'):
                raise ConflictError('승인 범위 전체를 표시하지 못한 요청입니다. Orca에서 상세 내용을 확인해 주세요')
            if method == 'item/tool/requestUserInput':
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
                        raise ConflictError('질문별 답변을 JSON으로 보내 주세요: ' + json.dumps({q['id']: '답변' for q in questions}, ensure_ascii=False))
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

    def describe(self, chat_id):
        rows = self.rows(chat_id)
        focus = self.focused(chat_id)
        try:
            self.ready()
            status = '연결 정상'
        except ConflictError as exc:
            status = str(exc)
        labels = {'CREATING':'연결 준비 중', 'IDLE':'다음 지시 대기', 'RUNNING':'작업 중', 'WAITING':'답변 대기', 'UNKNOWN':'상태 미확인', 'FAILED':'실패'}
        return 'Codex · ' + status + '\n' + ('\n\n'.join(
            ('선택됨 · ' if focus and focus['id'] == r['id'] else '') + r['id'] + ' / ' + r['project'] + ' · ' + labels.get(r['state'],r['state']) +
            '\n확인: '+r['observed_at']+'\n'+r['last_text'][:1000] for r in rows[:5]) or '연결된 작업이 없습니다.')


def handle_message(bot, message, update_id):
    text = message.get('text', '').strip()
    sessions = CodexSessions(bot.store, load_config(bot.settings.root))
    chat_id = bot.settings.allowed_chat_id
    source = f'telegram:{update_id}'
    if text in {'/assistant', '/비서', '비서로 돌아가', '비서 모드로 돌아가'}:
        sessions.focus(chat_id, None)
        bot._notify('codex:'+source, '비서 대화로 돌아왔습니다. Codex 작업은 계속 유지합니다.')
        return True
    command = text.split(maxsplit=1)[0].split('@')[0] if text else ''
    argument = text.split(maxsplit=1)[1] if ' ' in text else ''
    control = command == '/codex' and bool(argument)
    natural = re.match(r'^코덱스(?:로|에게|한테)?\s+(.+)', text, re.S)
    if natural and re.search(r'남겨|할\s*일|todo',text,re.I):
        natural = None
    focused = sessions.focused(chat_id)
    reply_text = (message.get('reply_to_message') or {}).get('text','')
    anchored = re.search(r'확인 요청 (q-[a-f0-9]{10})',reply_text)
    if not control and text.startswith('/'):
        return False
    if not control and not focused and not natural and not anchored:
        return False
    bot.store.add_chat_message(chat_id, 'user', text)
    try:
        if control:
            parts = argument.split(maxsplit=2)
            action = parts[0]
            if action in {'chat', 'status'}:
                reply = sessions.describe(chat_id) + '\n\n새 작업: /codex new trial 지시 내용\n선택: /codex use 작업ID · 비서: /assistant'
            elif action == 'new' and len(parts) >= 2:
                key = sessions.create(chat_id, parts[1], source+':create')
                if len(parts) == 3:
                    sessions.submit(chat_id, source, parts[2])
                reply = 'Codex 연결 접수 · '+key+' / '+parts[1]+'\n이후 일반 문장은 이 작업에 전달합니다. 비서 대화: /assistant'
            elif action == 'use' and len(parts) == 2:
                sessions.focus(chat_id, parts[1])
                reply = sessions.describe(chat_id)
            elif action == 'recover' and len(parts) == 2:
                sessions.recover(chat_id,parts[1],source)
                reply = '이전 Codex 대화의 연결 복구를 접수했습니다. 이전 지시는 자동 재실행하지 않습니다.'
            elif action == 'answer' and len(parts) == 3:
                sessions.answer(chat_id, parts[1], parts[2])
                reply = '답변을 접수했습니다. 같은 Codex 작업으로 전달합니다.'
            elif action == 'stop':
                sessions.submit(chat_id, source, '', 'stop')
                reply = '선택한 Codex 작업의 중단을 접수했습니다.'
            else:
                reply = '사용법: /codex chat · /codex new 프로젝트 지시 · /codex use 작업ID · /codex answer 질문ID 답변 · /codex stop'
        elif anchored:
            sessions.answer(chat_id, anchored[1], text)
            reply = '답장한 질문의 Codex 작업에 답변을 접수했습니다.'
        elif not focused:
            projects = [name for name,p in sessions.config.get('projects',{}).items() if any(alias in text for alias in [name]+p.get('aliases',[]))]
            if len(projects) != 1:
                reply = '어느 프로젝트를 Codex에 연결할까요? 현재 연결 가능: '+', '.join(sessions.config.get('projects',{}))+'\n예: 코덱스로 연결시험 README를 읽어줘'
            else:
                key = sessions.create(chat_id, projects[0], source+':create')
                sessions.submit(chat_id, source, text)
                reply = 'Codex 연결 접수 · '+key+' / '+projects[0]+'\n이후 일반 문장도 같은 작업에 전달합니다. 비서 대화: /assistant'
        else:
            questions = sessions.pending(focused['id'])
            if questions:
                if message.get('reply_to_message'):
                    raise ConflictError('답장 대상에서 Codex 질문을 확인하지 못했습니다. 질문 메시지에 답장하거나 질문 ID를 지정해 주세요')
                if len(questions) != 1 or questions[0]['method'] != 'item/tool/requestUserInput':
                    raise ConflictError('대기 중인 질문의 버튼 또는 /codex answer 질문ID 답변으로 대상과 답을 지정해 주세요')
                sessions.answer(chat_id, questions[0]['id'], text)
                reply = '질문 답변을 접수했습니다. 같은 Codex 작업으로 전달합니다.'
            else:
                key = sessions.submit(chat_id, source, text)
                reply = 'Codex 지시 접수 · '+key+'\n실행 결과와 확인할 질문을 이 대화로 알려드립니다.'
        bot._notify('codex:'+source, html.escape(reply))
    except ConflictError as exc:
        bot._notify('codex:'+source, html.escape(str(exc)))
    return True


def handle_callback(bot, data):
    _, key, answer = data.split(':', 2)
    sessions = CodexSessions(bot.store, load_config(bot.settings.root))
    sessions.answer(bot.settings.allowed_chat_id, key, answer)
    bot._send('답변을 접수했습니다. Codex의 처리 결과를 기다립니다.')

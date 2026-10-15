"""Explicit Telegram review of exact advanced ledger requests; no local execution."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from adapters.telegram import inline_keyboard
from app.redact import redact_text

PREFIX = '/strategy/advanced'
SCOPE = ('schema_version', 'source_kind', 'namespace', 'market')
LABELS = {'approve_experiment': '실험 승인', 'start_experiment': '실험 시작',
          'hold': '보류', 'reject': '거절', 'adopt': '연구 결과 채택'}
ALIASES = {'approve': 'approve_experiment', 'start': 'start_experiment'}
ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$')


def safe(value, limit=800):
    return html.escape(redact_text(str(value), limit), quote=True)


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def signed_headers(key, sender, method, target, body, *, timestamp=None, nonce=None):
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or secrets.token_urlsafe(32)
    sender = str(sender)
    if len(key) != 32 or not sender.isdecimal():
        raise ValueError('Invalid signing configuration')
    message = '\n'.join(('ione-advanced-auth-v1', 'telegram', sender, timestamp,
                         nonce, method.upper(), target, hashlib.sha256(body).hexdigest()))
    return {'X-IONE-Service': 'telegram', 'X-IONE-Sender': sender,
            'X-IONE-Timestamp': timestamp, 'X-IONE-Nonce': nonce,
            'X-IONE-Signature': hmac.new(key, message.encode(), hashlib.sha256).hexdigest(),
            'Content-Type': 'application/json'}


class RemoteError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__('Trader request refused')


class SignedHTTP:
    def __init__(self, base_url, key_path, *, opener=None):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or
                parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username):
            raise ValueError('Trader URL must be a loopback HTTP origin')
        self.base_url = base_url.rstrip('/')
        self.key_path = Path(key_path)
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open

    def request(self, sender, method, target, body=b''):
        if not target.startswith(PREFIX + '/') or '\n' in target:
            raise ValueError('Unsupported Trader endpoint')
        key_stat = self.key_path.stat()
        if key_stat.st_mode & 0o077:
            raise ValueError('Signing key permissions are not private')
        key = self.key_path.read_bytes()
        headers = signed_headers(key, sender, method, target, body)
        request = urllib.request.Request(self.base_url + target, data=body if method == 'POST' else None,
                                         headers=headers, method=method)
        try:
            with self.opener(request, timeout=12) as response:
                data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ValueError('Response limit exceeded')
            return json.loads(data)
        except urllib.error.HTTPError as exc:
            try:
                code = json.loads(exc.read(4096)).get('detail', {}).get('code', 'REQUEST_REFUSED')
            except (ValueError, AttributeError):
                code = 'REQUEST_REFUSED'
            raise RemoteError(exc.code, code if re.fullmatch('[A-Z_]{1,80}', str(code)) else 'REQUEST_REFUSED') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def reference(value, id_field):
    result = {key: value[key] for key in SCOPE}
    revision = value.get('revision', 1) if id_field == 'result_id' else value['revision']
    result.update(object_id=value[id_field], revision=revision, content_hash=value['content_hash'])
    if not ID_RE.fullmatch(result['object_id']) or not re.fullmatch('[0-9a-f]{64}', result['content_hash']):
        raise ValueError('Invalid immutable reference')
    return result


def request_for(view, action, reason='Telegram에서 검토 후 명시적으로 확인'):
    if action not in LABELS or action not in view.get('allowed_actions', []):
        raise ValueError('현재 상태에서 허용되지 않는 동작입니다. 상세를 다시 확인해 주세요.')
    imp, plan, result = view['improvement'], view.get('plan'), view.get('result')
    body = {key: imp[key] for key in SCOPE}
    body.update(improvement_ref=reference(imp, 'improvement_id'), state_version=view['state_version'],
                idempotency_key='tg:' + secrets.token_hex(24))
    plan_ref = reference(plan, 'plan_id') if plan else None
    result_ref = reference(result, 'result_id') if result else None
    for ref in (plan_ref, result_ref, plan.get('input_ref') if plan else None):
        if ref and any(ref[key] != body[key] for key in SCOPE):
            raise ValueError('자료 범위가 일치하지 않습니다.')
    if action == 'start_experiment':
        if not plan:
            raise ValueError('확정된 실험 계획이 없습니다.')
        approvals = [event for event in view.get('decisions', [])
                     if event.get('request', {}).get('decision') == 'approve_experiment'
                     and event['request'].get('improvement_ref') == body['improvement_ref']
                     and event['request'].get('plan_ref') == plan_ref]
        if not approvals:
            raise ValueError('이 계획의 승인 기록이 없습니다.')
        body.update(object_type='ExperimentRequest', plan_ref=plan_ref, input_ref=plan['input_ref'],
                    approval_event_id=approvals[-1]['event_id'], engine_version=plan['engine_version'])
        target = PREFIX + '/experiments'
    else:
        if action == 'approve_experiment':
            result_ref = None
        if action in ('approve_experiment', 'adopt') and not plan_ref or action == 'adopt' and not result_ref:
            raise ValueError('계획 또는 결과 참조가 없습니다.')
        body.update(object_type='DecisionRequest', decision=action, plan_ref=plan_ref, result_ref=result_ref,
                    application_scope='TEST_ONLY' if body['source_kind'] == 'synthetic' else 'RESEARCH_ONLY',
                    reason=redact_text(reason, 800))
        target = PREFIX + '/decisions'
    return {'target': target, 'body': body, 'action': action}


def review_text(view):
    imp = view['improvement']; draft = imp.get('draft', {}); plan = view.get('plan')
    lines = ['<b>트레이더 검토 대상</b>',
             f"<code>{safe(imp['improvement_id'], 170)}</code>",
             f"{safe(imp['market'])} · {safe(imp['source_kind'])} · {safe(imp['namespace'], 170)}",
             f"개정 {imp['revision']} · 상태 버전 {view['state_version']} · {safe(view['stage'])}",
             f"내용 해시 <code>{safe(imp['content_hash'], 70)}</code>",
             f"가설: {safe(draft.get('hypothesis', '미제공'), 300)}",
             f"자료: {safe(view.get('data_status'))} · {safe(draft.get('readiness', ''))}",
             f"근거: {safe(draft.get('support', {}).get('summary', ''), 220)}",
             f"반대: {safe(draft.get('opposition', {}).get('summary', ''), 220)}"]
    if plan:
        lines += [f"계획 <code>{safe(plan['plan_id'],170)}</code> 개정 {plan['revision']}",
                  f"계획 해시 <code>{safe(plan['content_hash'],70)}</code>",
                  f"입력 <code>{safe(plan['input_ref']['object_id'],170)}</code> 개정 {plan['input_ref']['revision']}",
                  f"입력 해시 <code>{safe(plan['input_ref']['content_hash'],70)}</code> · 엔진 {safe(plan['engine_version'],80)}",
                  f"변경: {safe(packed(plan['change']),300)}",
                  f"진단/평가: {safe(packed(plan['diagnostic_period']),160)} / {safe(packed(plan['evaluation_period']),160)}",
                  f"비용: {safe(packed(plan['costs']),360)}",
                  f"채택 기준: {safe(packed(plan['criteria']),450)}",
                  f"한도: {safe(packed(plan['budget']),360)}",
                  f"적용 범위: {safe(plan['application_scope'])}"]
    result = view.get('result')
    if result:
        lines += [f"결과: {safe(result.get('criteria_outcome'))}",
                  f"결과 해시 <code>{safe(result['content_hash'],70)}</code>"]
        for name, label in [('baseline', '기준'), ('candidate', '후보')]:
            portfolio = result.get(name, {})
            metrics = {key: portfolio.get(key) for key in ('net_return_pct', 'max_drawdown_pct', 'completed_trades')}
            lines.append(f'{label}: {safe(packed(metrics),350)}')
    lines += ['실험 승인과 시작은 별도입니다. 채택은 연구 기록이며 운영 전략 변경은 별도입니다.']
    return '\n'.join(lines)


class Confirmations:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600) if not self.path.exists() else None
        if fd is not None:
            os.close(fd)
        if self.path.stat().st_mode & 0o077:
            raise ValueError('Confirmation storage must be private')
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS trader_confirmations (
                token TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL, sender INTEGER NOT NULL,
                chat INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                expires REAL NOT NULL, state TEXT NOT NULL, attempted REAL, outcome TEXT)''')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def put(self, source, sender, chat, kind, payload, ttl):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO trader_confirmations VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (secrets.token_urlsafe(18), source, sender, chat, kind, packed(payload),
                        self.clock() + ttl, 'READY', None, None))
            row = db.execute('SELECT * FROM trader_confirmations WHERE source=?', (source,)).fetchone()
            if row['sender'] != sender or row['chat'] != chat or row['payload'] != packed(payload):
                raise ValueError('확인 요청이 일치하지 않습니다.')
            return dict(row)

    def by_source(self, source, sender, chat):
        with self.connect() as db:
            row = db.execute('SELECT * FROM trader_confirmations WHERE source=?', (source,)).fetchone()
        return self.load(row['token'], sender, chat) if row else None

    def load(self, token, sender, chat):
        with self.connect() as db:
            row = db.execute('SELECT * FROM trader_confirmations WHERE token=?', (token,)).fetchone()
        if not row or row['sender'] != sender or row['chat'] != chat:
            raise ValueError('이 사용자·대화의 확인 버튼이 아닙니다.')
        if row['expires'] <= self.clock():
            raise ValueError('확인 버튼이 만료됐습니다. 상세를 다시 확인해 주세요.')
        return dict(row)

    def claim(self, token, sender, chat):
        self.load(token, sender, chat)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = dict(db.execute('SELECT * FROM trader_confirmations WHERE token=?', (token,)).fetchone())
            if row['expires'] <= self.clock():
                raise ValueError('확인 버튼이 만료됐습니다.')
            if row['state'] in ('DONE', 'REFUSED'):
                return row, False
            if row['state'] == 'INFLIGHT' and row['attempted'] > self.clock() - 30:
                raise ValueError('같은 요청을 처리 중입니다. 잠시 후 상태를 확인해 주세요.')
            db.execute("UPDATE trader_confirmations SET state='INFLIGHT',attempted=? WHERE token=?", (self.clock(), token))
            return row, True

    def finish(self, token, state, outcome):
        with self.connect() as db:
            db.execute('UPDATE trader_confirmations SET state=?,outcome=? WHERE token=?', (state, outcome, token))


class TraderTelegram:
    def __init__(self, http, confirmations, *, ttl=600):
        self.http, self.confirmations, self.ttl = http, confirmations, min(max(ttl, 60), 600)

    @classmethod
    def load(cls, settings):
        path = settings.root / 'config' / 'trader_advanced.toml'
        config = tomllib.loads(path.read_text()) if path.is_file() else {}
        if config.get('enabled') is not True:
            raise ValueError('트레이더 Telegram 승인이 아직 연결되지 않았습니다.')
        auth_root = Path(config.get('auth_root', '/home/ionenet/work/ione-trader-auth'))
        http = SignedHTTP(config.get('base_url', 'http://127.0.0.1:8888'), auth_root / 'signing.key')
        return cls(http, Confirmations(settings.state_dir / 'trader-advanced.sqlite'),
                   ttl=int(config.get('confirmation_ttl_seconds', 600)))

    def offer(self, sender, chat, source, view, action, reason=None):
        previous = self.confirmations.by_source(source, sender, chat)
        if previous:
            return self.confirm_card(previous)
        payload = request_for(view, action, reason or 'Telegram에서 검토 후 명시적으로 확인')
        payload['review'] = review_text(view)
        row = self.confirmations.put(source, sender, chat, 'confirm', payload, self.ttl)
        return self.confirm_card(row)

    def confirm_card(self, row):
        payload = json.loads(row['payload'])
        text = payload['review'] + '\n\n<b>최종 확인: ' + LABELS[payload['action']] + '</b>'
        text += '\n위 개정·해시·상태 버전의 요청만 전달합니다. 확인 유효시간은 최대 10분입니다.'
        reason = payload['body'].get('reason')
        if reason:
            text += '\n사유: ' + safe(reason, 800)
        return text, inline_keyboard([[(LABELS[payload['action']] + ' 확인', 'ta:' + row['token'])]])

    def command(self, text, sender, chat, source):
        bits = text.strip().split(maxsplit=3)
        action = bits[1].lower() if len(bits) > 1 else 'status'
        if action == 'status':
            data = self.http.request(sender, 'GET', PREFIX + '/status')
            return ('<b>트레이더 자동 관측·실험</b>\n가용: ' + safe(data['availability']) +
                    '\n승인: ' + safe(data['authorization']) + '\n' +
                    '\n'.join(safe(v, 300) for v in data.get('prerequisites', [])) +
                    '\n/trader list · /trader view 개선ID', None)
        if action == 'list':
            market = bits[2].upper() if len(bits) > 2 else 'ALL'
            if market not in ('US', 'KR', 'ALL'):
                raise ValueError('시장: US · KR · ALL')
            data = self.http.request(sender, 'GET', PREFIX + '/improvements?market=' + market + '&limit=10')
            lines = ['<b>트레이더 개선 목록</b>']
            for view in data['items']:
                imp = view['improvement']
                lines.append(f"<code>/trader view {safe(imp['improvement_id'],170)}</code>\n"
                             f"{safe(imp['market'])} · {safe(imp['source_kind'])} · 개정 {imp['revision']} · {safe(view['stage'])}")
            if not data['items']:
                lines.append('생성된 개선안이 없습니다. 자료 축적과 실험 계획 확정이 먼저입니다.')
            if data.get('next_cursor'):
                lines.append('첫 10건입니다. 나머지는 앱에서 확인하세요.')
            return '\n'.join(lines), None
        action = ALIASES.get(action, action)
        if action not in ('view', *LABELS) or len(bits) < 3 or not ID_RE.fullmatch(bits[2]):
            raise ValueError('사용법: /trader status · list [US|KR|ALL] · view 개선ID · approve|start|hold|reject|adopt 개선ID [사유]')
        target = PREFIX + '/improvements/' + urllib.parse.quote(bits[2], safe='')
        previous = self.confirmations.by_source(source, sender, chat)
        if previous and previous['kind'] == 'confirm':
            return self.confirm_card(previous)
        if previous:
            view = json.loads(previous['payload'])['view']
        else:
            view = self.http.request(sender, 'GET', target)
        if action != 'view':
            return self.offer(sender, chat, source, view, action, bits[3] if len(bits) > 3 else None)
        if not previous:
            self.confirmations.put(source, sender, chat, 'view', {'view': view}, self.ttl)
        rows = []
        for name in view.get('allowed_actions', []):
            if name not in LABELS:
                continue
            row = self.confirmations.by_source(source + ':' + name, sender, chat)
            if row is None:
                payload = request_for(view, name)
                payload['review'] = review_text(view)
                row = self.confirmations.put(source + ':' + name, sender, chat, 'review', payload, self.ttl)
            rows.append([(LABELS[name] + ' 검토', 'ta:' + row['token'])])
        return review_text(view), inline_keyboard(rows) if rows else None

    def callback(self, token, sender, chat):
        row = self.confirmations.load(token, sender, chat)
        if row['kind'] == 'review':
            payload = json.loads(row['payload'])
            confirmation = self.confirmations.put('review:' + token, sender, chat, 'confirm', payload,
                                                  min(self.ttl, max(1, row['expires'] - self.confirmations.clock())))
            return self.confirm_card(confirmation)
        if row['kind'] != 'confirm':
            raise ValueError('실행 확인 버튼이 아닙니다.')
        row, execute = self.confirmations.claim(token, sender, chat)
        if not execute:
            return row['outcome'] or '이미 처리된 요청입니다.', None
        payload = json.loads(row['payload'])
        try:
            receipt = self.http.request(sender, 'POST', payload['target'], packed(payload['body']).encode())
            if receipt.get('idempotency_key') != payload['body']['idempotency_key']:
                raise ValueError('Receipt mismatch')
            outcome = '접수 확인: ' + LABELS[payload['action']] + '\n영수증 <code>' + safe(receipt['receipt_id'], 170) + '</code>'
            outcome += '\n/trader view ' + safe(payload['body']['improvement_ref']['object_id'], 170)
            self.confirmations.finish(token, 'DONE', outcome)
            return outcome, None
        except RemoteError as exc:
            if exc.status < 500:
                outcome = f'요청 거절: {safe(exc.code)}. 상세를 다시 검토해 주세요. 새 개정은 자동 승인하지 않습니다.'
                self.confirmations.finish(token, 'REFUSED', outcome)
                return outcome, None
        except (OSError, ValueError, TimeoutError):
            pass
        self.confirmations.finish(token, 'UNKNOWN', '응답 미확인')
        return ('서버 응답을 확인하지 못했습니다. 같은 요청·같은 키로만 다시 확인합니다. 최신 개정으로 바꾸지 않습니다.',
                inline_keyboard([[('동일 요청 결과 다시 확인', 'ta:' + token)]]))


def command(bot, message, update_id):
    if not bot._authorized_message(message):
        return
    try:
        handler = getattr(bot, 'trader_advanced', None) or TraderTelegram.load(bot.settings)
        bot.trader_advanced = handler
        text, markup = handler.command(message['text'], message['from']['id'], message['chat']['id'],
                                       'telegram:' + str(update_id))
    except (ValueError, KeyError, OSError, RemoteError, tomllib.TOMLDecodeError):
        text, markup = '트레이더 요청을 처리하지 못했습니다. 연결 설정·현재 권한과 상세 상태를 확인해 주세요.', None
    bot._send(text, reply_markup=markup)


def callback(bot, query):
    if not bot._authorized_update({'callback_query': query}):
        return
    try:
        handler = getattr(bot, 'trader_advanced', None) or TraderTelegram.load(bot.settings)
        bot.trader_advanced = handler
        text, markup = handler.callback(query['data'][3:], query['from']['id'], query['message']['chat']['id'])
    except ValueError as exc:
        text, markup = safe(exc, 300), None
    except (KeyError, OSError, RemoteError, tomllib.TOMLDecodeError):
        text, markup = '트레이더 연결을 확인하지 못했습니다. 잠시 후 동일 요청으로 확인해 주세요.', None
    bot._send(text, reply_markup=markup)

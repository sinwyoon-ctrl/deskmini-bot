"""External approval requests: other agents drop a file in agent-hub/approvals/, the owner decides with a button.

The request body is data. Nothing in it is executed or interpreted, and a typed "approve" never decides anything;
only the card buttons write the decision file (<ID>.decision.md), atomically.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from adapters.telegram import inline_keyboard
from app.redact import redact_text
from app.state import utc_now
from app.telegram_copy import local_time

ID_PATTERN = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}'
ID_RE = re.compile('^'+ID_PATTERN+'$')
MAX_BYTES = 20*1024
KST = timezone(timedelta(hours=9))
MESSAGE_RE = re.compile(r'^\s*(?:(?P<id>'+ID_PATTERN+r')\s+)?(?:승인|거절|approved?|reject(?:ed)?|ok|오케이)\s*(?:해줘|해|합니다|요)?[.!]?\s*$',re.I)
REPLY_ID_RE = re.compile(r'\[승인 요청\s*·?\s*(?P<id>'+ID_PATTERN+r')\]')
LABELS = {'approved':'승인','rejected':'거절','expired':'만료'}


def approvals_dir(bot):
    return Path(bot.paths.agent_hub)/'approvals'


def parse_header(raw):
    """Return (header dict, body) from '---' key: value '---' text, or (None, raw) when there is no header."""
    lines = raw.lstrip('﻿').splitlines()
    if not lines or lines[0].strip()!='---':
        return None,raw
    header = {}
    for index in range(1,len(lines)):
        line = lines[index]
        if line.strip()=='---':
            return header,'\n'.join(lines[index+1:]).strip()
        key,sep,value = line.partition(':')
        if sep and key.strip():
            header[key.strip().lower()] = value.strip()
    return None,raw


def _parse_time(value):
    try:
        stamp = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def write_decision(directory, approval_id, decision, now, by='telegram'):
    """Atomic write (temp file + os.replace) of <ID>.decision.md; raises OSError on failure."""
    directory = Path(directory)
    final = directory/(approval_id+'.decision.md')
    temp = directory/('.'+approval_id+'.decision.tmp')
    body = ('---\nid: '+approval_id+'\ndecision: '+decision+'\ndecided_at: '+now.astimezone(KST).isoformat(timespec='seconds')
            +'\nby: '+by+'\n---\n')
    try:
        with open(temp,'w',encoding='utf-8') as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp,final)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def read_decision(directory, approval_id):
    """The decision value written in <ID>.decision.md, or None when the file is missing or unreadable."""
    try:
        header,_ = parse_header((Path(directory)/(approval_id+'.decision.md')).read_text(encoding='utf-8',errors='replace'))
    except OSError:
        return None
    return (header or {}).get('decision') or 'unknown'


def _buttons(rowid):
    return inline_keyboard([[('✅ 승인','xa:%d:a'%rowid),('⛔ 거절','xa:%d:r'%rowid)]])


def _card(approval_id, requester, summary, body, expires_at):
    text = ('[승인 요청 · '+approval_id+']\n요청: '+(requester or '확인 필요')+'\n요약: '+redact_text(summary or '',500)
            +'\n\n'+redact_text(body or '',800)+'\n\n유효: '+local_time(expires_at)
            +' · 결정은 agent-hub approvals/'+approval_id+'.decision.md 에 기록됩니다')
    return html.escape(redact_text(text,4000))


def _notice(bot, text):
    bot._send(html.escape('[봇 안내] '+text))


def _bad(bot, name, reason):
    bot._notify('xapproval-bad:'+name,html.escape('[봇 안내] 승인 요청 파일 무시: '+name+' ('+reason+')'))


def scan(bot):
    directory = approvals_dir(bot)
    if not directory.is_dir():
        return
    now = bot.clock()
    for path in sorted(directory.glob('*.md')):
        if path.name.endswith('.decision.md'):
            continue
        name = path.name
        approval_id = path.name[:-3]
        if not ID_RE.match(approval_id):
            _bad(bot,name,'파일 이름 규칙 위반');continue
        try:
            size = path.stat().st_size
            if size>MAX_BYTES:
                _bad(bot,name,'20KB 초과');continue
            raw = path.read_bytes().decode('utf-8',errors='replace')
        except OSError:
            continue
        with bot.store.connect() as db:
            known = db.execute('SELECT 1 FROM external_approvals WHERE id=?',(approval_id,)).fetchone()
        if known:
            continue
        header,body = parse_header(raw)
        if not header or header.get('id')!=approval_id:
            _bad(bot,name,'id 헤더가 없거나 파일 이름과 다름');continue
        created = _parse_time(header.get('created',''))
        if created is None:
            created = datetime.fromtimestamp(path.stat().st_mtime,UTC)
        try:
            hours = min(max(int(header.get('expires_hours','24')),1),168)
        except ValueError:
            hours = 24
        expires = created+timedelta(hours=hours)
        existing = read_decision(directory,approval_id)
        state = 'DECIDED_EXTERNAL' if existing else 'PENDING'
        with bot.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO external_approvals(id,path,requester,summary,created_at,expires_at,state,decision,seen_at) VALUES(?,?,?,?,?,?,?,?,?)',
                       (approval_id,name,redact_text(header.get('from',''),200),redact_text(header.get('summary',''),500),
                        created.isoformat(timespec='seconds'),expires.isoformat(timespec='seconds'),state,existing,utc_now()))
            row = db.execute('SELECT rowid AS rid FROM external_approvals WHERE id=?',(approval_id,)).fetchone()
        if state=='PENDING' and expires>now:
            bot._notify('xapproval:'+approval_id,_card(approval_id,header.get('from',''),header.get('summary',''),body,expires.isoformat(timespec='seconds')),
                        reply_markup=_buttons(row['rid']))
    _expire(bot,directory,now)


def _expire(bot, directory, now):
    with bot.store.connect() as db:
        due = [r['id'] for r in db.execute("SELECT id,expires_at FROM external_approvals WHERE state='PENDING'") if (_parse_time(r['expires_at']) or now)<=now]
    for approval_id in due:
        existing = read_decision(directory,approval_id)
        if existing:
            with bot.store.transaction() as db:
                db.execute("UPDATE external_approvals SET state='DECIDED_EXTERNAL',decision=? WHERE id=? AND state='PENDING'",(existing,approval_id))
            continue
        try:
            write_decision(directory,approval_id,'expired',now)
        except OSError:
            continue
        with bot.store.transaction() as db:
            changed = db.execute("UPDATE external_approvals SET state='EXPIRED',decision='expired',decided_at=?,decided_source='bot:expiry' WHERE id=? AND state='PENDING'",
                                 (now.isoformat(timespec='seconds'),approval_id)).rowcount
        if changed:
            bot._notify('xapproval-expired:'+approval_id,html.escape('[봇 안내] 승인 요청 '+approval_id+' 유효시간 지남 → expired 기록'))


def handle_callback(bot, data):
    parts = data.split(':')
    if len(parts)!=3 or not parts[1].isdigit() or parts[2] not in {'a','r'}:
        return
    rowid,choice = int(parts[1]),parts[2]
    directory = approvals_dir(bot)
    now = bot.clock()
    with bot.store.connect() as db:
        row = db.execute('SELECT * FROM external_approvals WHERE rowid=?',(rowid,)).fetchone()
    if not row:
        _notice(bot,'이 승인 카드의 요청을 찾지 못했습니다');return
    approval_id = row['id']
    if row['state']!='PENDING':
        _notice(bot,'이미 처리된 승인 요청입니다: '+approval_id+' · '+(row['decision'] or row['state']));return
    if (_parse_time(row['expires_at']) or now)<=now:
        try:
            write_decision(directory,approval_id,'expired',now)
        except OSError:
            _notice(bot,'결정을 기록하지 못했습니다(쓰기 오류). 다시 눌러 주세요');return
        with bot.store.transaction() as db:
            db.execute("UPDATE external_approvals SET state='EXPIRED',decision='expired',decided_at=?,decided_source='telegram:callback' WHERE id=? AND state='PENDING'",
                       (now.isoformat(timespec='seconds'),approval_id))
        _notice(bot,'유효시간이 지나 승인하지 않았습니다');return
    other = read_decision(directory,approval_id)
    if other:
        with bot.store.transaction() as db:
            db.execute("UPDATE external_approvals SET state='DECIDED_EXTERNAL',decision=? WHERE id=? AND state='PENDING'",(other,approval_id))
        _notice(bot,'이미 처리된 승인 요청입니다: '+approval_id+' · '+other);return
    decision = 'approved' if choice=='a' else 'rejected'
    try:
        write_decision(directory,approval_id,decision,now)
    except OSError:
        _notice(bot,'결정을 기록하지 못했습니다(쓰기 오류). 다시 눌러 주세요');return
    with bot.store.transaction() as db:
        db.execute("UPDATE external_approvals SET state='DECIDED',decision=?,decided_at=?,decided_source='telegram:callback' WHERE id=? AND state='PENDING'",
                   (decision,now.isoformat(timespec='seconds'),approval_id))
    _notice(bot,'승인 요청 '+approval_id+' 를 '+LABELS[decision]+'했습니다. approvals/'+approval_id+'.decision.md 에 기록했고 요청한 쪽이 읽어 진행합니다(hub 동기화 최대 15분).')


def handle_message(bot, message, update_id):
    text = message.get('text','')
    match = MESSAGE_RE.match(text)
    if not match:
        return False
    approval_id = match['id']
    from_body = bool(approval_id)
    if not approval_id:
        found = REPLY_ID_RE.search((message.get('reply_to_message') or {}).get('text','') or '')
        approval_id = found['id'] if found else None
    if not approval_id:
        return False
    with bot.store.connect() as db:
        row = db.execute('SELECT rowid AS rid,* FROM external_approvals WHERE id=? COLLATE NOCASE',(approval_id,)).fetchone()
    if not row and from_body and not re.search(r'[0-9_.-]',approval_id):
        # A plain word before "ok" is ordinary chat, not a request ID.
        return False
    chat = bot.settings.allowed_chat_id
    bot.store.add_chat_message(chat,'user',text)
    key = 'xapproval-msg:'+str(update_id)
    if not row:
        bot._notify(key,html.escape("[봇 안내] '"+approval_id+"' 는 봇에 등록된 승인 요청이 아닙니다. 요청한 쪽이 ~/agent-hub/approvals/"+approval_id
                                    +'.md 로 보내면 승인·거절 버튼이 뜹니다. 이 문장은 어디에도 보내지 않았습니다.'))
        return True
    if row['state']=='PENDING':
        bot._notify('xapproval:'+row['id']+':again:'+str(update_id),
                    html.escape('[봇 안내] 글로 쓴 승인은 실행하지 않습니다. 아래 버튼으로 확정해 주세요.\n')
                    +'[승인 요청 · '+html.escape(row['id'])+']\n요약: '+html.escape(redact_text(row['summary'] or '',500)),
                    reply_markup=_buttons(row['rid']))
        return True
    bot._notify(key,html.escape('[봇 안내] 이미 처리된 승인 요청입니다: '+row['id']+' · '+(row['decision'] or row['state'])))
    return True


def _cli_request(args):
    if not ID_RE.match(args.id) or args.id.endswith('.decision'):
        print('invalid id',file=sys.stderr);return 1
    body = ''
    if args.body:
        body = sys.stdin.read() if args.body=='-' else Path(args.body).read_text(encoding='utf-8')
    one_line = lambda value: ' '.join(value.split())
    content = ('---\nid: '+args.id+'\nfrom: '+one_line(args.sender)+'\ncreated: '+datetime.now(KST).isoformat(timespec='seconds')
               +'\nexpires_hours: '+str(min(max(args.expires_hours,1),168))+'\nsummary: '+one_line(args.summary)+'\n---\n'+body.strip()+'\n')
    if len(content.encode('utf-8'))>MAX_BYTES:
        print('request larger than 20KB',file=sys.stderr);return 1
    directory = Path(args.hub)/'approvals'
    directory.mkdir(parents=True,exist_ok=True)
    final = directory/(args.id+'.md')
    temp = directory/('.'+args.id+'.request.tmp')
    temp.write_text(content,encoding='utf-8')
    try:
        os.link(temp,final)
    except FileExistsError:
        print('request already exists: '+args.id,file=sys.stderr);return 1
    finally:
        temp.unlink(missing_ok=True)
    print(final)
    return 0


def _cli_wait(args):
    directory = Path(args.hub)/'approvals'
    deadline = time.monotonic()+args.timeout
    while True:
        path = directory/(args.id+'.decision.md')
        if path.is_file():
            header,_ = parse_header(path.read_text(encoding='utf-8',errors='replace'))
            value = (header or {}).get('decision')
            if value in LABELS:
                print(args.id+' '+value+' '+(header.get('decided_at') or ''))
                return 0 if value=='approved' else 2
        if time.monotonic()>=deadline:
            print(args.id+' timeout')
            return 3
        time.sleep(min(args.interval,max(deadline-time.monotonic(),0)))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='external_approvals')
    sub = parser.add_subparsers(dest='command',required=True)
    request = sub.add_parser('request')
    request.add_argument('--hub',required=True)
    request.add_argument('--id',required=True)
    request.add_argument('--from',dest='sender',required=True)
    request.add_argument('--summary',required=True)
    request.add_argument('--body')
    request.add_argument('--expires-hours',type=int,default=24)
    wait = sub.add_parser('wait')
    wait.add_argument('--hub',required=True)
    wait.add_argument('--id',required=True)
    wait.add_argument('--timeout',type=float,default=900)
    wait.add_argument('--interval',type=float,default=10)
    args = parser.parse_args(argv)
    return _cli_request(args) if args.command=='request' else _cli_wait(args)


if __name__ == '__main__':
    sys.exit(main())

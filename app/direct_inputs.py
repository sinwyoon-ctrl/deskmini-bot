"""[✏️ 직접 입력]: the next plain text message becomes the answer to one waiting question (spec F-1).

One waiting record per chat (a new one replaces the old). Slash commands cancel it; buttons and attachments never consume it,
and it expires after ten minutes, after which the next message is handled as usual.
"""
from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta

from app import cards
from app.state import ConflictError, utc_now

TTL = timedelta(minutes=10)
NOTICE = '다음 답장을 이 질문의 답으로 받습니다(10분). 취소하려면 아무 명령이나 보내거나 10분을 기다려 주세요.'


def arm(bot, kind, key, message, *, source='callback'):
    """Record the wait and tell the owner. ``message`` is the callback's card message."""
    now = cards.now_of(bot)
    card = message.get('text') if isinstance(message, dict) else None
    message_id = message.get('message_id') if isinstance(message, dict) else None
    with bot.store.transaction() as db:
        db.execute('INSERT INTO telegram_direct_inputs(chat_id,kind,key,message_id,expires_at,source,created_at,card_text) VALUES(?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(chat_id) DO UPDATE SET kind=excluded.kind,key=excluded.key,message_id=excluded.message_id,'
                   'expires_at=excluded.expires_at,source=excluded.source,created_at=excluded.created_at,card_text=excluded.card_text',
                   (bot.settings.allowed_chat_id, kind, key, message_id, (now + TTL).isoformat(timespec='seconds'), source,
                    now.isoformat(timespec='seconds'), card))
    if cards.mark_decided(bot, message, cards.verdict_line('direct_wait', now), fallback=NOTICE) == 'edited':
        bot._send(html.escape('[봇 안내]\n' + NOTICE))


def pending(bot):
    chat = bot.settings.allowed_chat_id
    with bot.store.connect() as db:
        return db.execute('SELECT * FROM telegram_direct_inputs WHERE chat_id=?', (chat,)).fetchone()


def consume(bot, message, update_id):
    """True when this text message was taken as the waiting answer."""
    text = (message.get('text') or '').strip()
    if not text:
        return False
    if text.startswith('/'):
        # A command cancels the wait (as the notice promises) and is then processed normally.
        with bot.store.transaction() as db:
            db.execute('DELETE FROM telegram_direct_inputs WHERE chat_id=?', (bot.settings.allowed_chat_id,))
        return False
    row = pending(bot)
    if not row:
        return False
    chat = bot.settings.allowed_chat_id
    now = cards.now_of(bot)
    expires = datetime.fromisoformat(row['expires_at'])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)

    def drop():
        with bot.store.transaction() as db:
            db.execute('DELETE FROM telegram_direct_inputs WHERE chat_id=? AND key=? AND created_at=?', (chat, row['key'], row['created_at']))

    if expires <= now:
        drop()
        return False
    bot.store.add_chat_message(chat, 'user', text)
    reply = None
    try:
        if row['kind'] == 'codex':
            from app.codex_session import CodexSessions, load_config
            CodexSessions(bot.store, load_config(bot.settings.root)).answer(chat, row['key'], text)
        else:
            raise ConflictError('이 질문은 직접 입력으로 받을 수 없습니다.')
    except (ConflictError, ValueError) as exc:
        reply = str(exc)
    drop()
    if reply is not None:
        bot._send(html.escape('[봇 안내]\n직접 입력을 전달하지 않았습니다. ' + reply))
        return True
    if row['card_text'] and row['message_id']:
        done = {'message_id': row['message_id'], 'text': row['card_text']}
        cards.mark_decided(bot, done, cards.verdict_line('direct', now), fallback='직접 입력을 질문의 답으로 전달했습니다.')
        return True
    bot._send(html.escape('[봇 안내]\n직접 입력을 질문의 답으로 전달했습니다.'))
    return True

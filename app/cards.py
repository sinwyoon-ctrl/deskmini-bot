"""Decision cards shared by every Telegram prompt that needs a tap (spec F-1).

The functions build text and inline keyboards only; the one exception is ``mark_decided``, which edits the
original card after a tap (buttons removed, last line replaced by the verdict) and falls back to a short new
message when the edit is impossible (older than 48 hours, transport without edit support, no message id).

Callback data is never built here: callers pass their own (legacy-compatible) strings, 64 bytes or less.
"""
from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta, timezone

from adapters.telegram import TelegramError, inline_keyboard
from app.redact import redact_html, redact_text

KST = timezone(timedelta(hours=9))
TITLE_LIMIT = 60
IMPACT_TEXT = {'external': '외부 전송 있음', 'money': '비용 발생', 'irreversible': '되돌릴 수 없음'}
TWO_STEP = {'money', 'irreversible'}
VERDICTS = {'approved': '✅ 승인됨', 'rejected': '⛔ 거절됨', 'cancelled': '⛔ 취소됨', 'expired': '⌛ 만료',
            'direct_wait': '✅ 직접 입력 대기', 'direct': '✅ 직접 입력'}
MORE_LABEL = '더 보기 ▸'
BACK_LABEL = '◂ 이전'
OTHER_LABEL = '✏️ 직접 입력'
PAGE_SIZE = 7
NO_BUTTONS = {'inline_keyboard': []}


def stamp(value=None):
    """'10/20 21:03' in Korean time from a datetime, an ISO string, or now."""
    if value is None:
        value = datetime.now(UTC)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return '시각 확인 필요'
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(KST).strftime('%m/%d %H:%M')


def _one_line(text, limit):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def impact_line(impact):
    """'영향: …' line for impact keywords (str, or an iterable of free-form phrases). None when nothing to say."""
    if not impact:
        return None
    parts = [impact] if isinstance(impact, str) else list(impact)
    parts = [IMPACT_TEXT.get(p, p) for p in parts if p and p != 'none']
    return '영향: ' + ' · '.join(parts) if parts else None


def needs_two_step(impact):
    return impact in TWO_STEP


def _compose(sender, title, impact, body, expires, escape, middle=None):
    title = ' '.join(str(title or '').split())
    lines = [str(sender)] if sender else []
    lines.append(_one_line(title, TITLE_LIMIT))
    effect = impact_line(impact)
    if effect:
        lines.append(effect)
    if len(title) > TITLE_LIMIT:
        body = title + '\n' + (body or '')
    if middle:
        lines.append(middle)
    if body and body.strip():
        lines.append(str(body).strip())
    if expires:
        lines.append('⏳ ' + str(expires))
    text = redact_text('\n'.join(lines), 3900)
    return html.escape(text) if escape else text


def approval_card(sender, title, *, impact=None, body='', expires=None, escape=True):
    """[sender] / one-line question / 영향 line (only when there is one) / body / ⏳ last line."""
    return _compose(sender, title, impact, body, expires, escape)


def choice_card(sender, title, options, *, recommended=None, reason=None, impact=None, body='', expires=None, escape=True):
    """Like approval_card plus a numbered list. options: labels or (label, description); recommended is 1-based."""
    rows = []
    for number, option in enumerate(options, 1):
        label, description = (option if isinstance(option, (tuple, list)) else (option, ''))
        line = '%d) %s' % (number, label)
        if recommended == number:
            line += ' ★' + (' — ' + reason if reason else '')
        rows.append(line)
        if description:
            rows.append('   ' + str(description))
    return _compose(sender, title, impact, body, expires, escape, middle='\n'.join(rows))


def approval_buttons(yes, no, *, detail=None):  # yes may be None (deny-only card)
    """yes/no/detail are (label, callback_data)."""
    row = [button for button in (yes, no, detail) if button]
    return inline_keyboard([row])


def _short_labels(labels, count):
    return (labels is not None and 0 < count <= 5 and len(labels) >= count
            and all(len(str(l)) <= 8 for l in labels[:count])
            and sum(len(str(l)) + 4 for l in labels[:count]) <= 30)


def choice_buttons(count, callback, *, labels=None, recommended=None, other=None, page=0, more=None):
    """Number buttons. callback: 'cx:key:{n}' format string or callable(n)->data (n is 1-based).

    <=5 one row, 6-8 two rows of 4, 9+ first 7 per page plus [더 보기 ▸]/[◂ 이전] when ``more`` (callable(page)->data)
    is given (without it only the first 8 are shown). ``other`` is the callback of [✏️ 직접 입력].
    """
    make = callback if callable(callback) else (lambda n: callback.format(n=n))
    numbers = list(range(1, count + 1))
    nav = []
    if count > 8:
        if more is None:
            numbers = numbers[:8]
        else:
            start = page * PAGE_SIZE
            numbers = numbers[start:start + PAGE_SIZE]
            if page > 0:
                nav.append((BACK_LABEL, more(page - 1)))
            if start + PAGE_SIZE < count:
                nav.append((MORE_LABEL, more(page + 1)))
    short = _short_labels(labels, count) and count <= 5

    def button(n):
        text = str(n) + (' ★' if recommended == n else '')
        if short:
            text += ' · ' + str(labels[n - 1])
        return (text, make(n))

    buttons = [button(n) for n in numbers]
    rows = [buttons] if len(buttons) <= 5 else [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    if other:
        if len(buttons) <= 3 and rows:
            rows[-1].append((OTHER_LABEL, other))
        else:
            rows.append([(OTHER_LABEL, other)])
    if nav:
        rows.append(nav)
    return inline_keyboard(rows)


def verdict_line(kind, when=None, who='대표', *, detail=None):
    """'✅ 승인됨 · 10/20 21:03 · 대표'. kind: approved|rejected|cancelled|expired|direct_wait|direct, or free text."""
    head = VERDICTS.get(kind, str(kind))
    parts = [head + (' ' + detail if detail else ''), stamp(when)]
    if who:
        parts.append(who)
    return ' · '.join(parts)


def decided_text(original, verdict):
    """Replace the last '⏳ …' line of a card (plain text) with the verdict; append it when there is none."""
    lines = str(original or '').rstrip().split('\n')
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith('⏳'):
            lines[index] = verdict
            return '\n'.join(lines)
    return '\n'.join(lines + [verdict])


def now_of(bot):
    clock = getattr(bot, 'clock', None)
    return clock() if callable(clock) else datetime.now(UTC)


def edit_card(bot, message, text, reply_markup=None):
    """Edit the card in place. True when edited (or already identical); False when a new message is needed."""
    if not isinstance(message, dict) or not message.get('message_id') or not isinstance(message.get('text'), str):
        return False
    try:
        bot.telegram.edit_message_text(bot.settings.allowed_chat_id, int(message['message_id']),
                                       redact_html(text, 4096), reply_markup=reply_markup)
    except TelegramError as exc:
        return 'message is not modified' in str(exc).lower()
    except AttributeError:
        return False
    return True


def mark_decided(bot, message, verdict, *, fallback=None, prefix='[봇 안내]\n'):
    """After a tap: card text -> original with its last line replaced by ``verdict`` and no buttons.

    ``message`` is the callback's message. Returns 'edited', or 'sent' when a short notice had to be sent instead.
    """
    original = message.get('text') if isinstance(message, dict) else None
    if isinstance(original, str) and edit_card(bot, message, html.escape(decided_text(original, verdict)), NO_BUTTONS):
        return 'edited'
    bot._send(html.escape(prefix + (fallback or verdict)))
    return 'sent'

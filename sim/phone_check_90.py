"""Replay the real 10/16-10/20 phone sentences through the bot without Telegram, CLI or API.

Usage (isolated copy, production venv):
    cd ~/work/deskmini-bot-fable && XDG_RUNTIME_DIR=/run/user/$(id -u) \
        ~/deskmini-bot/.venv/bin/python sim/phone_check_90.py

Everything runs in a temporary home/state directory (tests.test_watch.BotFixture). Telegram is FakeTelegram,
the intent API is stubbed with the intent the production API actually returned for that sentence
(events.AGENT_ROUTE_INTERPRETATION, read-only), the Claude CLI usage probe is a fake runner. Nothing is sent.
The printed transcript is what the phone should show after the fable/telegram-90 change.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

import test_agent_routing as routing  # noqa: E402
from app.agent_conversation import process_one  # noqa: E402
from app.chat import ChatConfig, ChatResponder  # noqa: E402

# (sentence, intent the production API returned on 10/16-10/20, what the phone showed then)
REAL = [
    ('클로드야 지금 주간 쿼터 얼마나 남았어', None, '어느 프로젝트에서 작업할까요? (10/16 08:21, 수정 전 코드)'),
    ('자 클로드 텔레그램 연동 최적화 작업 계속 할까', 'ambiguous', '요청의 뜻이나 중간 해석 결과를 하나로 확인하지 못해 … 전달하지 않았습니다 (10/16 09:12)'),
    ('그래서 얼마나 남았어?', 'quota', 'Claude 사용 한도 5시간/주간 수치 (10/16 10:16, 정상)'),
    ('아니 코덱스 비즈아이원 얼마나 남았어', 'quota', 'Claude 사용 한도 — 코덱스를 물었는데 클로드 수치 (10/16 10:16)'),
    ('코덱스야 지금 쿼터 얼마나 남았어?', None, '코덱스 작업자에게 전달돼 "직접 조회할 수 없습니다" (10/16 08:21, 수정 전 코드)'),
    ('APRIL-READ-01 승인', 'ambiguous', '요청의 뜻이나 … 전달하지 않았습니다 (10/20 09:34, 코덱스 선택 4일 경과)'),
    ('코덱스 한테 전달해', 'ambiguous', '요청의 뜻이나 … 전달하지 않았습니다 (10/20 09:35)'),
    ('해제 승인해줘 코덱스', 'ambiguous', '요청의 뜻이나 … 전달하지 않았습니다 (10/20 11:16)'),
    ('해제 승인', 'ambiguous', '요청의 뜻이나 … 전달하지 않았습니다 (10/20 11:16)'),
]


class Replay:
    def __init__(self):
        f = routing.RoutingTests(); f.setUp(); self.f = f
        f.bot.chat_config = ChatConfig(semantic_routing=True)
        f.bot.chat = ChatResponder(f.bot.chat_config, env_path=f.settings.env_path, gemini=None)
        patch.object(f.bot.chat, '_api_key', return_value='placeholder').start()
        limits = {'primary': {'used_percent': 43, 'window_minutes': 10080, 'resets_at': datetime.now(UTC) + timedelta(days=3)},
                  'secondary': None, 'plan_type': 'prolite', 'limit_reached': None, 'observed_at': datetime.now(UTC)}
        patch('app.bot.latest_codex_limits', return_value=limits).start()
        self.seen = 0

    def api(self, intent):
        if intent is None:
            return patch.object(self.f.bot.chat.claude, 'complete', side_effect=AssertionError('API must not be called'))
        return patch.object(self.f.bot.chat.claude, 'complete',
                            return_value=SimpleNamespace(data={'intent': intent}, model='claude-sonnet-5-5', usage={}))

    def claude_cli(self, agent, prompt, cwd):
        return 'OK', {'five_hour': {'used_percent': 31, 'resets_at': None}, 'seven_day': {'used_percent': 9, 'resets_at': None}}

    def new_replies(self):
        items = self.f.tg.sent[self.seen:]; self.seen = len(self.f.tg.sent)
        out = []
        for item in items:
            text = (item.get('text') or '').strip()
            buttons = [b['text'] for row in (item.get('reply_markup') or {}).get('inline_keyboard', []) for b in row]
            out.append((text, buttons))
        return out

    def say(self, text, intent, reply=None):
        with self.api(intent):
            self.f.say(text, reply)

    def press(self, part):
        cards = [i for i, item in enumerate(self.f.tg.sent) if item.get('reply_markup')]
        data = [d for t, d in self.f.buttons(cards[-1]) if part in t][0]
        self.f.number += 1
        self.f.press(data, update_id=self.f.number + 500)

    def side_effects(self):
        r = self.f.rows
        return {'codex_messages': len(r("SELECT * FROM codex_commands WHERE op='message'")),
                'claude_requests': len(self.f.queue.rows()),
                'chat_jobs': [(j['agent'], j['operation'], j['state']) for j in r('SELECT * FROM telegram_conversation_jobs')],
                'focus': [x['agent'] for x in r('SELECT agent FROM telegram_agent_focus')]}


def show(title, replies, effects):
    print(f'\n### {title}')
    for text, buttons in replies:
        print('  봇 → ' + text.replace('\n', '\n        '))
        if buttons:
            print('        버튼: ' + ' | '.join(buttons))
    print('  부수효과:', effects)


def main():
    sim = Replay()
    print('# 실제 폰 문장 재생 — 수정 후 동작 (FakeTelegram, API/CLI 모의, 전송 없음)')
    print('# 수정 전 폰 화면은 운영 outbox 기록(읽기 전용)에서 옮겨 적었다.')

    # 1. Claude usage: fresh lookup then cached answer (10/16 10:16 flow).
    text, intent, before = REAL[0]
    sim.say(text, intent)
    process_one(sim.f.bot.store, sim.claude_cli); sim.f.bot._flush_outbox()
    show(f'{text}  (수정 전: {before})', sim.new_replies(), sim.side_effects())
    text, intent, before = REAL[2]
    sim.say(text, intent)
    show(f'{text}  (수정 전: {before})', sim.new_replies(), sim.side_effects())

    # 2. Codex usage asked while Claude is selected.
    text, intent, before = REAL[3]
    sim.say(text, intent)
    show(f'{text}  (수정 전: {before})', sim.new_replies(), sim.side_effects())

    # 3. Ambiguous sentence with Claude selected and no pending question.
    text, intent, before = REAL[1]
    sim.say(text, intent)
    show(f'{text}  (수정 전: {before})', sim.new_replies(), sim.side_effects())
    sim.press('대화로 묻기')
    show('  ↳ 버튼 「클로드에게 대화로 묻기」', sim.new_replies(), sim.side_effects())
    sim.press('대화로 묻기')
    show('  ↳ 같은 버튼 다시 누름', sim.new_replies(), sim.side_effects())

    # 4. Codex selected by name, then usage via the authoritative path (no API call).
    text, intent, before = REAL[4]
    sim.say(text, intent)
    show(f'{text}  (수정 전: {before})', sim.new_replies(), sim.side_effects())

    # 5. Four days later: the 10/20 sentences. Selection is stale (inbox backdated 96h).
    stamp = (datetime.now(UTC) - timedelta(hours=96)).isoformat(timespec='seconds')
    with sim.f.bot.store.connect() as db:
        db.execute('UPDATE inbox SET received_at=?', (stamp,))
    with sim.api('work'):
        sim.f.say('코덱스야 README를 다시 확인해줘')
    with sim.f.bot.store.connect() as db:
        db.execute('UPDATE inbox SET received_at=?', (stamp,))
    sim.new_replies()
    # An ID followed by 승인 is an approval attempt: it ends in a notice (not a registered request), never an ask card.
    text, intent, before = REAL[5]
    sim.say(text, intent, reply='…독립 테스트는 성공했습니다.\n\n[승인 요청 APRIL-READ-01]\n추가 브라우저 조사는 …')
    show(f'{text} (알림 메시지에 답장)  (수정 전: {before})', sim.new_replies(), sim.side_effects())
    text, intent, before = REAL[6]
    sim.say(text, intent)
    show(f'{text}  (수정 전: {before})', sim.new_replies(), sim.side_effects())
    sim.press('작업에 지시로')
    show('  ↳ 버튼 「코덱스 작업에 지시로 보내기」', sim.new_replies(), sim.side_effects())
    text, intent, before = REAL[7]
    sim.say(text, intent)
    show(f'{text}  (수정 전: {before})', sim.new_replies(), sim.side_effects())
    text, intent, before = REAL[8]
    sim.say(text, intent)
    show(f'{text}  (수정 전: {before})', sim.new_replies(), sim.side_effects())

    # 6. Plain greeting after a lapsed selection goes to the assistant, not to Codex.
    with patch.object(sim.f.bot, '_chat_reply') as chat:
        sim.say('안녕 오늘 뭐 할까', 'conversation')
    show('안녕 오늘 뭐 할까 (코덱스 선택 4일 경과)  (수정 전: 코덱스 읽기 전용 대화로 전달)',
         sim.new_replies() + [('(비서 대화층 호출: ' + str(chat.called) + ')', [])], sim.side_effects())
    sim.f.tearDown()


if __name__ == '__main__':
    main()

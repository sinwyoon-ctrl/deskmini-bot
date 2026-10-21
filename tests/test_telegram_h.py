"""Spec H: natural wording, one-line footers that keep reply anchors, and card updates instead of extra messages."""
import asyncio
import html
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_agent_routing as routing
from adapters.telegram import TelegramError
from app.agent_routing import plan_input
from app.orca_bridge import OrcaBridge
from app.state import utc_now
from app.telegram_copy import COPY, activity_line, choose_text, message_card, meta_footer, state_label
from test_telegram_90 import SemanticFixture


class WordingTests(SemanticFixture):
    def test_choose_wording_without_and_with_target(self):
        self.assertEqual(choose_text('클로드',False),'클로드로 바꿨어요. 이어서 말씀하세요. 작업을 시키려면 프로젝트와 할 일을 알려 주세요.')
        self.assertEqual(choose_text('클로드',True),'클로드로 바꿨어요. 이어서 말씀하세요. 같은 작업으로 이어가요.')
        with self.answer('work'):
            self.f.say('클로드야')
        self.assertIn(choose_text('클로드',False),self.last())
        with self.answer('work'):
            self.f.claude()
        with self.answer('work'):
            self.f.say('클로드야')
        self.assertIn(choose_text('클로드',True),self.last())
        self.assertNotIn(COPY['choose_no_target'],self.last())

    def test_blocked_wording_adds_pending_question_line(self):
        with self.answer('work'):
            row=self.f.claude()
        self.f.question(row)
        with patch.object(self.f.bot.chat.claude,'complete',side_effect=RuntimeError('down')):
            self.f.say('클로드야 텔레그램 봇 README 수정해줘')
        text=self.last()
        lines=text.split('\n')
        self.assertIn(COPY['blocked'],text)
        self.assertTrue(any(l.startswith(COPY['blocked_waiting']+'alpha 또는 beta') for l in lines),text)
        self.assertIn(COPY['blocked_decision'],text)
        self.assertIn('답장',text)
        self.assertEqual(self.f.rows('SELECT state FROM orca_questions')[0]['state'],'PENDING')

    def test_blocked_without_question_has_no_question_line(self):
        with patch.object(self.f.bot.chat.claude,'complete',side_effect=RuntimeError('down')):
            self.f.say('클로드야 텔레그램 봇 README 수정해줘')
        self.assertNotIn(COPY['blocked_waiting'],self.last())
        self.assertEqual(self.f.queue.rows(),[])

    def test_receipts_are_short_and_keep_task_number(self):
        with self.answer('work'):
            self.f.say('클로드야 텔레그램 봇 README를 요약해줘')
        row=self.f.queue.rows()[-1]
        text=self.last()
        self.assertIn(COPY['claude_received'],text)
        self.assertTrue(text.rstrip().endswith('작업 번호: '+row['id']))
        self.assertLessEqual(len(text.split('\n')),3)

    def test_describe_activity_is_one_line_and_omitted_when_same_as_state(self):
        f=self.f
        f.bridge.activity({'id':f.key},'실행할 명령을 준비하고 있습니다.','turn-x')
        text=f.codex.describe(100)
        self.assertNotIn('마지막으로 확인한 동작',text)
        self.assertNotIn('동작 확인:',text)
        lines=[l for l in text.split('\n') if l.startswith('실행할 명령을 준비하고 있습니다.')]
        self.assertEqual(len(lines),1)
        self.assertRegex(lines[0],r' \(.* 확인\)$')
        f.bridge.activity({'id':f.key},state_label('IDLE'),None)
        self.assertNotIn(' 확인)',f.codex.describe(100))
        self.assertEqual(activity_line('같음','2026-10-20T00:00:00+00:00','같음'),'')


class Routed(unittest.TestCase):
    """Wraps RoutingTests' fixture without inheriting (and re-running) its tests."""
    def setUp(self):
        f=routing.RoutingTests();f.setUp();self.f=f
        self.addCleanup(f.tearDown);self.addCleanup(f.doCleanups)

    def __getattr__(self,name):
        if name=='f':
            raise AttributeError(name)
        return getattr(self.f,name)


class FooterTests(Routed):
    def test_codex_question_footer_keeps_anchor_then_small_meta_line(self):
        self.bridge.update(self.key,turn_id='turn-c',state='RUNNING')
        self.bridge.question(self.rows('SELECT * FROM codex_sessions WHERE id=?',self.key)[0],{'id':1,'method':'item/tool/requestUserInput','params':{
            'turnId':'turn-c','questions':[{'id':'name','header':'이름','question':'어느 이름?','options':[]}]}})
        q=self.rows('SELECT * FROM codex_questions')[0]
        text=self.bot.store.pending_outbox()[-1]['payload']['text']
        lines=text.rstrip().split('\n')
        self.assertEqual(lines[-2],'확인 번호: '+q['id'])
        self.assertEqual(lines[-1],'· 대화 '+self.key)
        self.assertNotIn('대화 번호:',text)
        self.bot._flush_outbox()
        self.say('gamma',text)
        self.assertEqual(self.rows('SELECT state FROM codex_questions')[0]['state'],'ANSWERING')

    def test_orca_question_footer_keeps_anchor_then_task_number(self):
        row=self.claude()
        OrcaBridge(self.queue,client=object()).notify(row,'question:m1','alpha 또는 beta?',question_id='msg-native',model=True)
        text=self.bot.store.pending_outbox()[-1]['payload']['text']
        lines=text.rstrip().split('\n')
        self.assertEqual(lines[-2],'질문 번호: msg-native')
        self.assertEqual(lines[-1],'작업 번호: '+row['id'])
        with self.bot.store.connect() as db:
            db.execute('INSERT INTO orca_questions(message_id,request_id,body) VALUES(?,?,?)',('msg-native',row['id'],'alpha 또는 beta?'))
        self.queue.update(row['id'],state='ATTENTION')
        self.say('gamma로 써줘',text)
        self.assertEqual(self.rows('SELECT reply_text FROM orca_questions')[0]['reply_text'],'gamma로 써줘')

    def test_card_without_anchor_has_only_the_meta_line(self):
        card=message_card('[봇 안내 · 코덱스]','본문',meta_footer('대화','c-1'))
        self.assertEqual(card,'[봇 안내 · 코덱스]\n본문\n\n· 대화 c-1')
        self.assertTrue(message_card('[코덱스]','발언',meta_footer('대화','c-1')).startswith('[코덱스]\n\n발언'))


class CardUpdateTests(Routed):
    """접수 -> started -> completed: either one edited card that says it is done, or three separate messages."""

    def flow(self,*,repeat=False,after_say=None):
        before=len(self.tg.sent)
        self.say('코덱스야, README를 다시 확인해줘')
        command=self.rows("SELECT * FROM codex_commands WHERE op='message' ORDER BY rowid DESC")[0]
        self.assertEqual(len(self.tg.sent)-before,1)
        if after_say:
            after_say()
        with self.bot.store.connect() as db:
            db.execute("UPDATE codex_commands SET state='ACKED' WHERE id=?",(command['id'],))
        for _ in range(2 if repeat else 1):
            asyncio.run(self.bridge.event({'method':'turn/started','params':{'threadId':'codex-thread','turn':{'id':'turn-h','status':'inProgress'}}}))
            self.bot._flush_outbox()
            asyncio.run(self.bridge.event({'method':'turn/completed','params':{'threadId':'codex-thread','turn':{'id':'turn-h','status':'completed'}}}))
            self.bot._flush_outbox()
        return before,command

    def visible(self,before):
        sent=[m['text'] for m in self.tg.sent[before:]]
        edits=getattr(self.tg,'edited',[])
        return sent,edits

    def test_card_is_edited_in_place(self):
        before,command=self.flow()
        sent,edits=self.visible(before)
        self.assertEqual(len(sent),1)
        self.assertEqual(len(edits),2)
        self.assertEqual({e['message_id'] for e in edits},{before+1})
        self.assertIn(COPY['codex_working'],html.unescape(edits[0]['text']))
        self.assertIn('끝났어요',html.unescape(edits[-1]['text']))
        self.assertIn('· 대화 '+self.key,html.unescape(edits[-1]['text']))
        card=self.rows('SELECT * FROM telegram_cards WHERE dedupe_key=?','agent-route:'+command['source'])[0]
        self.assertEqual((card['chat_id'],card['message_id'],card['kind']),(100,before+1,'turn:turn-h'))
        self.assertEqual(self.rows("SELECT count(*) AS n FROM outbox WHERE state!='SENT'")[0]['n'],0)

    def test_rerunning_the_same_notices_adds_nothing(self):
        before,_=self.flow(repeat=True)
        sent,edits=self.visible(before)
        self.assertEqual((len(sent),len(edits)),(1,2))
        self.bot._flush_outbox()
        sent,edits=self.visible(before)
        self.assertEqual((len(sent),len(edits)),(1,2))

    def test_edit_failure_falls_back_to_three_messages(self):
        def refuse(*args,**kwargs):
            raise TelegramError('Bad Request: message to edit not found',error_code=400)
        self.tg.edit_message_text=refuse
        before,_=self.flow(repeat=True)
        sent,_=self.visible(before)
        self.assertEqual(len(sent),3)
        self.assertIn(COPY['codex_started'],sent[1])
        self.assertIn('끝났어요',sent[2])
        self.assertEqual(getattr(self.tg,'edited',[]),[])

    def test_missing_card_falls_back_to_three_messages(self):
        with patch.object(self.bot.store,'remember_card'):
            before,_=self.flow()
        sent,edits=self.visible(before)
        self.assertEqual((len(sent),len(edits)),(3,0))
        self.assertIn('끝났어요',sent[-1])

    def test_old_card_is_not_edited(self):
        with patch.object(self.bot.store,'find_card',return_value=None):
            before,_=self.flow()
        sent,edits=self.visible(before)
        self.assertEqual((len(sent),len(edits)),(3,0))

    def test_never_zero_never_more_than_three_when_edit_dies_midway(self):
        calls=[]
        def first_only(chat_id,message_id,text,reply_markup=None,parse_mode='HTML'):
            calls.append(text)
            if len(calls)>1:
                raise TelegramError('Bad Request: message can\'t be edited',error_code=400)
            return {'message_id':message_id}
        self.tg.edit_message_text=first_only
        before,_=self.flow(repeat=True)
        sent=[m['text'] for m in self.tg.sent[before:]]
        done=[t for t in sent if '끝났어요' in t]
        self.assertEqual(len(done),1)
        self.assertEqual(len(sent),2)  # receipt (edited to 작업 중) + completed notice sent as a new message
        self.assertTrue(1<=len(sent)+len(calls)-1<=3)

    def test_card_older_than_48_hours_is_not_edited(self):
        def age():
            with self.bot.store.connect() as db:
                db.execute("UPDATE telegram_cards SET created_at=?",((datetime.now(timezone.utc)-timedelta(hours=48)).isoformat(timespec='seconds'),))
        before,_=self.flow(after_say=age)
        sent,edits=self.visible(before)
        self.assertEqual((len(sent),len(edits)),(3,0))

    def test_transport_without_edit_support_falls_back_to_three_messages(self):
        with patch.object(type(self.tg),'edit_message_text',side_effect=AttributeError('no edit'),create=True):
            before,_=self.flow()
        sent,_=self.visible(before)
        self.assertEqual(len(sent),3)
        self.assertEqual(len([t for t in sent if '끝났어요' in t]),1)

    def test_turn_started_by_question_answer_does_not_edit_older_receipt_card(self):
        def refuse(*args,**kwargs):
            raise TelegramError('Bad Request: message to edit not found',error_code=400)
        self.tg.edit_message_text=refuse
        before,command=self.flow()
        first=self.rows('SELECT * FROM telegram_cards WHERE dedupe_key=?','agent-route:'+command['source'])[0]['message_id']
        with self.bot.store.connect() as db:
            db.execute("INSERT INTO codex_commands VALUES (?,?,?,?,?,?,?,?,?)",
                       ('cc-answer','answer:q-1',self.key,'message','네',None,'ACKED','',utc_now()))
        edited=[]
        def record(chat_id,message_id,text,reply_markup=None,parse_mode='HTML'):
            edited.append(message_id);return {'message_id':message_id}
        self.tg.edit_message_text=record
        mark=len(self.tg.sent)
        asyncio.run(self.bridge.event({'method':'turn/started','params':{'threadId':'codex-thread','turn':{'id':'turn-answer','status':'inProgress'}}}))
        self.bot._flush_outbox()
        self.assertNotIn(first,edited)
        started=[m['text'] for m in self.tg.sent[mark:] if COPY['codex_started'] in html.unescape(m['text'])]
        self.assertEqual(len(started),1)

    def test_edit_not_modified_counts_as_done(self):
        def same(*args,**kwargs):
            raise TelegramError('Bad Request: message is not modified',error_code=400)
        self.tg.edit_message_text=same
        before,_=self.flow()
        sent,_=self.visible(before)
        self.assertEqual(len(sent),1)


if __name__=='__main__':
    unittest.main()

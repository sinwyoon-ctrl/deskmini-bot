"""Telegram 90% round (2026-10-20): ask instead of refuse, lapsed selection, named usage lookups, vocative names.

Grounded in the production routing log of 10/16-10/20: "자 클로드 텔레그램 연동 최적화 작업 계속 할까",
"APRIL-READ-01 승인" (reply to a notify message), "코덱스 한테 전달해", "해제 승인해줘 코덱스" were all refused
with the same sentence, and "아니 코덱스 비즈아이원 얼마나 남았어" returned Claude's usage while Claude was selected.
"""
import re
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_agent_routing as routing
from app.agent_router import interpret
from app.telegram_copy import COPY
from app.agent_routing import address, named_agents
from app.chat import ChatConfig, ChatResponder


class SemanticFixture(unittest.TestCase):
    def setUp(self):
        f=routing.RoutingTests();f.setUp();self.f=f
        self.addCleanup(f.tearDown);self.addCleanup(f.doCleanups)
        f.bot.chat_config=ChatConfig(semantic_routing=True)
        f.bot.chat=ChatResponder(f.bot.chat_config,env_path=f.settings.env_path,gemini=None)
        p=patch.object(f.bot.chat,'_api_key',return_value='placeholder');p.start();self.addCleanup(p.stop)
        limits={'primary':{'used_percent':40,'window_minutes':10080,'resets_at':datetime.now(UTC)+timedelta(days=3)},
                'secondary':None,'plan_type':'test','limit_reached':None,'observed_at':datetime.now(UTC)}
        p=patch('app.bot.latest_codex_limits',return_value=limits);p.start();self.addCleanup(p.stop)

    def answer(self,intent,**kw):
        if kw:return patch.object(self.f.bot.chat.claude,'complete',**kw)
        return patch.object(self.f.bot.chat.claude,'complete',return_value=SimpleNamespace(data={'intent':intent},model='tested-model',usage={}))

    def last(self):
        return self.f.tg.sent[-1]['text']

    def buttons(self):
        return self.f.buttons()

    def options(self):
        """Labels of the numbered list of the last card (buttons carry numbers only)."""
        return [l for l in self.last().split('\n') if re.match(r'^\d\)',l)]

    def press(self,label_part):
        """Tap a button on the most recent message that carried buttons (later plain replies do not clear it)."""
        cards=[i for i,item in enumerate(self.f.tg.sent) if item.get('reply_markup')]
        self.assertTrue(cards,'no card with buttons was sent')
        numbers=[l.split(')')[0] for l in self.f.tg.sent[cards[-1]]['text'].split('\n') if re.match(r'^\d\)',l) and label_part in l]
        self.assertEqual(len(numbers),1,self.f.tg.sent[cards[-1]]['text'])
        data=[d for t,d in self.f.buttons(cards[-1]) if t.split()[0]==numbers[0]]
        self.assertEqual(len(data),1,self.f.buttons(cards[-1]))
        self.f.number+=1
        self.f.press(data[0],update_id=self.f.number+500)

    def select_codex(self):
        with self.answer('work'):
            self.f.say('코덱스야 README를 다시 확인해줘')
        self.assertEqual(self.f.active(),'codex')

    def backdate_inputs(self,hours):
        stamp=(datetime.now(UTC)-timedelta(hours=hours)).isoformat(timespec='seconds')
        with self.f.bot.store.connect() as db:
            db.execute('UPDATE inbox SET received_at=?',(stamp,))

    def jobs(self):
        return self.f.rows('SELECT * FROM telegram_conversation_jobs')

    def codex_messages(self):
        return self.f.rows("SELECT * FROM codex_commands WHERE op='message'")


class AskInsteadOfRefuseTests(SemanticFixture):
    def test_real_phone_sentence_gets_choice_card_and_nothing_is_sent(self):
        with self.answer('conversation'):
            self.f.say('클로드야')
        with self.answer('ambiguous'):
            self.f.say('자 클로드 텔레그램 연동 최적화 작업 계속 할까')
        self.assertIn('어디로 보낼까요',self.last())
        self.assertNotIn('전달하지 않았습니다',self.last())
        labels=self.options()
        self.assertEqual(len(labels),3)
        self.assertEqual([t for t,_ in self.buttons()],['1','2','3'])
        self.assertTrue(any('작업에 지시로' in t for t in labels))
        self.assertTrue(any('대화로 묻기' in t for t in labels))
        self.assertTrue(any('비서' in t for t in labels))
        self.assertEqual(self.f.queue.rows(),[])
        self.assertEqual(self.jobs(),[])

    def test_chat_choice_runs_read_only_conversation_once(self):
        with self.answer('conversation'):
            self.f.say('클로드야')
        with self.answer('ambiguous'):
            self.f.say('자 클로드 텔레그램 연동 최적화 작업 계속 할까')
        self.press('대화로 묻기')
        jobs=self.jobs()
        self.assertEqual(len(jobs),1)
        self.assertEqual((jobs[0]['agent'],jobs[0]['operation']),('claude','chat'))
        self.assertIn('텔레그램 연동 최적화',jobs[0]['body'])
        self.assertIn(COPY['chat_pending'],self.last())
        self.press('대화로 묻기')
        self.assertEqual(len(self.jobs()),1)
        self.assertIn('이미 처리한 선택',self.last())

    def test_work_choice_sends_saved_text_to_connected_codex_session(self):
        self.select_codex()
        before=len(self.codex_messages())
        with self.answer('ambiguous'):
            self.f.say('어제 말한 그 건 처리')
        self.assertEqual(len(self.codex_messages()),before)
        self.assertIn('어디로 보낼까요',self.last())
        self.press('작업에 지시로')
        messages=self.codex_messages()
        self.assertEqual(len(messages),before+1)
        self.assertIn('어제 말한 그 건 처리',messages[-1]['body'])
        self.assertIn(COPY['codex_received'],self.last())
        # Telegram may redeliver the tap; the saved message is not sent twice.
        self.press('작업에 지시로')
        self.assertEqual(len(self.codex_messages()),before+1)

    def test_assistant_choice_hands_the_text_to_the_assistant_layer(self):
        self.select_codex()
        with self.answer('ambiguous'):
            self.f.say('해제 승인')
        with patch.object(self.f.bot,'_chat_reply') as chat:
            self.press('비서')
        chat.assert_called_once()
        self.assertEqual(chat.call_args.args[0],'해제 승인')
        self.assertEqual(self.jobs(),[])

    def test_pending_question_offers_answer_choice_but_never_answers_by_itself(self):
        with self.answer('work'):row=self.f.claude()
        self.f.question(row)
        with self.answer('ambiguous'):
            self.f.say('그냥 그걸로')
        self.assertEqual(self.f.rows('SELECT state FROM orca_questions')[0]['state'],'PENDING')
        self.assertIn('alpha 또는 beta',self.last())
        labels=self.options()
        self.assertTrue(any('답변으로 보내기' in t for t in labels))
        self.assertFalse(any('작업에 지시로' in t for t in labels))
        self.press('답변으로 보내기')
        question=self.f.rows('SELECT * FROM orca_questions')[0]
        self.assertEqual((question['state'],question['reply_text']),('ANSWERING','그냥 그걸로'))

    def test_card_from_wrong_chat_or_unknown_row_is_ignored(self):
        self.select_codex()
        with self.answer('ambiguous'):
            self.f.say('어제 말한 그 건 처리')
        before=len(self.codex_messages())
        self.f.press('ar:999999:work',update_id=9001)
        self.assertIn('찾지 못했습니다',self.last())
        self.f.press('ar:abc:work',update_id=9002)
        self.assertEqual(len(self.codex_messages()),before)

    def test_api_failure_still_refuses_work_with_actionable_text(self):
        with patch.object(self.f.bot.chat.claude,'complete',side_effect=RuntimeError('unavailable')):
            self.f.say('클로드야 텔레그램 봇 README 수정해줘')
        self.assertEqual(self.f.queue.rows(),[])
        self.assertIn(COPY['blocked'],self.last())
        self.assertEqual(self.f.tg.sent[-1].get('reply_markup'),None)


class LapsedSelectionTests(SemanticFixture):
    def test_unnamed_chat_after_ttl_goes_back_to_assistant(self):
        self.select_codex()
        self.backdate_inputs(20)
        with self.answer('conversation'),patch.object(self.f.bot,'_chat_reply') as chat:
            self.f.say('안녕 오늘 뭐할까')
        chat.assert_called_once()
        self.assertEqual(self.jobs(),[])
        self.assertEqual(self.f.active(),'codex')

    def test_unnamed_chat_within_ttl_stays_with_selected_model(self):
        self.select_codex()
        with self.answer('conversation'),patch.object(self.f.bot,'_chat_reply') as chat:
            self.f.say('안녕 오늘 뭐할까')
        chat.assert_not_called()
        self.assertEqual([(j['agent'],j['operation']) for j in self.jobs()],[('codex','chat')])

    def test_ttl_zero_restores_previous_behaviour(self):
        self.f.bot.chat_config=replace(self.f.bot.chat_config,agent_focus_ttl_hours=0)
        self.select_codex()
        self.backdate_inputs(200)
        with self.answer('conversation'),patch.object(self.f.bot,'_chat_reply') as chat:
            self.f.say('안녕 오늘 뭐할까')
        chat.assert_not_called()
        self.assertEqual(len(self.jobs()),1)

    def test_work_after_ttl_still_reaches_the_selected_model(self):
        self.select_codex()
        self.backdate_inputs(20)
        before=len(self.codex_messages())
        with self.answer('work'):
            self.f.say('README 마지막 절을 다시 확인해줘')
        self.assertEqual(len(self.codex_messages()),before+1)

    def test_unclear_after_ttl_asks_with_assistant_first(self):
        self.select_codex()
        self.backdate_inputs(20)
        with self.answer('ambiguous'):
            self.f.say('해제 승인')
        self.assertIn('시간이 지났습니다',self.last())
        self.assertIn('비서',self.options()[0])

    def test_asks_and_clarifications_do_not_refresh_the_selection(self):
        self.select_codex()
        self.backdate_inputs(20)
        with self.answer('ambiguous'):
            self.f.say('해제 승인')
        with self.answer('conversation'),patch.object(self.f.bot,'_chat_reply') as chat:
            self.f.say('고마워')
        chat.assert_called_once()

    def test_choosing_to_send_from_an_ask_card_refreshes_the_selection(self):
        self.select_codex()
        self.backdate_inputs(20)
        with self.answer('ambiguous'):
            self.f.say('해제 승인')
        self.press('작업에 지시로')
        with self.f.bot.store.connect() as db:
            # Only the original message is old; the tap just happened.
            db.execute("UPDATE inbox SET received_at=? WHERE update_id=?",(datetime.now(UTC).isoformat(timespec='seconds'),self.f.number))
        with self.answer('conversation'),patch.object(self.f.bot,'_chat_reply') as chat:
            self.f.say('고마워')
        chat.assert_not_called()
        self.assertEqual([(j['agent'],j['operation']) for j in self.jobs()],[('codex','chat')])

    def test_config_reads_ttl_hours(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'models.toml'
            path.write_text('[chat]\nsemantic_routing = true\nagent_focus_ttl_hours = 6\n',encoding='utf-8')
            self.assertEqual(ChatConfig.load(path).agent_focus_ttl_hours,6.0)
            path.write_text('[chat]\nagent_focus_ttl_hours = -3\n',encoding='utf-8')
            self.assertEqual(ChatConfig.load(path).agent_focus_ttl_hours,0.0)
            path.write_text('[chat]\n',encoding='utf-8')
            self.assertEqual(ChatConfig.load(path).agent_focus_ttl_hours,12.0)


class NamedUsageAndVocativeTests(SemanticFixture):
    def test_usage_question_naming_the_other_model_reads_that_model(self):
        with self.answer('conversation'):
            self.f.say('클로드야')
        with self.answer('quota'):
            self.f.say('아니 코덱스 비즈아이원 얼마나 남았어')
        self.assertIn('Codex 사용 한도',self.last())
        self.assertNotIn('Claude 사용 한도',self.last())
        self.assertEqual(self.f.active(),'claude')
        self.assertEqual(self.jobs(),[])

    def test_usage_question_naming_claude_while_codex_selected_queues_claude_lookup(self):
        self.select_codex()
        with self.answer('quota') as call:
            self.f.say('그럼 클로드 쿼터는 얼마나 남았어')
        call.assert_not_called()
        jobs=self.jobs()
        self.assertEqual([(j['agent'],j['operation']) for j in jobs],[('claude','quota')])
        self.assertEqual(self.f.active(),'codex')

    def test_usage_question_without_a_name_uses_the_selected_model(self):
        self.select_codex()
        self.f.say('지금 쿼터 얼마나 남았어?')
        self.assertIn('Codex 사용 한도',self.last())

    def test_named_agents(self):
        self.assertEqual(named_agents('아니 코덱스 비즈아이원 얼마나 남았어'),{'codex'})
        self.assertEqual(named_agents('Claude 랑 codex 둘 다'),{'claude','codex'})
        self.assertEqual(named_agents('쿼터 얼마나 남았어'),set())

    def test_particle_after_space_and_trailing_name_select_the_model(self):
        self.assertEqual(address('코덱스 한테 전달해'),('codex','전달해'))
        self.assertEqual(address('코덱스한테 전달해'),('codex','전달해'))
        self.assertEqual(address('해제 승인해줘 코덱스'),('codex','해제 승인해줘'))
        self.assertEqual(address('이거 어떻게 생각해? 클로드'),('claude','이거 어떻게 생각해?'))
        self.assertEqual(address('코덱스 로그 읽어줘'),('codex','로그 읽어줘'))
        self.assertEqual(address('코덱스로 README 읽어줘'),('codex','README 읽어줘'))
        self.assertIsNone(address('README 읽어줘'))
        self.assertIsNone(address('그냥 그걸로'))
        # A name at the start is an explicit call and still selects that model.
        self.assertEqual(address('클로드 쿼터는 얼마나 남았어'),('claude','쿼터는 얼마나 남았어'))

    def test_trailing_name_routes_through_agent_selection(self):
        with self.answer('ambiguous'):
            self.f.say('해제 승인해줘 코덱스')
        self.assertEqual(self.f.active(),'codex')
        self.assertIn('[봇 안내 · 코덱스]',self.last())
        self.assertIn('어디로 보낼까요',self.last())


class InterpretTests(SemanticFixture):
    def test_ambiguous_api_result_is_unclear_not_blocked(self):
        with self.answer('ambiguous'):
            self.assertEqual(interpret(self.f.bot,'그냥 그걸로','claude',False),'unclear')
            self.assertEqual(interpret(self.f.bot,'그냥 그걸로','claude',True),'unclear')

    def test_mixed_status_sentence_is_still_a_read_only_status_query(self):
        with self.answer('ambiguous'):
            self.assertEqual(interpret(self.f.bot,'지금 뭐 하고 있어? 그리고 alpha 말고 gamma로 해','claude',True),'ambiguous')

    def test_api_failure_keeps_blocking_work_shaped_text(self):
        with patch.object(self.f.bot.chat.claude,'complete',side_effect=RuntimeError('down')):
            self.assertEqual(interpret(self.f.bot,'README 수정해줘','claude',False),'blocked')
            self.assertEqual(interpret(self.f.bot,'안녕','claude',False),'conversation')


class NameOnlyTests(SemanticFixture):
    """Spec G-1: a bare name selects the model without any API call."""

    def check(self,text,agent,**kw):
        with self.answer('work',**kw) as call:
            self.f.say(text)
        call.assert_not_called()
        self.assertEqual(self.f.active(),agent)
        self.assertIn(COPY['choose'].format(agent={'claude':'클로드','codex':'코덱스'}[agent]),self.last())
        self.assertNotIn(COPY['blocked'],self.last())
        self.assertEqual(self.jobs(),[])

    def test_bare_names_select_without_api(self):
        self.check('클로드야','claude')
        self.check('코덱스야','codex')

    def test_selection_survives_a_dead_api(self):
        self.check('클로드야','claude',side_effect=RuntimeError('down'))
        self.assertEqual(self.f.rows("SELECT * FROM events WHERE event_type LIKE 'AGENT_ROUTE_INTERPRETATION%'"),[])

    def test_confirmation_wording_depends_on_target(self):
        self.check('클로드야','claude')
        self.assertIn(COPY['choose_no_target'].strip(),self.last())
        with self.answer('work'):
            self.f.claude()
        self.check('클로드야','claude')
        self.assertIn(COPY['choose_target'].strip(),self.last())

    def test_interpret_blank_body_is_ambiguous_without_event(self):
        with self.answer('work') as call:
            self.assertEqual(interpret(self.f.bot,'   ','claude'),'ambiguous')
        call.assert_not_called()
        self.assertEqual(self.f.rows("SELECT * FROM events WHERE event_type LIKE 'AGENT_ROUTE_INTERPRETATION%'"),[])


class RoutingStatusTests(SemanticFixture):
    """Spec G-2: routing-state questions are answered by the bot from routing_summary, not as a task status."""
    QUESTION='지금 어떤 모델 골라져 있고 답 기다리는 질문 있어?'

    def test_real_sentence_reports_selection_and_pending_question(self):
        with self.answer('work'):
            row=self.f.claude()
        self.f.question(row)
        self.select_codex()
        with self.answer('status') as call:
            self.f.say(self.QUESTION)
        call.assert_not_called()
        text=self.last()
        self.assertIn('[봇 안내]',text)
        self.assertNotIn('[봇 안내 · ',text)
        self.assertIn('지금 고른 모델: 코덱스',text)
        self.assertIn('클로드 1',text)
        self.assertNotIn('지금 대화하는 작업',text)
        self.assertEqual(self.f.rows("SELECT state FROM orca_questions")[0]['state'],'PENDING')

    def test_answers_without_any_selection_and_without_assistant(self):
        with patch.object(self.f.bot,'_chat_reply') as chat, self.answer('status') as call:
            self.f.say(self.QUESTION)
        chat.assert_not_called();call.assert_not_called()
        self.assertIn('[봇 안내]',self.last())
        self.assertIn('지금 고른 모델: 없음',self.last())

    def test_named_routing_question_also_selects(self):
        with self.answer('status') as call:
            self.f.say('클로드야 지금 어떤 모델이야')
        call.assert_not_called()
        self.assertEqual(self.f.active(),'claude')
        self.assertIn('지금 고른 모델: 클로드',self.last())

    def test_work_sentence_is_not_a_routing_question(self):
        with self.answer('work'):
            self.f.say('클로드야 모델 선택 기능 코드 고쳐줘')
        self.assertNotIn('지금 고른 모델',self.last())

    def test_task_progress_question_keeps_status_card(self):
        self.select_codex()
        with self.answer('status'):
            self.f.say('지금 어디까지 했어?')
        self.assertNotIn('지금 고른 모델',self.last())


if __name__=='__main__':
    unittest.main()

import unittest
from unittest.mock import patch
from types import SimpleNamespace

import test_agent_routing as routing
from app.agent_router import interpret
from app.chat import ChatConfig,ChatResponder


class SemanticRouterTests(unittest.TestCase):
    def setUp(self):
        f=routing.RoutingTests();f.setUp();self.f=f
        self.addCleanup(f.tearDown);self.addCleanup(f.doCleanups)
        f.bot.chat_config=ChatConfig(semantic_routing=True)
        f.bot.chat=ChatResponder(f.bot.chat_config,env_path=f.settings.env_path,gemini=None)
        p=patch.object(f.bot.chat,'_api_key',return_value='placeholder');p.start();self.addCleanup(p.stop)

    def answer(self,intent):
        return patch.object(self.f.bot.chat.claude,'complete',return_value=SimpleNamespace(data={'intent':intent},model='tested-model',usage={}))

    def test_api_proposes_intent_but_cannot_choose_agent_or_target(self):
        with self.answer('conversation'):
            self.f.say('클로드야 이 방에서 뭘 할 수 있어?')
        jobs=self.f.rows('SELECT * FROM telegram_conversation_jobs')
        self.assertEqual(jobs[0]['agent'],'claude')
        self.assertEqual(self.f.queue.rows(),[])

    def test_api_cannot_convert_question_into_decision(self):
        with self.answer('answer'):
            intent=interpret(self.f.bot,'왜 그렇게 생각해?','claude',True)
        self.assertEqual(intent,'conversation')

    def test_schema_valid_misinterpretation_cannot_turn_greeting_into_work(self):
        with self.answer('work'):
            self.f.say('클로드야 안녕')
        self.assertEqual(self.f.queue.rows(),[])
        self.assertEqual(self.f.rows('SELECT * FROM telegram_conversation_jobs'),[])

    def test_api_failure_does_not_execute_work(self):
        with patch.object(self.f.bot.chat.claude,'complete',side_effect=RuntimeError('unavailable')):
            self.f.say('클로드야 텔레그램 봇 README를 읽어줘')
        self.assertEqual(self.f.queue.rows(),[])
        self.assertIn('전달하지 않았습니다',self.f.tg.sent[-1]['text'])

    def test_invalid_api_fields_cannot_supply_execution_commands(self):
        result=SimpleNamespace(data={'intent':'work','command':'delete everything'},model='test',usage={})
        with patch.object(self.f.bot.chat.claude,'complete',return_value=result):
            self.assertEqual(interpret(self.f.bot,'README 읽어줘','claude'),'blocked')

    def test_authoritative_quota_query_does_not_spend_api_call(self):
        with self.answer('work') as call:
            self.assertEqual(interpret(self.f.bot,'지금 주간 쿼터 얼마나 남았어','claude'),'quota')
        call.assert_not_called()

    def test_opinion_questions_are_never_promoted_to_decisions_for_either_agent(self):
        f=self.f
        with self.answer('work'):row=f.claude()
        f.question(row)
        f.bridge.update(f.key,turn_id='turn-c',state='RUNNING')
        f.bridge.question(f.rows('SELECT * FROM codex_sessions WHERE id=?',f.key)[0],{'id':1,'method':'item/tool/requestUserInput','params':{
            'turnId':'turn-c','questions':[{'id':'name','header':'이름','question':'어느 이름?','options':[]}]}})
        with self.answer('answer'):
            for text in ['코덱스야 너는 어떻게 생각해','클로드야 그 방법은 어때','클로드야 설명해줘']:
                f.say(text)
        self.assertEqual(f.rows('SELECT state FROM codex_questions')[0]['state'],'PENDING')
        self.assertEqual(f.rows('SELECT state FROM orca_questions')[0]['state'],'PENDING')
        self.assertEqual(len(f.rows('SELECT * FROM telegram_conversation_jobs')),3)

    def test_missing_key_and_read_failure_do_not_bypass_semantic_gate(self):
        with patch.object(self.f.bot.chat,'_api_key',return_value=''):
            self.f.say('클로드야 텔레그램 봇 README 수정해줘')
            self.assertEqual(self.f.queue.rows(),[])
            self.f.say('클로드야 안녕')
            self.assertEqual(len(self.f.rows('SELECT * FROM telegram_conversation_jobs')),1)

    def test_semantic_hop_keeps_entire_current_request(self):
        body='설명할 내용 '*700+' 마지막 요청도 그대로 전달해줘'
        with self.answer('conversation') as call:
            interpret(self.f.bot,body,'claude')
        self.assertEqual(call.call_args.kwargs['messages'][0]['content'],body)

    def test_actual_pending_question_is_supplied_even_after_unrelated_queries(self):
        import json
        with self.answer('work'):row=self.f.claude()
        self.f.question(row)
        with self.answer('answer') as call:
            self.f.say('alpha나 beta 말고 gamma로 써줘')
        context=json.loads(call.call_args.kwargs['context_text'])
        self.assertEqual(context['pending_question_text'],['alpha 또는 beta?'])
        self.assertEqual(self.f.rows('SELECT state FROM orca_questions')[0]['state'],'ANSWERING')

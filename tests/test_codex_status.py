import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.codex_bridge import Bridge
from app.codex_session import CodexSessions
from app.codex_status import status_query
from test_watch import BotFixture


class ProgressQueries(BotFixture):
    def setUp(self):
        super().setUp()
        self.config={'enabled':True,'projects':{'trial':{'path':str(self.home),'aliases':['연결시험']}}}
        self.addCleanup(patch.stopall)
        patch('app.codex_session.load_config',return_value=self.config).start()
        self.api=CodexSessions(self.bot.store,self.config)
        self.bridge=Bridge(self.bot.store,self.config,self.home/'socket')
        self.bridge.runtime('READY')
        self.key=self.api.create(100,'trial','create-one')
        self.bridge.update(self.key,thread_id='thread-one',epoch=self.bridge.epoch,state='RUNNING',turn_id='turn-one')

    def rows(self,sql,*args):
        with self.bot.store.connect() as db:
            return [dict(row) for row in db.execute(sql,args)]

    def question(self,method='item/tool/requestUserInput'):
        self.bridge.question(self.api.focused(100),{'id':7,'method':method,'params':{
            'threadId':'thread-one','turnId':'turn-one','itemId':'item-one','command':'printf harmless',
            'questions':[{'id':'choice','header':'이름','question':'파일 이름을 골라 주세요.',
                          'options':[{'label':'alpha','description':'첫 이름'},{'label':'beta','description':'둘째 이름'}]}]}})
        return self.api.pending(self.key)[0]

    def test_common_and_held_out_progress_phrasings_are_read_only(self):
        examples=['지금 뭐 하고 있어?', '어디까지 했어?', '진행 상황 알려줘', '다 끝났어?',
                  '현재 상태는?', '왜 멈췄어?', '코덱스 지금 어디까지 됐어?',
                  '혹시 작업은 어디까지 진행됐나요?', '현재 무엇을 하고 있나요?',
                  '코덱스는 다 끝났나요?', '지금 진행 상태 보여주세요.',
                  '혹시 지금 작업이 진행 중이야?', '현재 하고 있는 작업이 무엇인가요?',
                  '현재 진행 상황을 알려줘', '작업 중이야?', '지금 작업 중이야?', '작업 진행 상황 알려줘']
        for example in examples:
            with self.subTest(example=example):
                self.assertEqual(status_query(example),'status')

    def test_decisions_and_instructions_are_not_mistaken_for_status(self):
        for text in ['alpha','2','A로 하되 진행 상황은 수시로 보고해줘.',
                     '현재 상태를 저장하고 테스트를 실행해줘',
                     '메뉴 이름은 진행 상황으로 해줘', '다 끝나면 알려줘',
                     '어디까지 했어라는 문구를 수정해줘', '진행 상황 알려주는 기능을 만들어줘',
                     '파일 이름은 status로 하고 진행해', '1. alpha\n2. beta', 'status.py',
                     'progress.md', '1. status\n2. results']:
            with self.subTest(text=text):
                self.assertIsNone(status_query(text))

    def test_progress_during_a_question_never_consumes_answer_or_enqueues_work(self):
        question=self.question()
        before=self.rows('SELECT * FROM codex_commands')
        for n,text in enumerate(['지금 뭐 하고 있어?', '어디까지 했어?', '진행 상황 알려줘']):
            self.bot._handle_update(self.message(text,9000+n))
            self.assertEqual(self.api.pending(self.key)[0]['id'],question['id'])
            self.assertEqual(self.rows('SELECT * FROM codex_commands'),before)
            self.assertIn('파일 이름을 골라',self.tg.sent[-1]['text'])
        self.assertIsNone(self.rows('SELECT response FROM codex_questions')[0]['response'])

    def test_progress_during_approval_cannot_approve(self):
        self.question('item/commandExecution/requestApproval')
        self.bot._handle_update(self.message('현재 상태는?',9010))
        self.assertEqual(self.rows('SELECT state,response FROM codex_questions'),[{'state':'PENDING','response':None}])
        self.assertIn('실행 또는 권한 허용 여부',self.tg.sent[-1]['text'])

    def test_status_reports_real_tool_event_without_creating_a_new_turn(self):
        asyncio.run(self.bridge.event({'method':'item/started','params':{
            'threadId':'thread-one','turnId':'turn-one','item':{'id':'cmd','type':'commandExecution','command':'python3 -m unittest checks'}}}))
        before=self.rows('SELECT * FROM codex_commands')
        self.bot._handle_update(self.message('지금 뭐 하고 있어?',9020))
        self.assertEqual(self.rows('SELECT * FROM codex_commands'),before)
        self.assertIn('python3 -m unittest checks',self.tg.sent[-1]['text'])
        self.assertIn('동작 확인:',self.tg.sent[-1]['text'])
        self.assertNotIn('실행하고 있습니다',self.tg.sent[-1]['text'])

    def test_stale_connection_does_not_present_recorded_running_state_as_current(self):
        with self.bot.store.connect() as db:
            db.execute('UPDATE codex_runtime SET observed_at=?',((datetime.now(UTC)-timedelta(seconds=60)).isoformat(),))
        self.bot._handle_update(self.message('진행 상황 알려줘',9030))
        text=self.tg.sent[-1]['text']
        self.assertIn('현재 연결 상태를 확인하지 못했습니다',text)
        self.assertIn('현재 상태 미확인',text)
        self.assertNotIn('상태: 작업 중',text)

    def test_status_reply_to_another_session_question_keeps_both_questions_untouched(self):
        question=self.question()
        second=self.api.create(100,'trial','create-two')
        self.bridge.update(second,thread_id='thread-two',epoch=self.bridge.epoch,state='RUNNING',turn_id='turn-two')
        self.bridge.question(self.api.focused(100),{'id':8,'method':'item/tool/requestUserInput','params':{
            'threadId':'thread-two','turnId':'turn-two','itemId':'item-two',
            'questions':[{'id':'q','header':'다른 질문','question':'다른 작업의 질문'}]}})
        update=self.message('지금 뭐 하고 있어?',9040)
        update['message']['reply_to_message']={'text':'확인 번호: '+question['id']}
        self.bot._handle_update(update)
        self.assertEqual(len(self.rows("SELECT * FROM codex_questions WHERE state='PENDING'")),2)
        self.assertIn('파일 이름을 골라',self.tg.sent[-1]['text'])
        self.assertNotIn('다른 작업의 질문',self.tg.sent[-1]['text'])
        self.assertEqual(self.api.focused(100)['id'],second)

    def test_mixed_status_and_decision_is_not_silently_executed(self):
        self.question()
        before=self.rows('SELECT * FROM codex_commands')
        examples=['어디까지 했어? 그리고 alpha로 진행해','진행 상황 알려줘. 그리고 alpha로 진행해',
                  '진행 상황 알려줘, alpha로 진행해','진행 상황 알려줘 그리고 alpha로 진행해']
        for n,text in enumerate(examples):
            self.bot._handle_update(self.message(text,9050+n))
            self.assertEqual(self.rows('SELECT * FROM codex_commands'),before)
            self.assertEqual(self.rows('SELECT state FROM codex_questions')[0]['state'],'PENDING')
            self.assertIn('조회만 처리했습니다',self.tg.sent[-1]['text'])

    def test_freeform_decision_still_reaches_the_same_question(self):
        self.question()
        text='alpha로 하되 진행 상황은 수시로 보고해줘.'
        self.bot._handle_update(self.message(text,9060))
        answer=json.loads(self.rows('SELECT response FROM codex_questions')[0]['response'])
        self.assertEqual(answer,{'answers':{'choice':{'answers':[text]}}})

    def test_filename_answer_and_numbered_answers_reach_questions(self):
        self.question()
        self.bot._handle_update(self.message('status.py',9061))
        answer=json.loads(self.rows('SELECT response FROM codex_questions')[0]['response'])
        self.assertEqual(answer['answers']['choice']['answers'],['status.py'])
        self.bridge.question(self.api.focused(100),{'id':8,'method':'item/tool/requestUserInput','params':{
            'threadId':'thread-one','turnId':'turn-one','questions':[
                {'id':'a','question':'이름?','header':'이름'},{'id':'b','question':'폴더?','header':'폴더'}]}})
        pending=self.api.pending(self.key)[0]
        update=self.message('1. status\n2. results',9062)
        update['message']['reply_to_message']={'text':'확인 번호: '+pending['id']}
        self.bot._handle_update(update)
        answer=json.loads(self.rows('SELECT response FROM codex_questions WHERE id=?',pending['id'])[0]['response'])
        self.assertEqual(answer,{'answers':{'a':{'answers':['status']},'b':{'answers':['results']}}})

    def test_explicit_other_project_starts_separate_work_without_answering_old_question(self):
        question=self.question()
        self.config['projects']['bot']={'path':str(self.home/'bot'),'aliases':['텔레그램봇']}
        self.bot._handle_update(self.message('코덱스로 텔레그램봇 README를 읽어줘',9065))
        self.assertEqual(self.api.focused(100)['project'],'bot')
        self.assertEqual(self.api.pending(self.key)[0]['id'],question['id'])
        self.assertIsNone(self.rows('SELECT response FROM codex_questions WHERE id=?',question['id'])[0]['response'])
        commands=self.rows("SELECT * FROM codex_commands WHERE op='message'")
        self.assertEqual(len(commands),1)
        self.assertNotEqual(commands[0]['session_id'],self.key)

    def test_explicit_answer_can_contain_status_words(self):
        question=self.question()
        self.bot._handle_update(self.message('/codex answer '+question['id']+' 지금 뭐 하고 있어?',9070))
        self.assertEqual(self.rows('SELECT state FROM codex_questions')[0]['state'],'ANSWERING')

    def test_no_focus_explicit_codex_status_does_not_create_a_session(self):
        self.api.focus(100,None)
        count=len(self.api.rows(100))
        for n,text in enumerate(['코덱스 지금 뭐 하고 있어?', '코덱스는 다 끝났나요?', '코덱스가 뭐 하고 있나요?']):
            self.bot._handle_update(self.message(text,9080+n))
            self.assertEqual(len(self.api.rows(100)),count)
            self.assertIn('마지막 확인:',self.tg.sent[-1]['text'])

    def test_question_from_another_chat_does_not_leak_status(self):
        foreign=self.api.create(101,'trial','foreign')
        self.bridge.update(foreign,thread_id='foreign',epoch=self.bridge.epoch,state='RUNNING',turn_id='foreign-turn')
        self.bridge.question(self.api.focused(101),{'id':9,'method':'item/tool/requestUserInput','params':{
            'threadId':'foreign','turnId':'foreign-turn','questions':[{'id':'f','question':'PRIVATE OTHER CHAT','header':'외부'}]}})
        update=self.message('지금 뭐 하고 있어?',9090)
        update['message']['reply_to_message']={'text':'확인 번호: '+self.api.pending(foreign)[0]['id']}
        self.bot._handle_update(update)
        self.assertNotIn('PRIVATE OTHER CHAT',self.tg.sent[-1]['text'])
        self.assertEqual(self.api.pending(foreign)[0]['state'],'PENDING')

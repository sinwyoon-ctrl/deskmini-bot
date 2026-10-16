"""Opt-in real Claude API routing and subscription CLI replies; private fake transport."""
import json,os,secrets,sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
from test_agent_routing import RoutingTests
from app.agent_conversation import process_one
from app.chat import ChatResponder,ChatConfig
from app.config import Settings
from app.codex_usage import latest_codex_limits


def run(report):
    assert os.environ.get('LIVE_CONVERSATION_INTENT')=='1'
    report=Path(report);assert not report.exists()
    real=Settings.load();limits=latest_codex_limits(Path.home());assert limits
    f=RoutingTests();f.setUp();passed=False;cases=[]
    def record(name,**data):
        cases.append({'case':name,**data});print(json.dumps(cases[-1],ensure_ascii=False),flush=True)
    def finish():
        process_one(f.bot.store);f.bot._flush_outbox()
        assert f.rows('SELECT state FROM telegram_conversation_jobs ORDER BY rowid DESC LIMIT 1')[0]['state']=='DONE'
        return f.tg.sent[-1]['text']
    try:
        f.bot.chat_config=replace(ChatConfig.load(real.root/'config/models.toml'),semantic_routing=True)
        f.bot.chat=ChatResponder(f.bot.chat_config,env_path=real.env_path,gemini=None)
        assert f.bot.chat._api_key()
        with patch('app.bot.latest_codex_limits',return_value=limits):
            initial=f.rows('SELECT * FROM codex_commands')
            f.say('코덱스야 지금 쿼터 얼마나 남았어?')
            assert '남음' in f.tg.sent[-1]['text']
            assert f.rows('SELECT * FROM codex_commands')==initial
            record('exact_codex_quota',reply=f.tg.sent[-1]['text'])
            f.say('클로드야 지금 주간 쿼터 얼마나 남았어')
            assert f.queue.rows()==[]
            reply=finish();assert '주간:' in reply and '남음' in reply
            record('exact_claude_quota_no_project',reply=reply,source='actual CLI rate_limit_event')
            marker='이어봄-'+secrets.token_hex(5)
            update=f.say('클로드야 안녕. 이 대화의 확인 표식은 '+marker+'야. 표식만 답해줘.')
            reply=finish();assert marker in reply and reply.startswith('[클로드]')
            assert f.queue.rows()==[]
            record('projectless_claude_conversation',reply=reply)
            count=len(f.rows('SELECT * FROM telegram_conversation_jobs'));f.bot._handle_update(update)
            assert len(f.rows('SELECT * FROM telegram_conversation_jobs'))==count
            f.say('코덱스야 클로드가 방금 답한 확인 표식을 그대로 적고 너의 의견을 한 문장으로 말해줘. 도구는 사용하지 마.')
            reply=finish();assert marker in reply and reply.startswith('[코덱스]')
            assert f.rows('SELECT * FROM codex_commands')==initial
            record('cross_model_context_without_business_commands',reply=reply)
            f.bridge.update(f.key,turn_id='fixture-turn',state='RUNNING')
            f.bridge.question(f.rows('SELECT * FROM codex_sessions WHERE id=?',f.key)[0],{'id':1,'method':'item/tool/requestUserInput','params':{
                'turnId':'fixture-turn','questions':[{'id':'name','header':'이름','question':'alpha 또는 beta 중 이름을 골라 주세요','options':[]}]}})
            row=f.queue.submit('fixture:pending','bot-claude','claude','Only a fixture decision',100,select_chat=False)
            f.queue.update(row['id'],state='RUNNING',run_id='fixture-run',task_id='fixture-task',dispatch_id='fixture-dispatch')
            from app.agent_routing import select
            with f.bot.store.transaction() as db:select(db,100,'claude',row['id'],'telegram:'+str(f.number))
            f.question(row);f.bot._flush_outbox()
            f.say('코덱스야 지금 쿼터 얼마나 남았어?')
            f.say('클로드야 지금 주간 쿼터 얼마나 남았어')
            assert f.rows('SELECT state FROM codex_questions')[0]['state']=='PENDING'
            assert f.rows('SELECT state FROM orca_questions')[0]['state']=='PENDING'
            f.say('클로드야 왜 그런거야?')
            finish()
            assert f.rows('SELECT state FROM orca_questions')[0]['state']=='PENDING'
            record('new_questions_preserve_pending_decisions',codex='PENDING',claude='PENDING')
            f.say('alpha나 beta 말고 gamma로 써줘')
            assert f.rows('SELECT state,reply_text FROM orca_questions')[0]=={'state':'ANSWERING','reply_text':'alpha나 beta 말고 gamma로 써줘'}
            assert f.rows('SELECT state FROM codex_questions')[0]['state']=='PENDING'
            record('natural_decision_goes_only_to_selected_claude',reply='gamma',codex_question_unchanged=True)
            before=len(f.rows('SELECT * FROM telegram_conversation_jobs'))
            f.say('클로드야 주간 쿼터 얼마나 남았어 그리고 README 수정해줘')
            assert len(f.rows('SELECT * FROM telegram_conversation_jobs'))==before
            assert f.rows('SELECT * FROM orca_chat_commands')==[]
            record('mixed_query_and_work_not_executed')
            events=f.bot.store.recent_events_by_type('AGENT_ROUTE_INTERPRETATION',20)
            assert events
            record('actual_semantic_api_used',events=len(events),models=sorted({e['payload']['model'] for e in events}))
        passed=True
    finally:
        result={'status':'PASS' if passed else 'FAIL','actual_claude_api':True,'actual_codex_cli':True,'actual_claude_cli':True,
                'telegram_transport':'FakeTelegram','production_db_changed':False,'cases':cases}
        if not passed:result['fixture_path']=str(f.home)
        report.write_text(json.dumps(result,ensure_ascii=False,indent=2))
        if passed:f.doCleanups();f.tearDown()
        else:f.temp._finalizer.detach()
    print(json.dumps({'status':'PASS','cases':len(cases)},ensure_ascii=False),flush=True)


if __name__=='__main__':run(sys.argv[1])

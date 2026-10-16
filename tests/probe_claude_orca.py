"""Opt-in Claude Opus/native Orca ask-reply check with FakeTelegram and a separate DB."""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from test_watch import BotFixture
from app.orca_bridge import OrcaBridge, OrcaQueue

CLI='/home/ionenet/.orca/cli/bin/orca'


def released_worker(workers, dispatch_id):
    matches=[worker for worker in workers if worker.get('dispatchId')==dispatch_id]
    return len(matches)==1 and matches[0].get('terminalState')=='released'


def finish_fixture(fixture, passed):
    if passed:
        fixture.doCleanups(); fixture.tearDown()
    else:
        fixture.temp._finalizer.detach()


def reserve_report(report):
    if report.exists():
        raise FileExistsError(report)
    resources=report.with_suffix('.resources.json')
    with resources.open('x') as out:
        json.dump({'stage':'reserved'},out)
    return resources


def call(*args,cwd='/home/ionenet/deskmini-bot'):
    p=subprocess.run([CLI,*args,'--json'],cwd=cwd,capture_output=True,text=True,timeout=100)
    data=json.loads(p.stdout)
    if p.returncode or not data.get('ok',True):
        raise RuntimeError(str(data.get('error') or {'exit_code':p.returncode}))
    return data


def probe(report):
    resources=reserve_report(report)
    fixture=BotFixture(); fixture.setUp()
    evidence={'status':'RUNNING','telegram_transport':'FakeTelegram','actual_claude':True,'production_db_changed':False,'production_services_changed':False,
        'cli_permission_forwarding_tested':False,'cli_permission_note':'Native terminal permission prompts may require separate operator approval; this probe does not forward them.'}
    coordinator=None
    try:
        created=call('worktree','create','--repo','id:a26c7673-34a6-47d0-a50f-62182a3e9a48','--name',report.stem,'--setup','skip')
        workspace=created['result']['worktree']; root=Path(workspace['path'])
        resource={'workspace':workspace['id'],'workspace_path':str(root),'runtime_id':created['_meta']['runtimeId']}
        resources.write_text(json.dumps(resource,indent=2))
        terminal=call('terminal','create','--worktree','id:'+workspace['id'],'--title','Claude Telegram test coordinator')
        result=terminal['result']
        coordinator=result.get('terminal',{}).get('handle') if isinstance(result.get('terminal'),dict) else result.get('terminal')
        coordinator=coordinator or result.get('handle')
        if not coordinator:
            resource['terminal_receipt']=result; resources.write_text(json.dumps(resource,indent=2))
            raise RuntimeError('Coordinator handle is absent; inspect saved terminal receipt, do not relaunch.')
        resource['coordinator']=coordinator; resources.write_text(json.dumps(resource,indent=2))
        run=call('orchestration','run-create','--objective','Isolated Claude Telegram question/reply acceptance','--from',coordinator,cwd=str(root))['result']
        run_id=(run.get('run') or run).get('id') or run.get('runId')
        resource['run_id']=run_id; resources.write_text(json.dumps(resource,indent=2))
        assert run_id
        config={'enabled':True,'cli':CLI,'cwd':str(root),'runtime_id':resource['runtime_id'],'run_id':run_id,'coordinator':coordinator,
            'hub_root':str(fixture.home/'agent-hub'),'projects':{'claude-proof':{
                'path':str(root),'worktree':'id:'+workspace['id'],'hub_project':'claude-proof','verify_workspace':True,'ownership_ready':True,
                'scope':'Create only CLAUDE_PROBE.json in this dedicated test worktree. Do not edit other files. Native coordinator ask/check/worker_done are allowed. No hub commands, production, secrets, installations, services, new agents, commit/push or deployment.'}}}
        queue=OrcaQueue(fixture.bot.store,config); bridge=OrcaBridge(queue)
        request=('Telegram 답변이 실제 결과를 바꾸는지 검증합니다. 아직 파일을 만들지 말고 먼저 live preamble의 blocking coordinator ask를 사용해 '
            '출력할 이름은 alpha와 beta 중 무엇인가요? 라고 한 번 질문하세요. 로컬 AskUserQuestion 대신 반드시 preamble의 ask 셸 명령을 쓰세요. '
            '답변을 받을 때까지 결과 파일을 만들지 마세요. 답변은 선택지 밖의 자연어일 수 있습니다. 받은 지시의 이름과 출력 형식을 해석해 '
            'CLAUDE_PROBE.json 하나만 만들고 {"chosen": 이름, "rendered": 지시한 형식으로 변환한 이름, "received_answer": 받은 답변 원문}을 저장하세요. '
            'Python으로 결과를 읽어 확인한 뒤 preamble의 worker_done succeeded를 보내고 끝내세요. '
            'Read AGENTS.md/HANDOFF.md only if present. 이 시험에서는 hub·외부 메모리·설정·프로젝트·비밀값을 읽지 말고 다른 에이전트를 만들지 마세요.')
        row=queue.submit('claude-proof:'+report.stem,'claude-proof','claude',request,100)
        queue.update(row['id'],model='claude-opus-5-5')
        bridge.start(queue.rows()[0])
        row=queue.rows()[0]; resource.update(request_id=row['id'],dispatch_id=row['dispatch_id'],task_id=row['task_id'])
        resources.write_text(json.dumps(resource,indent=2))
        assert row['state'] not in {'BLOCKED','FAILED','UNKNOWN'},row.get('error')
        end=time.monotonic()+240
        question=None
        while time.monotonic()<end:
            bridge.reconcile()
            with queue.store.connect() as db:
                question=db.execute("SELECT * FROM orca_questions WHERE state='PENDING'").fetchone()
            if question: break
            time.sleep(1)
        assert question,'No native Claude question yet; retain resources for inspection.'
        assert not (root/'CLAUDE_PROBE.json').exists()
        evidence['native_question_received']=True; evidence['file_absent_before_answer']=True
        answer='alpha나 beta 대신 gamma로 해줘. 출력 문구는 대문자 GAMMA로 써줘.'
        with patch('app.orca_bridge.OrcaQueue.load',return_value=queue):
            fixture.bot._handle_update(fixture.message('/orca reply '+row['id']+' '+answer,97001))
        end=time.monotonic()+240
        while time.monotonic()<end:
            bridge.reconcile()
            row=queue.rows()[0]
            if row['state'] in {'SUCCEEDED','FAILED'}: break
            time.sleep(1)
        assert row['state']=='SUCCEEDED',row.get('error') or row['state']
        result=json.loads((root/'CLAUDE_PROBE.json').read_text())
        evidence['artifact']=result
        assert result['chosen']=='gamma' and result['rendered']=='GAMMA',result
        assert result['received_answer']==answer,result
        with queue.store.connect() as db:
            question_state=db.execute('SELECT state FROM orca_questions').fetchone()[0]
            evidence['question_state']=question_state
            assert question_state=='ANSWERED'
            receipt=json.loads(db.execute('SELECT receipt_json FROM orca_attempts').fetchone()[0])
        evidence.update(answer_applied=True,freeform_outside_options=True,native_succeeded=True,
            requested_model='claude-opus-5-5',launch=receipt.get('launch'),telegram_network_calls=0)
        workers=call('orchestration','worker-list','--run',run_id)['result']['workers']
        evidence['worker_terminal_released']=released_worker(workers,row['dispatch_id'])
        assert evidence['worker_terminal_released']
        call('terminal','close','--terminal',coordinator)
        evidence['coordinator_closed']=True
        evidence['status']='PASS'
    except Exception as exc:
        evidence.update(status='FAIL',error_type=type(exc).__name__,error=str(exc)[:700])
        evidence['preserved_fixture']=str(fixture.home)
    finally:
        finish_fixture(fixture,evidence['status']=='PASS')
        with report.open('x') as out: json.dump(evidence,out,ensure_ascii=False,indent=2)
    print(json.dumps(evidence,ensure_ascii=False),flush=True)
    if evidence['status']!='PASS': raise SystemExit(1)


if __name__=='__main__':
    if os.environ.get('LIVE_CLAUDE_ORCA')!='1':
        raise SystemExit('Set LIVE_CLAUDE_ORCA=1; launches Claude Opus 5.5 in its own Orca workspace/run, uses FakeTelegram.')
    parser=argparse.ArgumentParser(); parser.add_argument('--report',type=Path,required=True)
    probe(parser.parse_args().report)

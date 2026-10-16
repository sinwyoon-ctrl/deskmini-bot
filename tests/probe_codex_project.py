"""Opt-in real Codex read against the registered bot development worktree."""
import argparse
import asyncio
import hashlib
import os
import subprocess
from pathlib import Path

from autonomous_codex_bridge import Scenarios
from app.codex_session import load_config


class ProjectProbe(Scenarios):
    async def scenarios(self):
        project=load_config(Path(__file__).resolve().parents[1])['projects']['bot']
        root=Path(project['path'])
        before_status=subprocess.run(['git','status','--porcelain'],cwd=root,capture_output=True,text=True,check=True).stdout
        digest=hashlib.sha256((root/'README.md').read_bytes()).hexdigest()
        expected=next(line.lstrip('#').strip() for line in (root/'README.md').read_text().splitlines() if line.startswith('#'))
        self.config['projects']['bot']=dict(project,scope=project['scope']+' This probe is read-only: do not edit any file or run tests.')
        self.send('/codex new trial')
        previous=self.api.focused(100)['id']
        await self.until(lambda:self.session(previous)['state']=='IDLE')
        before=len(self.events)-1
        self.send('코덱스로 텔레그램봇 README.md만 읽고 첫 번째 제목의 # 기호를 제외한 문구만 그대로 답해줘. 파일 수정·테스트·hub·외부 파일 접근은 하지 마.')
        key=self.api.focused(100)['id']
        response=await self.done(key,before)
        assert key!=previous
        assert response['project']=='bot' and Path(response['cwd'])==root
        assert expected in response['last_text'],response['last_text']
        assert hashlib.sha256((root/'README.md').read_bytes()).hexdigest()==digest
        after_status=subprocess.run(['git','status','--porcelain'],cwd=root,capture_output=True,text=True,check=True).stdout
        assert after_status==before_status
        self.record('actual_bot_project_read',actual_codex=True,registered_project='bot',expected_readme_heading=True,readme_hash_unchanged=True,git_status_unchanged=True,previous_focus_switched=True)
        commands=len(self.rows('SELECT id FROM codex_commands'))
        self.send('지금 뭐 하고 있어?')
        assert len(self.rows('SELECT id FROM codex_commands'))==commands
        self.record('actual_bot_project_status',new_commands=0,same_thread=self.session(key)['thread_id']==response['thread_id'])


if __name__=='__main__':
    if os.environ.get('LIVE_CODEX_AUTONOMOUS')!='1':
        raise SystemExit('Set LIVE_CODEX_AUTONOMOUS=1; real Codex, isolated DB, FakeTelegram, read-only project request.')
    parser=argparse.ArgumentParser()
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    asyncio.run(ProjectProbe(args.report).run())

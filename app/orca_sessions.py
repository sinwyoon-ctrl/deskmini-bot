"""Read-only Orca session observations; stored separately from supervised task state."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.redact import redact, redact_text

CACHE_NAME = 'orca-sessions.json'
MAX_AGE_SECONDS = 120
STATE_LABELS = {'working': '응답 생성 중', 'done': '응답 완료·다음 지시 대기',
                'idle': '입력 대기', 'waiting': '입력 필요', 'blocked': '입력 필요',
                'error': '오류', 'failed': '오류'}


def capture_sessions(store, config, client=None):
    observed = datetime.now(UTC).isoformat(timespec='seconds')
    observation = {'status': 'error', 'observed_at': observed, 'items': [],
                   'source': 'Orca worktree ps; agent turn state is not project completion'}
    try:
        if not config.get('enabled'):
            raise ValueError('Orca 연결 꺼짐')
        if client is None:
            from app.orca_bridge import NativeOrca
            client = NativeOrca(config)
        result = client.call('worktree', 'ps', '--limit', '12', timeout=8)
        if not isinstance(result, dict) or not isinstance(result.get('worktrees'), list):
            raise ValueError('잘못된 Orca 세션 응답')
        scope = result.get('hostScope', {})
        observation['omitted_hosts'] = scope.get('omittedHostIds', [])
        observation['truncated'] = bool(result.get('truncated'))
        for workspace in result['worktrees'][:12]:
            agents = workspace.get('agents')
            if not isinstance(agents, list):
                raise ValueError('잘못된 Orca 에이전트 목록')
            for agent in agents[:4]:
                if not isinstance(agent, dict):
                    raise ValueError('잘못된 Orca 에이전트 상태')
                observation['items'].append({
                    'workspace': redact_text(str(workspace.get('displayName') or workspace.get('repo') or ''), 120),
                    'repo': redact_text(str(workspace.get('repo') or ''), 100),
                    'worktree_id': redact_text(str(workspace.get('worktreeId') or ''), 400),
                    'host': redact_text(str(workspace.get('hostId') or ''), 100),
                    'agent': redact_text(str(agent.get('agentType') or 'unknown'), 40),
                    'state': str(agent.get('state') or 'unknown')[:40],
                    'terminal_verdict': str(workspace.get('terminalVerdict') or 'unverifiable')[:40],
                    'last_response': redact_text(str(agent.get('lastAssistantMessage') or ''), 1200),
                    'agent_updated_at': agent.get('updatedAt'),
                })
            if len(agents) > 4:
                observation['truncated'] = True
        if len(result['worktrees']) > 12:
            observation['truncated'] = True
        observation['items'].sort(key=lambda item: item['agent_updated_at'] if isinstance(item['agent_updated_at'], (int, float)) else 0, reverse=True)
        observation['status'] = 'partial' if observation['omitted_hosts'] or observation['truncated'] else 'ok'
    except Exception as exc:
        observation.update(status='error', items=[], error=type(exc).__name__)
    destination = store.db_path.parent / CACHE_NAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.orca-sessions-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(redact(observation), stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return observation


def read_sessions(store):
    path = store.db_path.parent / CACHE_NAME
    try:
        if path.is_symlink() or path.stat().st_size > 150_000:
            raise ValueError('잘못된 세션 관측 파일')
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise ValueError('잘못된 세션 관측')
        stamp = datetime.fromisoformat(data['observed_at'])
        if stamp.tzinfo is None:
            raise ValueError('관측 시각에 시간대 없음')
        age = (datetime.now(UTC) - stamp).total_seconds()
        if age < -30 or age > MAX_AGE_SECONDS:
            return {'status': 'stale', 'observed_at': data['observed_at'], 'items': []}
        if data.get('status') not in {'ok', 'partial'}:
            data['items'] = []
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return {'status': 'unavailable', 'items': []}


def session_summary(store):
    data = read_sessions(store)
    return {**{k: v for k, v in data.items() if k != 'items'}, 'items': [
        {**{k: item.get(k) for k in ('workspace', 'repo', 'agent', 'state', 'terminal_verdict', 'agent_updated_at')},
         'last_response': redact_text(str(item.get('last_response') or ''), 180)}
        for item in data['items'][:4]], 'total_sessions': len(data['items'])}


def describe_sessions(store):
    data = read_sessions(store)
    if data['status'] not in {'ok', 'partial'}:
        return '실제 Orca 세션: 미확인 (' + data['status'] + ')'
    lines = ['실제 Orca 세션 · ' + data['observed_at']]
    for item in data['items'][:6]:
        label = STATE_LABELS.get(item['state'], '상태 미확인')
        if item['terminal_verdict'] != 'live':
            label = '세션 생존 미확인 · 마지막 응답 상태: ' + label
        lines.append(f"• {item['repo']} / {item['workspace']} · {item['agent']} · {label}")
        if item['last_response']:
            lines.append('  마지막 응답: ' + redact_text(' '.join(item['last_response'].split()), 240))
    if not data['items']:
        lines.append('• 관측된 에이전트 세션 없음')
    if data['status'] == 'partial' or len(data['items']) > 6:
        lines.append('• 일부 호스트 또는 세션은 생략됨')
    lines.append('응답 완료는 프로젝트 전체 완료를 뜻하지 않습니다.')
    return '\n'.join(lines)

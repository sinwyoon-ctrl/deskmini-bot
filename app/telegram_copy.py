"""Plain-language labels shared by Telegram receipts and status messages."""
from datetime import datetime, timedelta, timezone
import json


STATE_LABELS = {
    'CREATING': '대화 연결 중', 'IDLE': '다음 요청 대기', 'RUNNING': '작업 중',
    'WAITING': '답변을 기다리는 중', 'UNKNOWN': '현재 진행 상태 확인 필요',
    'FAILED': '오류로 멈춤', 'QUEUED': '시작 대기', 'DRAFT': '요청 내용 확인 중',
    'AWAITING_APPROVAL': '실행 승인 대기', 'APPROVED': '실행 준비 중',
    'VERIFYING': '결과 확인 중', 'READY': '검증 통과 · 반영 승인 대기',
    'BLOCKED': '진행을 막는 문제 확인 필요', 'CANCELLED': '중단됨',
    'INTERRUPTED': '연결 중단으로 확인 필요', 'COMPLETED': '처리 완료',
    'SUCCEEDED': '정상 종료', 'PENDING': '처리 대기', 'REJECTED': '거절됨',
    'AWAITING_START': '작업 시작 승인 대기', 'STARTING': '작업 시작 중',
    'STOP_REQUESTED': '중단 요청 전달 중', 'STOPPING': '작업을 정리하고 중단하는 중',
}


def state_label(value):
    return STATE_LABELS.get(value, '상태 확인 필요')


def local_time(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            return '시각 확인 필요'
        return stamp.astimezone(timezone(timedelta(hours=9))).strftime('%m월 %d일 %H:%M (한국 시간)')
    except (ValueError, TypeError):
        return '시각 확인 필요'


def project_name(config, key):
    project = config.get('projects', {}).get(key, {})
    return project.get('label') or next(iter(project.get('aliases') or []), key)


def sender_title(agent, project='', *, model=False):
    name = {'codex':'코덱스','claude':'클로드'}.get(agent,agent)
    return ('['+name+']' if model else '[봇 안내 · '+name+']')+(' '+project if project else '')


def message_card(title, body, footer, limit=3500):
    from app.redact import redact_text
    title = redact_text(title, 120)
    footer = redact_text(footer, len(footer)+1)
    body = redact_text(body, len(body)+1)
    budget = limit-len(title)-len(footer)-4
    if len(body) > budget:
        notice = '\n…내용이 길어 일부를 생략했습니다.'
        body = body[:budget-len(notice)]+notice
    return title+'\n\n'+body+'\n\n'+footer


def approval_details(fields):
    labels = {'reason': '확인이 필요한 이유', 'command': '실행할 명령', 'cwd': '작업 폴더',
              'additionalPermissions': '추가로 요청한 권한', 'networkApprovalContext': '접속할 외부 주소와 범위',
              'grantRoot': '접근을 요청한 폴더', 'permissions': '요청한 권한', 'changes': '변경할 파일과 내용'}
    sections = []
    for key in ('reason', 'command', 'cwd', 'networkApprovalContext', 'additionalPermissions', 'grantRoot', 'permissions', 'changes'):
        if key not in fields:
            continue
        value = fields[key]
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        sections.append(labels[key]+'\n'+text)
    return '\n\n'.join(sections)


def error_detail(error):
    if isinstance(error, dict):
        return str(error.get('message') or '오류의 상세 설명을 받지 못했습니다.')
    return str(error or '오류의 상세 설명을 받지 못했습니다.')

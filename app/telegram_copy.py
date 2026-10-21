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


# H: 사용자에게 보이는 안내 문구(해요체). 승인·2단계 확인 문구(cards.py)와 모델 발언은 여기 두지 않는다.
COPY = {
    'blocked': '무슨 뜻인지 정확히 못 알아들어서 아무 데도 보내지 않았어요. 조금만 풀어서 다시 말씀해 주시겠어요?',
    'blocked_waiting': '답을 기다리는 질문: ',
    'blocked_decision': '결정이면 그 질문 카드에 답장하거나 버튼으로 답해 주세요.',
    'codex_received': '코덱스에 전달했어요. 결과가 나오면 여기로 알려드릴게요.',
    'claude_received': '클로드에 전달했어요',
    'claude_received_previous': '이전 결과를 참고하는 새 작업으로 클로드에 전달했어요',
    'claude_followup': '진행 중인 클로드 작업에 덧붙였어요. 반영 여부는 결과에서 확인돼요.',
    'chat_pending': '잠시만요, 답변 준비 중이에요.',
    'quota_pending': '사용량을 확인하고 있어요.',
    'choose': '{agent}로 바꿨어요. 이어서 말씀하세요.',
    'choose_target': ' 같은 작업으로 이어가요.',
    'choose_no_target': ' 작업을 시키려면 프로젝트와 할 일을 알려 주세요.',
    'codex_working': '코덱스가 작업 중이에요…',
    'codex_started': '코덱스가 시작했어요.',
    'codex_done': '코덱스 응답이 끝났어요. 이어서 말씀하세요.',
    'conversation_failed': '{agent} 답변을 받지 못했어요. 작업이나 질문은 그대로예요.',
    'approval_unknown': "'{id}' 승인 요청은 아직 등록돼 있지 않아요. 요청한 쪽이 approvals/{id}.md 로 보내면 버튼이 떠요. 지금은 아무것도 보내지 않았어요.",
    'orca_sent': '추가 지시를 원래 작업자의 수신함에 전달했어요. 반영 여부는 결과에서 확인돼요.',
    'permission_allowed': '허용했어요. 클로드에 전달할게요.',
}


def choose_text(agent_label, has_target):
    return COPY['choose'].format(agent=agent_label)+(COPY['choose_target'] if has_target else COPY['choose_no_target'])


def meta_footer(kind, ident, anchor=None):
    """Last small line ``· 대화 c-…``/``· 작업 …``; reply anchors (확인 번호/질문 번호) stay above it unchanged."""
    return (anchor+'\n' if anchor else '')+'· '+kind+' '+ident


def activity_line(summary, observed_at, state_text=''):
    """describe: one line ``<요약> (<시각> 확인)``; omitted when the summary only repeats the state line."""
    if not summary or summary.strip() == (state_text or '').strip():
        return ''
    return ' · '.join(part.strip() for part in summary.strip().splitlines() if part.strip())+' ('+local_time(observed_at)+' 확인)'


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
    return title+('\n' if title.startswith('[봇 안내') else '\n\n')+body+'\n\n'+footer


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

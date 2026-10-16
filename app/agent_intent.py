"""Classify a Telegram message aimed at a named model before it can become work or a decision answer.

classify(text) returns one of: quota, status, conversation, work, answer, ambiguous.
It is pure: no I/O, no project inference. Callers still validate who owns a pending
question and which method it expects before treating 'answer' as a reply.
"""
import re
import unicodedata

from app.codex_status import status_query

QUOTA = 'quota'
STATUS = 'status'
CONVERSATION = 'conversation'
WORK = 'work'
ANSWER = 'answer'
AMBIGUOUS = 'ambiguous'

_QUOTES = r'"[^"]*"|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|`[^`]*`'
_PAST = re.compile(
    r'(?:아까|방금|이전에|전에|어제|지난번)[^\n]{0,40}?(?:물어|질문|말했|말한|했던|했잖|물었)'
    r'|(?:물어봤|물어본|질문했|물었)(?:는데|잖아|던|었|어|다)'
    r'|\b(?:i|we)\s+(?:already\s+)?asked\b|\basked\s+(?:you\s+)?(?:earlier|before)\b|\byou\s+said\b',
    re.I)

_QUOTA_NOUN = re.compile(
    r'쿼터|사용량|사용\s*한도|한도|리밋|리미트|레이트\s*리밋|크레딧|토큰\s*(?:잔량|남은|얼마)|잔량|잔여'
    r'|\b(?:quota|quotas|usage|limits?|rate[\s-]*limits?|credits?|allowance)\b', re.I)
_QUOTA_WINDOW = re.compile(r'주간|이번\s*주|세션|5\s*시간|\b(?:weekly|session|5[\s-]*h(?:our)?)\b', re.I)
_QUOTA_QUERY = re.compile(
    r'남았|남아|남은|남음|얼마|몇\s*(?:%|퍼|프로|개|번|시간)|확인|알려|보여|체크|언제|어때|어떻게\s*(?:돼|되|됐)'
    r'|리셋\s*(?:돼|되|됨|시간|까지|은|는|이)|초기화\s*(?:돼|되|됨|시간|까지)|풀려|풀리|[?？]'
    r'|\b(?:left|remaining|remain|how\s+much|how\s+many|when|check|show|what(?:\'s|\s+is)|status|resets?\s+(?:at|in|time)|reset\s+time)\b',
    re.I)
_QUOTA_ACTION = re.compile(
    r'늘려|늘리|올려|올리|증가|바꿔|바꾸|변경|수정|설정해|삭제|지워|지우|줄여|줄이|해제|없애|리셋해|리셋\s*시켜|초기화해|초기화\s*시켜|무시해'
    r'|\b(?:increase|raise|bump|change|set|modify|delete|remove|clear|lift|extend|disable|reset\s+(?:my|the|our)|reset\s+(?:usage|quota|limit))\b',
    re.I)

_WORK_NOUN = re.compile(
    r'파일|readme|코드|소스|레포|리포|저장소|프로젝트|폴더|디렉토리|디렉터리|함수|클래스|모듈|테스트|브랜치|커밋|풀리퀘|이슈|로그|문서|스크립트|패키지|의존성|버그|에러|오류|기능|설정|화면|페이지|엔드포인트|데이터베이스|스키마|마이그레이션|서비스'
    r'|\b(?:file|files|repo|repository|project|folder|directory|function|class|module|tests?|branch|commit|pr|issue|logs?|docs?|script|package|bug|error|config|feature|endpoint|database|schema|migration|service|readme)\b'
    r'|[\w.-]*/[\w./-]+|\b[\w-]+\.(?:py|md|js|ts|tsx|jsx|json|ya?ml|toml|txt|sh|html|css|sql|ini|cfg|lock)\b',
    re.I)
_WORK_VERB = re.compile(
    r'수정|고쳐|고치|삭제|지워|지우|만들어|만들|생성|추가|구현|작성|써\s*(?:줘|주|놔|둬)|리팩터|리팩토링|커밋|푸시|배포|설치|실행|돌려|빌드|읽고|읽어|열어|요약|분석|정리해|바꿔|변경|업데이트|적용|반영|머지|병합|옮겨|이동|복사|저장|찾아|검색해|검토|리뷰|작업해|살펴|점검|확인해|고쳐줘'
    r'|\b(?:edit|modify|fix|delete|remove|create|add|implement|write|refactor|commit|push|deploy|install|run|build|read|open|summari[sz]e|update|rename|move|merge|review|check|inspect|apply)\b',
    re.I)
_WORK_STRONG = re.compile(
    r'구현해|리팩터링해|리팩토링해|커밋해|푸시해|배포해|머지해|작업\s*(?:시작|해줘|해주세요|진행해줘)'
    r'|\b(?:implement|refactor|deploy)\b', re.I)

_YES_NO = (
    r'좋아|좋아요|좋습니다|좋네|네|예|응|ㅇㅇ|ㅇㅋ|그래|그래요|오케이|오케|콜|고고|승인|허용|맞아|맞아요|그걸로|그거로|그렇게\s*해\s*줘|그렇게\s*해|그렇게\s*하자'
    r'|진행|진행해|진행해줘|진행해주세요|진행하세요|진행하자|계속|계속해|계속해줘|아니|아니요|아니오|아뇨|싫어|거절'
    r'|ok|okay|yes|yep|yeah|sure|no|nope|go|go\s+ahead|proceed|approve|approved|continue|do\s+it'
)
_ORDINAL = r'(?:첫|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*번\s*째|첫째|둘째|셋째|넷째|마지막|맨\s*위|맨\s*아래|전자|후자'
_CHOICE_TAIL = r'(?:\s*(?:거|것|꺼|안|옵션|선택지|방법))?(?:\s*(?:으로|로|이요|요|이|가|을|를))?(?:\s*(?:해\s*줘|해\s*주세요|해|할게|갈게|가자|하자|선택|선택할게|좋아|부탁해|부탁드려요|써\s*줘|써))?'
_DO_TAIL = r'(?:\s*(?:해\s*줘|해\s*주세요|해|할게|갈게|가자|하자|써\s*줘|써\s*주세요|써|부탁해|부탁드려요|바꿔\s*줘|가\s*줘|주세요))?'
_ANSWERS = (
    re.compile(rf'(?:{_YES_NO})(?:\s*(?:요|임|입니다))?', re.I),
    re.compile(rf'(?:\d{{1,2}}|[①-⑩])\s*번{_CHOICE_TAIL}', re.I),
    re.compile(rf'(?:{_ORDINAL}){_CHOICE_TAIL}', re.I),
    re.compile(rf'[^\s?？]{{1,20}}(?:\s+[^\s?？]{{1,20}}){{0,2}}\s*(?:나|이나|랑|하고|,)?\s*(?:[^\s?？]{{1,20}}\s*)?(?:말고|대신에?)\s+[^\s?？]{{1,20}}\s*(?:으로|로){_DO_TAIL}', re.I),
    re.compile(rf'[^\s?？]{{1,20}}(?:으로|로){_DO_TAIL}', re.I),
    re.compile(r'(?:option|choice)\s*#?\s*\d{1,2}|#?\d{1,2}|(?:the\s+)?(?:first|second|third|fourth|last)(?:\s+(?:one|option))?'
               r'|(?:go\s+with|use|pick|choose|let\'?s\s+(?:go\s+with|use))\s+[\w-]{1,20}(?:\s+instead)?'
               r'|[\w-]{1,20}\s+instead(?:\s+of\s+[\w-]{1,20}(?:\s+(?:or|and)\s+[\w-]{1,20})?)?', re.I),
    # A choice with a follow-up condition, e.g. "alpha로 하되 진행 상황은 수시로 보고해줘".
    re.compile(r'[^\s?？]{1,20}\s*(?:으로|로)\s*(?:하되|하고|가되|가고|해서|하면서)\s+[^?？]{1,60}', re.I),
)
# A lone identifier-like name ("gamma", "status.py") picks a freeform option.
_NAME = re.compile(r'[A-Za-z][\w.\-]{0,39}')
_NOT_NAME = re.compile(
    r'(?:hi|hello|hey|thanks|help|status|progress|quota|usage|limits?|credits?|what|why|how|when|where|who|which|maybe|hmm+|um+|idk|asdf\w*|test|stop|cancel)',
    re.I)

_GREETING = re.compile(
    r'안녕|하이|ㅎㅇ|반가워|반갑|고마워|고맙|감사|수고|잘\s*자|좋은\s*아침|\b(?:hi|hello|hey|thanks|thank\s+you|good\s+(?:morning|evening|night))\b',
    re.I)
_QUESTION = re.compile(
    r'[?？]\s*$|뭐야|뭐예요|뭔가요|뭘까|뭐지|뭐니|무엇|왜|어떻게|어때|어떤|어디|언제|누구|생각해|생각은|의견|설명해|설명\s*좀|알려\s*줘|알려\s*주세요|궁금|가능해|가능할까|가능한가|할\s*수\s*있|되나요|될까|인가요|일까|나요\s*$|니\s*$|냐\s*$|까\s*$|거야\s*$|건가'
    r'|\b(?:what|why|how|when|where|who|which|can\s+you|could\s+you|do\s+you|is\s+it|are\s+you|explain|tell\s+me|thoughts?|opinion)\b',
    re.I)
_SPLIT = re.compile(r'[?？!！,，;；。\n]|\.(?=\s|$)|\s+(?:그리고|그다음에?|다음에|그런\s*다음|그러고\s*나서|하고\s*나서|and\s+then|then|and|also)\s+', re.I)


def _norm(text):
    text = unicodedata.normalize('NFKC', text or '')
    return re.sub(r'\s+', ' ', text).strip()


def _clauses(text):
    return [part.strip() for part in _SPLIT.split(text) if part and part.strip()]


def _is_quota_clause(clause):
    if not (_QUOTA_NOUN.search(clause) or (_QUOTA_WINDOW.search(clause) and re.search(r'남|리셋|초기화|\b(?:left|remaining|reset)', clause, re.I))):
        return False
    if _QUOTA_ACTION.search(clause):
        return False
    return bool(_QUOTA_QUERY.search(clause))


def _is_work_clause(clause):
    if _WORK_STRONG.search(clause):
        return True
    return bool(_WORK_NOUN.search(clause) and _WORK_VERB.search(clause))


def _is_answer(text):
    body = text.strip(' .!！~')
    if not body or len(body) > 80 or re.search(r'[?？]', body) or '\n' in body:
        return False
    if _NAME.fullmatch(body):
        return not _NOT_NAME.fullmatch(body)
    return len(body) <= 40 and any(pattern.fullmatch(body) for pattern in _ANSWERS[:-1]) or bool(_ANSWERS[-1].fullmatch(body))


def _is_choice_question(text):
    body = text.rstrip(' ?？')
    return body != text.rstrip() and _is_answer(body)


def classify(text):
    text = _norm(text)
    if not text:
        return AMBIGUOUS
    status = status_query(text)
    if status == 'status':
        return STATUS
    quoted_free = re.sub(_QUOTES, ' ', text)
    clauses = _clauses(text) or [text]
    quota = not _PAST.search(text) and any(_is_quota_clause(part) for part in _clauses(quoted_free))
    # A quota phrase that only appears quoted or as a past question is talk about it, not a request.
    recalled = not quota and any(_is_quota_clause(part) for part in clauses)
    work = any(_is_work_clause(part) for part in clauses)
    if quota and (work or status == 'mixed'):
        return AMBIGUOUS
    if quota:
        return QUOTA
    if status == 'mixed':
        return AMBIGUOUS
    if work:
        return WORK
    if _is_answer(text):
        return ANSWER
    if _is_choice_question(text):
        # "1번?" may be a confirmation or a question; never settle it as an answer.
        return AMBIGUOUS
    if recalled or _GREETING.search(text) or _QUESTION.search(text):
        return CONVERSATION
    return AMBIGUOUS

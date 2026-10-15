from __future__ import annotations

import hashlib
from html import escape as html_escape
import json
import re
from dataclasses import dataclass
from typing import Any

from adapters.gemini_rest import GeminiInterpreter, Interpretation, InterpretationUnavailable
from app.ids import canonical_task_id
from app.visual import is_visual_deliverable


TASK_ID_RE = re.compile(r"\bT-\d{8}-[0-9a-f]{6}\b", re.IGNORECASE)
FIELD_LABELS = {
    "objective": "목표",
    "output_format": "산출물 형식",
    "boundaries": "범위 밖",
    "acceptance": "완료 기준",
}
READ_COMMANDS = {"/hub": "hub", "/git": "git", "/work": "work", "/model": "model", "/jobs": "jobs", "/codex": "codex"}
# 인자를 받는 읽기 전용·알림 설정 명령. 승인 없이 처리하되 경로 검사는 실행 단계에서 한다.
UNQUIET_VALUES = {"unquiet", "unmute", "quiet off", "quiet_off", "조용히 해제", "해제"}
ARG_COMMANDS = {"/files": "files", "/logs": "logs", "/quiet": "quiet", "/alerts": "alerts", "/brief": "brief"}
HINT_RE = re.compile(r"[^0-9A-Za-z가-힣 _.\-]+")
USAGE = {
    "/new": (
        "작업 내용을 함께 적어 주세요.\n"
        "예: <code>/new 메모리 조회 API 추가, 테스트 포함</code>\n"
        "목표가 분명하면 발주 카드가 뜨고, 🚀 착수 버튼을 눌러야 시작합니다."
    ),
    "/task": "작업 ID를 함께 적어 주세요.\n예: <code>/task T-20261009-abc123</code>\n작업 ID는 /tasks 에서 볼 수 있습니다.",
    "/stop": "중단할 작업 ID를 함께 적어 주세요.\n예: <code>/stop T-20261009-abc123</code>\n진행 중 작업은 /status 에서 볼 수 있습니다.",
    "/retry": "다시 시도할 작업 ID를 함께 적어 주세요.\n예: <code>/retry T-20261009-abc123</code>\n실패·차단된 작업만 다시 시도할 수 있고, 버튼 승인을 거칩니다.",
    "/todo": (
        "Codex·Claude 에게 할 일을 남깁니다(agent-hub/todo, 커밋).\n"
        "예: <code>/todo codex 로그인 화면 문구 정리</code> · <code>/todo claude 테스트 보강</code> · <code>/todo any …</code>\n"
        "<code>/todo</code> 만 보내면 열린 할 일 목록, <code>/todo done 번호</code> 로 닫습니다."
    ),
    "/visual": (
        "영상·모션·이미지·디자인 시안을 Codex 에게 넘깁니다(agent-hub/visual 요청 + 할 일, 승인 없음).\n"
        "예: <code>/visual 홈페이지 온라인 교육 메뉴용 모션 그래픽</code> · <code>/visual ionenet-hero: 히어로 2안 다듬기</code>\n"
        "Codex 가 판단 카드를 올리면 텔레그램으로 알려 드립니다."
    ),
    "unknown": "모르는 명령입니다. /help 에서 명령 목록을 확인해 주세요.",
}


@dataclass(frozen=True)
class Decision:
    intent: str
    task_id: str | None = None
    project_id: str | None = None
    specification: dict[str, Any] | None = None
    needs_confirmation: bool = False
    message: str = ""
    argument: str = ""


class Policy:
    def __init__(
        self, interpreter: GeminiInterpreter, project_ids: list[str], descriptions: dict[str, str] | None = None
    ):
        self.interpreter = interpreter
        self.project_ids = project_ids
        # 프로젝트 ID → 저장소 한 줄 설명(projects.toml description). 발주 프로젝트 확인에 쓴다.
        self.descriptions = dict(descriptions or {})

    @property
    def policy_hash(self) -> str:
        source = {
            "version": 1,
            "projects": sorted(self.project_ids),
            "intents": [
                "status",
                "list_tasks",
                "task_detail",
                "create_task",
                "stop_task",
                "resume_task",
                "verify",
                "merge",
                "push",
                "deploy",
                "discard",
            ],
        }
        return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()

    def decide(self, text: str) -> Decision:
        """고정 규칙 → 진행상황 휴리스틱 → Gemini 의도 분류 (구형 경로, 대화층 없이 쓸 때)."""
        fixed = self.decide_fixed(text)
        if fixed is not None:
            return fixed
        normalized = text.strip()
        lowered = normalized.lower()
        if not TASK_ID_RE.search(normalized) and any(
            word in lowered for word in ("어디까지", "진행 상황", "진행상황")
        ):
            return Decision(intent="status")
        try:
            interpreted = self.interpreter.interpret(normalized, self.project_ids)
        except InterpretationUnavailable as exc:
            return Decision(
                intent="unknown",
                message=f"자연어 해석기를 사용할 수 없습니다: {exc}. /status, /tasks, /new 명령을 사용할 수 있습니다.",
            )
        return self._from_interpretation(interpreted, normalized)

    def orca_intent(self, text: str) -> Decision | None:
        """Explicit manual control, available even when every chat provider is down."""
        if text.strip().startswith("/"):
            return None
        lowered = text.strip().lower()
        if re.search(r"(누가|어떤 모델|무슨 모델).*(작업|진행|담당)|작업.*(누가|진행 상황|진행상황)", lowered):
            return Decision(intent="orca_status")
        from app.orca_handoff import CHOICES
        # Target follows the agent name; the source agent ('클로드 한도') is not the target.
        choices = "|".join(re.escape(c) for c in sorted(CHOICES,key=len,reverse=True))
        targets = re.findall(r"(?<![a-z])("+choices+r")(?:으로|로|에게|한테|에)\s*(?:바꿔서\s*)?(?:이어|넘겨|전환|바꿔|계속|인계)",lowered)
        # Elliptical correction still requires an explicit source and target model.
        correction = re.search(r"(?<![a-z])(?:"+choices+r")\s*말고\s*("+choices+r")(?:으로|로)\s*[.!]?\s*$", lowered)
        if correction and not targets:
            targets = [correction.group(1)]
        switching = bool(re.search(r"이어\s*줘|넘겨\s*줘|전환해|바꿔서\s*계속|인계해|(?:넘기|바꾸|전환하|인계하)지\s*(?:마|말아)|전환\s*취소",lowered))
        if not switching and not targets:
            return None
        if re.search(r"그(?:거|게|\s*작업)?\s*(?:아니라|말고)|아니[,， ]", lowered) and not correction:
            return Decision(intent="orca_clarify", message="앞서 고른 작업을 바꾸려는 뜻인가요? 이어갈 작업 하나와 모델을 알려 주세요.")
        if re.search(r"하지\s*마(?:\s|$|[.!?])|(?:넘기|바꾸|전환하|인계하)지\s*(?:마|말아|말고)|전환\s*취소|어떻게|방법|가능|할까|하면|라면|지\s*마(?:\s|$|[.!?])",lowered):
            return Decision(intent="orca_clarify",message="실행 요청인지 확인해 주세요. 전환하려면 /orca switch 작업ID codex|claude|astra|sol 로 보내 주세요")
        if len(targets) != 1:
            return Decision(intent="orca_clarify",message="어떤 작업을 어느 모델로 이어갈까요? /orca switch 작업ID codex|claude|astra|sol")
        return Decision(intent="orca_switch",argument=targets[0])

    def decide_fixed(self, text: str) -> Decision | None:
        """LLM 없이 결정되는 명령. None 이면 대화 응답층으로 넘긴다."""
        normalized = text.strip()
        lowered = normalized.lower()
        command = lowered.split(maxsplit=1)[0] if lowered else ""
        if command.startswith("/") and "@" in command:
            command = command.split("@", 1)[0]
        argument = normalized.split(maxsplit=1)[1].strip() if len(normalized.split(maxsplit=1)) > 1 else ""
        task_match = TASK_ID_RE.search(normalized)
        task_id = canonical_task_id(task_match.group(0)) if task_match else None

        if lowered in {"도움말", "help"} or command in {"/start", "/help"}:
            return Decision(intent="help")
        if lowered in {"상태", "서버 상태", "상태 알려줘"} or command == "/status":
            return Decision(intent="status")
        if lowered in {"작업 목록", "태스크 목록"} or command == "/tasks":
            return Decision(intent="list_tasks")
        if command in READ_COMMANDS:
            return Decision(intent=READ_COMMANDS[command])
        if command in ARG_COMMANDS:
            return Decision(intent=ARG_COMMANDS[command], argument=clean_hint(argument))
        if command == "/task":
            if task_id:
                return Decision(intent="task_detail", task_id=task_id)
            return Decision(intent="usage", message=USAGE["/task"])
        if command == "/stop":
            if task_id:
                return Decision(intent="stop_task", task_id=task_id, needs_confirmation=False)
            return Decision(intent="usage", message=USAGE["/stop"])
        if command == "/retry":
            if task_id:
                return Decision(intent="retry", task_id=task_id)
            return Decision(intent="usage", message=USAGE["/retry"])
        if command == "/todo":
            return todo_decision(argument)
        if command == "/visual":
            if not argument:
                return Decision(intent="usage", message=USAGE["/visual"])
            match = re.match(r"^([a-z0-9][a-z0-9_-]{1,40})\s*[:：]\s*(.+)$", argument, re.S)
            if match:
                return Decision(intent="visual_request", project_id=match.group(1), argument=match.group(2).strip())
            return Decision(intent="visual_request", argument=argument)
        if command == "/new":
            if not argument:
                return Decision(intent="usage", message=USAGE["/new"])
            return self._new_task_command(argument)
        if lowered.startswith(("개발:", "구현:", "작업:")):
            return self._new_task_command(normalized.split(":", 1)[1].strip())
        if command.startswith("/"):
            return Decision(intent="usage", message=USAGE["unknown"])
        return None

    def from_chat_action(self, action: Any, original: str) -> Decision | None:
        """대화 모델이 제안한 동작을 정책 결정으로 바꾼다. 실행 여부는 여기와 승인 버튼이 정한다."""
        kind = getattr(action, "type", "none")
        task_id = getattr(action, "task_id", None)
        if kind == "create_task":
            objective = (getattr(action, "objective", "") or "").strip()
            if not objective:
                return None
            project_id = getattr(action, "project_id", None)
            if project_id:
                # 등록되지 않은 프로젝트를 기본 프로젝트로 몰래 바꾸지 않는다. 답변만 남기고 동작은 버린다.
                if project_id not in self.project_ids:
                    return None
            # 프로젝트를 말하지 않았으면 _task_from_text 가 기본 프로젝트(첫 등록)로 둔다(분명히 안 맞으면 되묻는다).
            # 발주 카드에 프로젝트가 보이고 착수 버튼을 눌러야 시작한다.
            interpretation = Interpretation.from_dict(
                {
                    "intent": "create_task",
                    "project_id": project_id,
                    "objective": objective,
                    "output_format": getattr(action, "output_format", ""),
                    "boundaries": list(getattr(action, "boundaries", []) or []),
                    "acceptance": list(getattr(action, "acceptance", []) or []),
                }
            )
            return self._task_from_text(original, interpretation)
        if kind == "visual_request":
            # 넘기는 일(C): 시각·디자인·모션은 Codex 에게 시안 요청으로 넘긴다. 되돌릴 수 있는 기록이라 승인 없이 한다.
            text = (getattr(action, "objective", "") or "").strip() or original.strip()
            project = getattr(action, "project_id", None)
            return Decision(intent="visual_request", argument=text[:1_500], project_id=project or None)
        if kind == "send_files":
            # 읽기 전용 전달: 승인 없이 결과물 목록 버튼을 띄운다. 실제 파일은 경로 검사 후 버튼으로만 보낸다.
            return Decision(intent="files", argument=clean_hint(getattr(action, "job_hint", None) or ""))
        if kind == "set_alerts":
            # 값: on(먼저 알리기 켜기 + 조용히 해제) | off(치명적인 것만) | quiet <기간> | unquiet(조용히만 해제) | status
            setting = clean_hint(getattr(action, "alerts", None) or "").lower()
            if setting in {"on", "off", "status", ""}:
                return Decision(intent="alerts", argument="" if setting == "status" else setting)
            if setting in UNQUIET_VALUES:
                return Decision(intent="quiet", argument="off")
            if setting.startswith("quiet"):
                rest = setting.removeprefix("quiet").strip()
                return Decision(intent="quiet", argument="off" if rest in {"off", "해제"} else rest)
            return Decision(intent="quiet", argument=setting)
        if kind == "dispatch_todo":
            # agent-hub 할 일 남기기: 되돌릴 수 있는 기록이라 승인 없이 하되 답장으로 확인한다.
            assignee = str(getattr(action, "assignee", None) or "").lower()
            text = (getattr(action, "todo_text", None) or "").strip()
            if assignee not in TODO_ASSIGNEES or not text:
                return None
            return Decision(intent="todo_add", argument=text, project_id=assignee)
        if kind in {"task_detail", "stop_task", "merge", "push", "deploy"}:
            if not task_id or not TASK_ID_RE.fullmatch(task_id):
                return None
            # 중단도 모델 제안만으로 실행하지 않고 버튼 확인을 거친다.
            return Decision(intent=kind, task_id=canonical_task_id(task_id), needs_confirmation=kind != "task_detail")
        return None

    @property
    def default_project(self) -> str:
        """projects.toml 의 첫 프로젝트. 프로젝트를 말하지 않은 발주는 여기로 간다(카드에 프로젝트가 보인다)."""
        return self.project_ids[0]

    def split_project(self, text: str) -> tuple[str | None, str]:
        """'/new sandbox: 할 일' · '/new Sandbox 할 일' → ('sandbox', '할 일'). 등록 ID 가 아니면 (None, 원문).
        프로젝트 이름은 대소문자를 가리지 않는다."""
        stripped = text.strip()
        registered = {project_id.lower(): project_id for project_id in self.project_ids}
        match = re.match(r"^@?([A-Za-z0-9][A-Za-z0-9_-]{1,40})\s*[:：]?\s+(.*)$", stripped, re.S)
        if match and match.group(1).lower() in registered:
            return registered[match.group(1).lower()], match.group(2).strip()
        match = re.match(r"^@?([A-Za-z0-9][A-Za-z0-9_-]{1,40})\s*[:：]\s*(.*)$", stripped, re.S)
        if match and match.group(1).lower() in registered:
            return registered[match.group(1).lower()], match.group(2).strip()
        return None, stripped

    def unknown_project_selector(self, text: str) -> str | None:
        """발주 명령(/new·개발: 등) 바로 뒤의 '이름:' 이 등록되지 않은 프로젝트 지정으로 보이면 그 이름.

        규칙(평범한 글의 콜론과 구분): 첫 단어가 소문자 영숫자·_·- 2~40자이고 바로 뒤에 콜론이 붙어 있으며
        흔한 머리말(fix·docs·readme 등)이 아닐 때만 프로젝트 지정으로 본다. 'README: 실행 방법 추가' 처럼
        대문자가 섞였거나 한글·공백이 있으면 평범한 작업 문장이다(등록 프로젝트는 대소문자 무관하게 split_project 가 먼저 잡는다).
        """
        match = re.match(r"^@?([a-z0-9][a-z0-9_-]{1,39})[:：]", text.strip())
        if not match:
            return None
        name = match.group(1)
        if name in {project_id.lower() for project_id in self.project_ids} or name in SELECTOR_STOPWORDS:
            return None
        return name

    def project_question(self, request: str) -> str | None:
        """프로젝트를 말하지 않은 발주가 기본 프로젝트와 분명히 안 맞으면(예: 홈페이지 작업인데 기본은 API 서버) 되묻는 문구.

        규칙: 요청에 사이트·브랜드 낱말(DOMAIN_TERMS)이 있는데 어느 등록 프로젝트의 ID·설명에도 그 낱말이 없을 때만 묻는다.
        그 밖에는 지금처럼 기본 프로젝트로 두고 카드에 프로젝트를 보여 준다(착수 버튼을 눌러야 시작)."""
        lowered = request.lower()
        terms = [term for term in DOMAIN_TERMS if term in lowered]
        if not terms:
            return None
        haystack = " ".join(f"{pid} {self.descriptions.get(pid, '')}".lower() for pid in self.project_ids)
        if any(term in haystack for term in terms):
            return None
        listing = "\n".join(
            f"• {pid} — {html_escape(self.descriptions.get(pid) or '설명 없음')}" for pid in self.project_ids
        )
        return (
            f"어느 저장소 작업인가요? '{html_escape(terms[0])}' 작업은 등록된 저장소와 맞지 않아 기본 프로젝트로 보내지 않았습니다.\n"
            f"{listing}\n"
            "맞는 저장소가 있으면 <code>/new 프로젝트: 내용</code> 으로 다시 보내 주세요. "
            "없으면 대표가 저장소를 만들고 config/projects.toml 에 등록해야 합니다(봇이 할 수 없는 일). "
            "디자인·영상이면 <code>/visual 내용</code> 으로 Codex 에게 넘길 수 있습니다."
        )

    def _new_task_command(self, argument: str) -> Decision:
        project_id, request = self.split_project(argument)
        if project_id is None:
            unknown = self.unknown_project_selector(argument)
            if unknown is not None:
                return Decision(
                    intent="unknown",
                    message=(
                        f"등록되지 않은 프로젝트입니다: <code>{unknown}</code> (가능: {', '.join(self.project_ids)})\n"
                        "프로젝트 지정이 아니라면 콜론 없이 적어 주세요."
                    ),
                )
        if not request:
            return Decision(intent="usage", message=USAGE["/new"])
        return self._task_from_text(request, None, project_id=project_id)

    def _task_from_text(
        self, request: str, interpretation: Interpretation | None, *, project_id: str | None = None
    ) -> Decision:
        if not request:
            return Decision(intent="unknown", message="작업 내용을 입력해 주세요.")
        objective = (interpretation.objective if interpretation else "") or ""
        output_format = (interpretation.output_format if interpretation else "") or ""
        if is_visual_deliverable(request, objective, output_format):
            # 작업자는 등록 저장소 파일만 고친다. 영상·모션·이미지·디자인 결과물은 발주 카드 대신 Codex 시안 요청으로 넘긴다.
            return Decision(intent="visual_suggest", argument=request, message=VISUAL_SUGGEST)
        explicit = project_id or (interpretation.project_id if interpretation else None)
        if not explicit:
            question = self.project_question(request)
            if question:
                return Decision(intent="ask_project", message=question)
        if project_id is None:
            project_id = explicit or self.default_project
        if project_id not in self.project_ids:
            return Decision(intent="unknown", message="등록되지 않은 프로젝트입니다.")
        specification = {
            "objective": (interpretation.objective if interpretation else "") or request,
            "output_format": (interpretation.output_format if interpretation else "")
            or "변경사항과 검증 결과",
            "boundaries": (interpretation.boundaries if interpretation else [])
            or ["병합·push·배포 제외"],
            "acceptance": (interpretation.acceptance if interpretation else [])
            or ["등록된 검증 명령 통과"],
            "original_request": request,
        }
        return Decision(
            intent="create_task",
            project_id=project_id,
            specification=specification,
            needs_confirmation=True,
        )

    def _from_interpretation(self, result: Interpretation, original: str) -> Decision:
        if result.intent == "create_task":
            if result.missing_fields:
                return Decision(
                    intent="unknown",
                    message="확인이 필요합니다: "
                    + ", ".join(FIELD_LABELS.get(name, name) for name in result.missing_fields),
                )
            return self._task_from_text(original, result)
        if result.intent in {"merge", "push", "deploy", "discard", "resume_task", "verify"}:
            return Decision(
                intent=result.intent,
                task_id=result.task_id,
                needs_confirmation=True,
                message=result.summary,
            )
        return Decision(
            intent=result.intent,
            task_id=result.task_id,
            project_id=result.project_id,
            message=result.summary,
        )


# 사이트·브랜드 작업을 가리키는 낱말. 등록 프로젝트 설명에 없으면 기본 프로젝트로 보내지 않고 묻는다.
DOMAIN_TERMS = ("홈페이지", "웹사이트", "사이트", "랜딩", "쇼핑몰", "블로그", "website", "homepage", "landing", "blog")
VISUAL_SUGGEST = (
    "🎨 이건 코드 작업이 아니라 시각 결과물(영상·모션·이미지·디자인) 요청이라 발주 카드를 만들지 않았습니다.\n"
    "작업자는 등록 저장소의 파일만 고칠 수 있고 영상·이미지를 만들거나 렌더하지 못합니다.\n"
    "대신 Codex 에게 시안을 요청할 수 있습니다: <code>/visual 내용</code> (agent-hub/visual 요청 + Codex 할 일). "
    "Codex 가 판단 카드를 올리면 텔레그램으로 알려 드리고, 고른 안은 DeskMini 에서 Blender 로 렌더합니다."
)
# '/new fix: …' 처럼 커밋 머리말·흔한 낱말에 콜론을 붙인 것은 프로젝트 지정으로 보지 않는다.
SELECTOR_STOPWORDS = frozenset({
    "fix", "feat", "docs", "doc", "test", "tests", "chore", "refactor", "style", "perf", "build", "ci",
    "todo", "note", "bug", "readme", "api", "ui", "ux", "db", "cli", "http", "https", "ex", "eg",
})
TODO_ASSIGNEES = ("codex", "claude", "any")
TODO_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9-]{1,60}$")


def todo_decision(argument: str) -> Decision:
    """/todo → 목록, /todo <codex|claude|any> 내용 → 추가, /todo done <id> → 닫기."""
    parts = argument.split(maxsplit=1)
    if not parts:
        return Decision(intent="todo_list")
    head = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if head in {"list", "목록"}:
        return Decision(intent="todo_list")
    if head in {"done", "완료"}:
        if TODO_ID_RE.fullmatch(rest) or re.fullmatch(r"\d{1,3}", rest):
            return Decision(intent="todo_done", argument=rest)
        return Decision(intent="usage", message=USAGE["/todo"])
    if head in TODO_ASSIGNEES and rest:
        return Decision(intent="todo_add", argument=rest, project_id=head)
    return Decision(intent="usage", message=USAGE["/todo"])


def clean_hint(value: str, limit: int = 80) -> str:
    """작업 힌트·명령 인자. 경로 구분자·셸 문자를 지운 짧은 문자열만 남긴다(경로로 쓰지 않는다)."""
    return " ".join(HINT_RE.sub(" ", str(value or "")).split())[:limit]

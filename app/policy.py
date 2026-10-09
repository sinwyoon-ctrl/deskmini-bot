from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from adapters.gemini_rest import GeminiInterpreter, Interpretation, InterpretationUnavailable
from app.ids import canonical_task_id


TASK_ID_RE = re.compile(r"\bT-\d{8}-[0-9a-f]{6}\b", re.IGNORECASE)
FIELD_LABELS = {
    "objective": "목표",
    "output_format": "산출물 형식",
    "boundaries": "범위 밖",
    "acceptance": "완료 기준",
}
READ_COMMANDS = {"/hub": "hub", "/git": "git", "/work": "work", "/model": "model"}
USAGE = {
    "/new": (
        "작업 내용을 함께 적어 주세요.\n"
        "예: <code>/new 메모리 조회 API 추가, 테스트 포함</code>\n"
        "목표가 분명하면 발주 카드가 뜨고, 🚀 착수 버튼을 눌러야 시작합니다."
    ),
    "/task": "작업 ID를 함께 적어 주세요.\n예: <code>/task T-20261009-abc123</code>\n작업 ID는 /tasks 에서 볼 수 있습니다.",
    "/stop": "중단할 작업 ID를 함께 적어 주세요.\n예: <code>/stop T-20261009-abc123</code>\n진행 중 작업은 /status 에서 볼 수 있습니다.",
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


class Policy:
    def __init__(self, interpreter: GeminiInterpreter, project_ids: list[str]):
        self.interpreter = interpreter
        self.project_ids = project_ids

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
        if command == "/task":
            if task_id:
                return Decision(intent="task_detail", task_id=task_id)
            return Decision(intent="usage", message=USAGE["/task"])
        if command == "/stop":
            if task_id:
                return Decision(intent="stop_task", task_id=task_id, needs_confirmation=False)
            return Decision(intent="usage", message=USAGE["/stop"])
        if command == "/new":
            if not argument:
                return Decision(intent="usage", message=USAGE["/new"])
            return self._task_from_text(argument, None)
        if lowered.startswith(("개발:", "구현:", "작업:")):
            request = normalized.split(":", 1)[1].strip()
            if not request:
                return Decision(intent="usage", message=USAGE["/new"])
            return self._task_from_text(request, None)
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
            elif len(self.project_ids) == 1:
                project_id = self.project_ids[0]
            else:
                return None
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
        if kind in {"task_detail", "stop_task", "merge", "push", "deploy"}:
            if not task_id or not TASK_ID_RE.fullmatch(task_id):
                return None
            # 중단도 모델 제안만으로 실행하지 않고 버튼 확인을 거친다.
            return Decision(intent=kind, task_id=canonical_task_id(task_id), needs_confirmation=kind != "task_detail")
        return None

    def _task_from_text(self, request: str, interpretation: Interpretation | None) -> Decision:
        if not request:
            return Decision(intent="unknown", message="작업 내용을 입력해 주세요.")
        project_id = interpretation.project_id if interpretation else self.project_ids[0]
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

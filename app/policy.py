from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from adapters.gemini_rest import GeminiInterpreter, Interpretation, InterpretationUnavailable
from app.ids import canonical_task_id


TASK_ID_RE = re.compile(r"\bT-\d{8}-[0-9a-f]{6}\b", re.IGNORECASE)


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
        normalized = text.strip()
        lowered = normalized.lower()
        task_match = TASK_ID_RE.search(normalized)
        task_id = canonical_task_id(task_match.group(0)) if task_match else None

        if lowered in {"/start", "/help", "도움말", "help"}:
            return Decision(intent="help")
        if lowered in {"/status", "상태", "서버 상태", "상태 알려줘"}:
            return Decision(intent="status")
        if not task_id and any(word in lowered for word in ("어디까지", "진행 상황", "진행상황")):
            return Decision(intent="status")
        if lowered in {"/tasks", "작업 목록", "태스크 목록"}:
            return Decision(intent="list_tasks")
        if lowered.startswith("/task ") and task_id:
            return Decision(intent="task_detail", task_id=task_id)
        if lowered.startswith("/stop ") and task_id:
            return Decision(intent="stop_task", task_id=task_id, needs_confirmation=False)
        if lowered.startswith("/new "):
            return self._task_from_text(normalized[5:].strip(), None)
        if lowered.startswith(("개발:", "구현:", "작업:")):
            request = normalized.split(":", 1)[1].strip()
            return self._task_from_text(request, None)

        try:
            interpreted = self.interpreter.interpret(normalized, self.project_ids)
        except InterpretationUnavailable as exc:
            return Decision(
                intent="unknown",
                message=f"자연어 해석기를 사용할 수 없습니다: {exc}. /status, /tasks, /new 명령을 사용할 수 있습니다.",
            )
        return self._from_interpretation(interpreted, normalized)

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
                    message="확인이 필요합니다: " + ", ".join(result.missing_fields),
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

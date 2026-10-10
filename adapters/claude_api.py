from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.redact import redact_text


class ClaudeUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaudeResult:
    data: dict[str, Any]
    model: str
    usage: dict[str, int] = field(default_factory=dict)


class ClaudeChatClient:
    """Anthropic Messages API 로 구조화된 JSON 답을 받는다. 키는 호출마다 명시적으로 넘긴다."""

    def __init__(
        self,
        *,
        model: str,
        effort: str = "low",
        max_tokens: int = 2_000,
        timeout_seconds: float = 60.0,
        client_factory: Any = None,
    ):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.client_factory = client_factory
        self.workspace_id = ""

    def _client(self, api_key: str) -> Any:
        if self.client_factory is not None:
            return self.client_factory(api_key)
        try:
            import anthropic
        except ImportError as exc:
            raise ClaudeUnavailable("anthropic SDK not installed") from exc
        headers = {"anthropic-workspace-id": self.workspace_id} if self.workspace_id else None
        return anthropic.Anthropic(
            api_key=api_key, timeout=self.timeout_seconds, max_retries=1, default_headers=headers
        )

    def complete(
        self,
        *,
        api_key: str,
        system_prompt: str,
        context_text: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
    ) -> ClaudeResult:
        client = self._client(api_key)
        try:
            import anthropic

            api_errors: tuple[type[BaseException], ...] = (anthropic.APIError,)
        except ImportError:
            api_errors = ()
        system = [
            # 고정 지침을 먼저 두고 캐시한다. 매 턴 바뀌는 상태 스냅샷은 그 뒤에 둔다.
            {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": context_text},
        ]
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=messages,
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except api_errors as exc:
            status = getattr(exc, "status_code", None)
            # API 오류 본문(키 없음)을 마스킹해서 남긴다. 400 원인을 알 수 없던 문제(10/10) 때문에 추가.
            detail = redact_text(str(getattr(exc, "message", "") or exc), 300)
            raise ClaudeUnavailable(f"{type(exc).__name__}{f' {status}' if status else ''}: {detail}") from exc
        if getattr(response, "stop_reason", None) == "refusal":
            raise ClaudeUnavailable("refusal")
        text = next(
            (block.text for block in response.content if getattr(block, "type", "") == "text"),
            "",
        )
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ClaudeUnavailable(
                f"invalid JSON (stop_reason={getattr(response, 'stop_reason', None)})"
            ) from exc
        if not isinstance(data, dict):
            raise ClaudeUnavailable("JSON object expected")
        return ClaudeResult(
            data=data,
            model=redact_text(str(getattr(response, "model", self.model)), 80),
            usage=_usage(getattr(response, "usage", None)),
        )


def _usage(usage: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = getattr(usage, name, None)
        if isinstance(value, int):
            result[name] = value
    return result

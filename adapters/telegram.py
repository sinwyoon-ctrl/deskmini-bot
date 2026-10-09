from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class TelegramError(RuntimeError):
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class TelegramResponse:
    result: Any


class TelegramAPI:
    def __init__(self, token: str, request_timeout: int = 60):
        self._base_url = f"https://api.telegram.org/bot{token}"
        self.request_timeout = request_timeout

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        encoded = urllib.parse.urlencode(
            {
                key: json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list))
                else str(value)
                for key, value in (payload or {}).items()
                if value is not None
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/{method}",
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            retry_after = None
            try:
                body = json.load(exc)
                retry_after = (body.get("parameters") or {}).get("retry_after")
                description = body.get("description", f"HTTP {exc.code}")
            except Exception:
                description = f"HTTP {exc.code}"
            raise TelegramError(description, retry_after=retry_after) from exc
        except (OSError, ValueError) as exc:
            raise TelegramError(f"Telegram request failed: {type(exc).__name__}") from exc
        if not body.get("ok"):
            parameters = body.get("parameters") or {}
            raise TelegramError(
                body.get("description", "Telegram API rejected request"),
                retry_after=parameters.get("retry_after"),
            )
        return body.get("result")

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        return self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
        )

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        return self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text[:4096],
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
                "disable_web_page_preview": True,
            },
        )

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        return self._call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:4096],
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
                "disable_web_page_preview": True,
            },
        )

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        self._call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text[:200],
            },
        )

    def call_with_retry(self, method: str, payload: dict[str, Any], attempts: int = 3) -> Any:
        for attempt in range(attempts):
            try:
                return self._call(method, payload)
            except TelegramError as exc:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(min(exc.retry_after or (2**attempt), 30))
        raise AssertionError("unreachable")


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": callback_data} for label, callback_data in row]
            for row in rows
        ]
    }


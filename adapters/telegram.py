from __future__ import annotations

import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class TelegramError(RuntimeError):
    def __init__(self, message: str, retry_after: int | None = None, error_code: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after
        self.error_code = error_code

    @property
    def permanent(self) -> bool:
        """400(잘못된 HTML·본문 등)은 다시 보내도 같은 결과라 재시도하지 않는다."""
        return self.error_code == 400


@dataclass(frozen=True)
class TelegramResponse:
    result: Any


class TelegramAPI:
    def __init__(self, token: str, request_timeout: int = 60):
        self._base_url = f"https://api.telegram.org/bot{token}"
        self.request_timeout = request_timeout
        self.upload_timeout = 180

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        encoded = urllib.parse.urlencode(_form_fields(payload)).encode("utf-8")
        return self._post(method, encoded, "application/x-www-form-urlencoded", self.request_timeout)

    def _post(self, method: str, body: bytes, content_type: str, timeout: float) -> Any:
        request = urllib.request.Request(
            f"{self._base_url}/{method}",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            retry_after = None
            error_code = exc.code
            try:
                body = json.load(exc)
                retry_after = (body.get("parameters") or {}).get("retry_after")
                description = body.get("description", f"HTTP {exc.code}")
            except Exception:
                description = f"HTTP {exc.code}"
            raise TelegramError(description, retry_after=retry_after, error_code=error_code) from exc
        except (OSError, ValueError) as exc:
            raise TelegramError(f"Telegram request failed: {type(exc).__name__}") from exc
        if not body.get("ok"):
            parameters = body.get("parameters") or {}
            raise TelegramError(
                body.get("description", "Telegram API rejected request"),
                retry_after=parameters.get("retry_after"),
                error_code=body.get("error_code"),
            )
        return body.get("result")

    def get_file(self, file_id: str) -> dict[str, Any]:
        """getFile → {file_id, file_size, file_path}. 봇 API 는 20MB 까지만 내려받을 수 있다."""
        return self._call("getFile", {"file_id": file_id})

    def download_file(self, file_path: str, max_bytes: int) -> bytes:
        """getFile 의 file_path 를 내려받는다. 크기 상한을 넘으면 중간에 끊고 TelegramError."""
        if not FILE_PATH_RE.fullmatch(file_path or "") or ".." in file_path:
            raise TelegramError("Unexpected Telegram file path")
        url = self._base_url.replace("/bot", "/file/bot", 1) + "/" + urllib.parse.quote(file_path)
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=max(self.request_timeout, 60)) as response:
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = response.read(65_536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise TelegramError("File exceeds the size limit", error_code=413)
                    chunks.append(chunk)
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"HTTP {exc.code}", error_code=exc.code) from exc
        except (OSError, ValueError) as exc:
            raise TelegramError(f"Telegram download failed: {type(exc).__name__}") from exc
        return b"".join(chunks)

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

    def send_file(
        self,
        chat_id: int,
        kind: str,
        filename: str,
        content: bytes,
        *,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> dict[str, Any]:
        """sendVideo / sendPhoto / sendDocument 를 multipart 로 올린다(표준 라이브러리만)."""
        method, field = UPLOAD_METHODS.get(kind, UPLOAD_METHODS["document"])
        fields = {
            "chat_id": chat_id,
            "caption": caption[:1024] if caption else None,
            "parse_mode": parse_mode if caption else None,
            "reply_markup": reply_markup,
        }
        if method == "sendVideo":
            fields["supports_streaming"] = "true"
        body, content_type = build_multipart(_form_fields(fields), field, filename, content)
        return self._post(method, body, content_type, max(self.request_timeout, self.upload_timeout))

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



FILE_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,200}$")
UPLOAD_METHODS = {
    "video": ("sendVideo", "video"),
    "photo": ("sendPhoto", "photo"),
    "document": ("sendDocument", "document"),
}
CONTENT_TYPES = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json",
    "csv": "text/csv; charset=utf-8",
    "log": "text/plain; charset=utf-8",
}


def _form_fields(payload: dict[str, Any] | None) -> dict[str, str]:
    return {
        key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        for key, value in (payload or {}).items()
        if value is not None
    }


def safe_filename(name: str) -> str:
    """Content-Disposition 에 넣을 파일 이름. 따옴표·제어문자·경로 구분자를 지운다."""
    cleaned = re.sub(r'[\x00-\x1f\x7f"\\/]+', "_", name).strip() or "file"
    return cleaned[:120]


def build_multipart(
    fields: dict[str, str],
    file_field: str,
    filename: str,
    content: bytes,
    content_type: str | None = None,
    boundary: str | None = None,
) -> tuple[bytes, str]:
    """multipart/form-data 본문과 Content-Type 헤더 값. 네트워크와 무관한 순수 함수."""
    boundary = boundary or "----deskmini" + secrets.token_hex(16)
    name = safe_filename(filename)
    if content_type is None:
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        content_type = CONTENT_TYPES.get(extension, "application/octet-stream")
    marker = boundary.encode("ascii")
    if marker in content:  # 경계 문자열이 본문에 있으면 새로 만든다(사실상 일어나지 않음)
        return build_multipart(fields, file_field, filename, content, content_type, None)
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            b"--" + marker + b"\r\n"
            + f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8")
            + value.encode("utf-8") + b"\r\n"
        )
    parts.append(
        b"--" + marker + b"\r\n"
        + f'Content-Disposition: form-data; name="{file_field}"; filename="{name}"\r\n'.encode("utf-8")
        + f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
        + content + b"\r\n"
    )
    parts.append(b"--" + marker + b"--\r\n")
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"

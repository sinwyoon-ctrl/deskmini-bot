from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


# 이미 마스킹된 자리(평문 '<redacted' 또는 HTML 이스케이프된 '&lt;redacted')는 다시 잡지 않는다.
# 다시 잡으면 '&lt;' 만 먹고 '<redacted>;redacted&gt;' 같은 원시 태그가 생겨 Telegram HTML 이 400 으로 거절한다.
_DONE = r"(?!<redacted|&lt;redacted)"
SECRET_NAME_RE = re.compile(
    r"(?:^|[_-])(?:token|secret|password|passwd|api[_-]?key|authorization|cookie)(?:$|[_-])",
    re.IGNORECASE,
)
PATTERNS = (
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"), "<redacted>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "<redacted>"),
    (re.compile(r"\b(?:sk[-_]|ghp_|github_pat_)[A-Za-z0-9_-]{16,}\b", re.IGNORECASE), "<redacted>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{12,}"), r"\1<redacted>"),
    (re.compile(r"(?i)(https?://[^\s:/]+:)" + _DONE + r"[^@\s]+@"), r"\1<redacted>@"),
    (
        re.compile(
            r'(?i)(["\']?(?:password|passwd|token|secret|api[_-]?key|authorization|cookie)["\']?\s*[:=]\s*)"(?:\\.|[^"\\])*"'
        ),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r"(?i)([\"']?(?:password|passwd|token|secret|api[_-]?key|authorization|cookie)[\"']?\s*[:=]\s*)'(?:\\.|[^'\\])*'"
        ),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r"(?i)([\"']?(?:password|passwd|token|secret|api[_-]?key|authorization|cookie)[\"']?\s*[:=]\s*)" + _DONE + r"[^\s,;]+"
        ),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\Z)"
        ),
        "<redacted-private-key>",
    ),
)


def redact_text(value: str, limit: int = 12_000) -> str:
    text = value
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:limit]


_HTML_TAG_RE = re.compile(r"(<[^<>]*>)")
_RAW_MARKER_RE = re.compile(r"<(redacted(?:-private-key)?)>")


def redact_html(value: str, limit: int = 12_000) -> str:
    """Telegram HTML(parse_mode=HTML) 본문의 마지막 안전망 마스킹.

    태그 바깥 글자만 마스킹하고, 넣는 표시는 '&lt;redacted&gt;' 로 이스케이프해 원시 태그가 생기지 않게 한다.
    본문은 이스케이프 전에 redact_text 로 먼저 마스킹하는 것이 원칙이고, 이것은 놓친 것을 잡는 용도다.
    """
    parts = _HTML_TAG_RE.split(value)
    out = []
    for index, part in enumerate(parts):
        if index % 2 == 1:  # 봇이 만든 태그
            out.append(part)
            continue
        redacted = redact_text(part, len(part) + 1)
        out.append(_RAW_MARKER_RE.sub(r"&lt;\1&gt;", redacted) if redacted != part else part)
    return "".join(out)[:limit]


def contains_secret(value: str) -> bool:
    return redact_text(value, len(value) + 1) != value


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): "<redacted>" if SECRET_NAME_RE.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact(item) for item in value]
    return value

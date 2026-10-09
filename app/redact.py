from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


SECRET_NAME_RE = re.compile(
    r"(?:^|[_-])(?:token|secret|password|passwd|api[_-]?key|authorization|cookie)(?:$|[_-])",
    re.IGNORECASE,
)
PATTERNS = (
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"), "<redacted>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "<redacted>"),
    (re.compile(r"\b(?:sk[-_]|ghp_|github_pat_)[A-Za-z0-9_-]{16,}\b", re.IGNORECASE), "<redacted>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{12,}"), r"\1<redacted>"),
    (re.compile(r"(?i)(https?://[^\s:/]+:)[^@\s]+@"), r"\1<redacted>@"),
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
            r"(?i)([\"']?(?:password|passwd|token|secret|api[_-]?key|authorization|cookie)[\"']?\s*[:=]\s*)[^\s,;]+"
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

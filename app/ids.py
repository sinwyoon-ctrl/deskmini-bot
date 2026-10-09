from __future__ import annotations

import re


TASK_ID_RE = re.compile(r"^T-(\d{8})-([0-9a-f]{6})$", re.IGNORECASE)


def canonical_task_id(value: str) -> str:
    match = TASK_ID_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("Invalid task id")
    return f"T-{match.group(1)}-{match.group(2).lower()}"

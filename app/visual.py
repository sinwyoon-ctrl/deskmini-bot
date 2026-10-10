"""넘기는 일(hand-off): 시각·디자인·모션 요청을 Codex 에게 넘기고 결과를 받아 온다.

작업자(격리 Claude)는 등록 저장소의 파일만 고친다. 영상·이미지 렌더, 디자인 시안, 외부 도구가 필요한 일은 할 수 없다.
그런 요청은 ~/agent-hub/visual/<프로젝트>/<요청ID>/request.md 를 만들고 `hub todo add codex …` 로 알린다(고정 argv, 셸 없음).
Codex 가 card.md(판단 카드)를 쓰거나 할 일을 닫으면 봇이 텔레그램으로 결론 한 줄을 알린다(handoff_status).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.redact import redact_text
from app.todo import HubTodo


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
TODO_ID_OUT_RE = re.compile(r"할 일: ([0-9]{8}-[0-9]{6}-[a-z0-9-]{1,60})")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]+")

# 시각 결과물 판별(발주 카드 대신 넘기기를 제안). 강한 낱말은 그것만으로, 약한 낱말은 '만들기' 동사와 함께일 때만.
# 코드 작업 표시(CSS·API·버그·테스트 등)가 있으면 코드 작업으로 본다("CSS 애니메이션 추가", "이미지 업로드 API").
STRONG_VISUAL = ("모션 그래픽", "모션그래픽", "영상", "애니메이션", "시안", "목업", "mp4", "video", "motion", "mockup",
                 "렌더링", "rendering")
WEAK_VISUAL = ("렌더", "이미지", "디자인", "render", "image", "design", "그래픽", "graphic", "로고", "logo", "배너", "banner")
MAKE_VERBS = ("제작", "만들", "만들어", "그려", "뽑아", "생성", "렌더해", "create", "make", "produce", "draw")
MOTION_RE = re.compile(r"(?<!프로)모션")  # '프로모션'은 시각 작업이 아니다
CODE_MARKERS = ("css", "html", "api", "엔드포인트", "endpoint", "버그", "bug", "테스트", "test", "함수", "function", "코드",
                "code", "스크립트", "script", "컴포넌트", "component", "업로드", "upload", "리팩터", "refactor", "타입", "빌드")


def is_visual_deliverable(*texts: str) -> bool:
    """영상·모션·이미지·디자인 '결과물'을 만들어 달라는 요청이면 True. 작업자(파일만 고침)가 할 수 없는 일이다."""
    text = " ".join(t for t in texts if t).lower()
    if not text or any(marker in text for marker in CODE_MARKERS):
        return False
    if any(word in text for word in STRONG_VISUAL) or MOTION_RE.search(text):
        return True
    return any(word in text for word in WEAK_VISUAL) and any(verb in text for verb in MAKE_VERBS)


@dataclass(frozen=True)
class HandOff:
    ok: bool
    to: str
    request_id: str | None
    path: str | None  # agent-hub 기준 상대 경로
    todo_id: str | None
    output: str


def clean_line(text: str, limit: int) -> str:
    return " ".join(CONTROL_RE.sub(" ", redact_text(str(text or ""), limit * 2)).split())[:limit]


def request_markdown(request_id: str, project: str, text: str, now: datetime) -> str:
    """templates/request.md 형식(kind=concept)."""
    body = clean_line(text, 1_500)
    return (
        "---\n"
        f"request: {request_id}\n"
        f"project: {project}\n"
        "kind: concept\n"
        "from: telegram\n"
        f"date: {now:%Y-%m-%d}\n"
        "---\n\n"
        "# 요청\n\n"
        "## 목적\n"
        f"{body}\n\n"
        "## 입력\n"
        "- (없음 — 레퍼런스가 있으면 inputs/ 에 넣는다)\n\n"
        "## 요구사항·제약\n"
        f"- 대표 요청 원문(텔레그램): {body}\n"
        "- 운영 사이트에 직접 반영하지 않는다. 비밀값을 넣지 않는다.\n\n"
        "## 원하는 산출물\n"
        "- concept: 서로 다른 방향 4안(outputs/A.jpg~D.jpg) + 판단 카드(card.md)\n"
        "- 채택안은 DeskMini 에서 Blender 로 렌더한다(영상 제작은 DeskMini 몫).\n"
    )


def next_request_id(project_dir: Path, project: str, now: datetime) -> str:
    stamp = f"{project}-{now:%Y%m%d}-"
    used = {p.name for p in project_dir.glob(stamp + "*")} if project_dir.is_dir() else set()
    number = 1
    while f"{stamp}{number:02d}" in used:
        number += 1
    return f"{stamp}{number:02d}"


def create_visual_request(hub_root: Path, todo: HubTodo, text: str, project: str | None, now: datetime) -> HandOff:
    """visual/<프로젝트|general>/<요청ID>/request.md 를 만들고 Codex 할 일로 알린다(hub 가 커밋)."""
    slug = project if project and SLUG_RE.fullmatch(project) else "general"
    if not clean_line(text, 10):
        return HandOff(False, "codex", None, None, None, "요청 내용이 비었습니다")
    if not todo.available():
        return HandOff(False, "codex", None, None, None, "agent-hub 스크립트를 찾지 못했습니다")
    project_dir = hub_root / "visual" / slug
    request_id = next_request_id(project_dir, slug, now)
    folder = project_dir / request_id
    try:
        (folder / "inputs").mkdir(parents=True, exist_ok=False)
        (folder / "outputs").mkdir()
        for sub in ("inputs", "outputs"):
            (folder / sub / ".gitkeep").write_text("", encoding="utf-8")
        (folder / "request.md").write_text(request_markdown(request_id, slug, text, now), encoding="utf-8")
    except OSError as exc:
        return HandOff(False, "codex", request_id, None, None, f"요청 폴더를 만들지 못했습니다: {type(exc).__name__}")
    relative = f"visual/{slug}/{request_id}/request.md"
    result = todo.add("codex", f"[visual {request_id}] 시안 요청(concept): {clean_line(text, 220)} — {relative}")
    match = TODO_ID_OUT_RE.search(result.output or "")
    return HandOff(result.ok, "codex", request_id, relative, match.group(1) if match else None, result.output)


def todo_id_from_output(output: str) -> str | None:
    match = TODO_ID_OUT_RE.search(output or "")
    return match.group(1) if match else None


def card_conclusion(card: Path) -> str:
    """card.md 의 '# 결론' 아래 첫 줄(없으면 verdict). 마스킹·200자."""
    try:
        lines = card.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    verdict = next((line.split(":", 1)[1].strip() for line in lines[:12] if line.startswith("verdict:")), "")
    for index, line in enumerate(lines):
        if line.startswith("# 결론"):
            for following in lines[index + 1:]:
                if following.strip().startswith("#"):
                    break
                if following.strip():
                    conclusion = clean_line(following, 200)
                    return f"{conclusion} ({verdict})" if verdict and verdict not in conclusion else conclusion
    return clean_line(verdict, 200)


def handoff_status(hub_root: Path, item: dict[str, Any]) -> str:
    """넘긴 일의 상태: card(판단 카드 도착) | done(할 일 닫힘) | open | unknown."""
    path = item.get("path")
    if path and SAFE_REL_RE.fullmatch(str(path)) and (hub_root / str(path)).with_name("card.md").is_file():
        return "card"
    todo_id = item.get("todo_id")
    if todo_id and TODO_ID_OUT_RE.fullmatch(f"할 일: {todo_id}"):
        if (hub_root / "todo" / f"{todo_id}.md").is_file():
            return "open"
        if any((hub_root / "todo" / "done").glob(f"*/{todo_id}.md")):
            return "done"
    return "unknown"


SAFE_REL_RE = re.compile(r"^visual/[a-z0-9][a-z0-9_-]{1,40}/[a-z0-9][a-z0-9_-]{1,60}/request\.md$")
STATUS_LABELS = {"card": "결과 도착", "done": "끝남", "open": "진행 전/중", "unknown": "확인 불가"}

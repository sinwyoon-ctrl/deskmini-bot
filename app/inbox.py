"""사진·문서로 지시받기: 받은 파일을 검사해 state/inbox/<날짜>/ 에 저장하고, 작업 발주 때 worktree 로 넘긴다.

규칙
- 크기 20MB 이하(봇 API 내려받기 한도와 같음), 빈 파일 거부
- 확장자 허용 목록: jpg jpeg png webp pdf txt md csv json. zip·실행 파일·스크립트·오피스 문서 등은 거부
- 내용 서명 확인: 이미지·PDF 는 매직 바이트가 확장자와 맞아야 하고, 텍스트는 UTF-8·NUL 없음이어야 한다.
  ELF·PE(MZ)·셔뱅(#!)으로 시작하는 파일은 확장자와 무관하게 거부
- 파일 이름은 글자·숫자·한글·._- 만 남기고 숨김 이름·비밀처럼 보이는 이름은 거부
- LLM 에는 메타데이터만 준다. 텍스트 파일은 마스킹한 앞부분만 대화 모델에 준다. 이미지는 이번 라운드에서 모델에 보내지 않는다.
  TODO(Claude API 경로가 켜지면): 이미지 블록을 대화 모델에 첨부하는 경로 검토.
- worktree 에는 .chief-inbox/ 아래 읽기 전용(0444)으로 넣고 Git 제외 목록(info/exclude)에 넣어 커밋되지 않게 한다.
  텍스트는 마스킹 사본을 넣는다.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.files import secret_like_name
from app.redact import redact_text


MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
TEXT_EXTENSIONS = {"txt", "md", "csv", "json"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | TEXT_EXTENSIONS | {"pdf"}
MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/csv": "csv",
    "application/json": "json",
}
NAME_RE = re.compile(r"[^0-9A-Za-z가-힣._-]+")
INBOX_DIR = ".chief-inbox"
TEXT_PREVIEW_CHARS = 1_500


class AttachmentRejected(ValueError):
    """사용자에게 그대로 보여 줄 한국어 사유."""


@dataclass(frozen=True)
class IncomingFile:
    kind: str  # photo | document
    file_id: str
    name: str
    mime: str | None
    size: int | None
    file_unique_id: str | None = None

    @property
    def file_key(self) -> str:
        """같은 파일 판별 키. Telegram 의 file_unique_id(봇·시간에 무관), 없으면 file_id."""
        return self.file_unique_id or self.file_id


def incoming_from_message(message: dict[str, Any]) -> IncomingFile | None:
    """photo(가장 큰 크기) 또는 document. 둘 다 없으면 None."""
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        valid = [item for item in photos if isinstance(item, dict) and item.get("file_id")]
        if valid:
            largest = max(valid, key=lambda item: (int(item.get("file_size") or 0), int(item.get("width") or 0)))
            stamp = datetime.now().strftime("%H%M%S")
            return IncomingFile(
                kind="photo",
                file_id=str(largest["file_id"]),
                name=f"photo-{stamp}.jpg",
                mime="image/jpeg",
                size=int(largest["file_size"]) if largest.get("file_size") else None,
                file_unique_id=str(largest.get("file_unique_id") or "") or None,
            )
    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        return IncomingFile(
            kind="document",
            file_id=str(document["file_id"]),
            name=str(document.get("file_name") or "document"),
            mime=str(document.get("mime_type") or "") or None,
            size=int(document["file_size"]) if document.get("file_size") else None,
            file_unique_id=str(document.get("file_unique_id") or "") or None,
        )
    return None


def sanitize_name(name: str, mime: str | None = None) -> str:
    """경로·제어문자를 지운 짧은 이름. 확장자가 없으면 MIME 으로 붙인다. 허용 확장자가 아니면 거부."""
    base = Path(str(name).replace("\\", "/")).name
    cleaned = NAME_RE.sub("_", base).strip("._-") or "file"
    cleaned = re.sub(r"_+", "_", cleaned)
    stem, dot, extension = cleaned.rpartition(".")
    if not dot:
        stem, extension = cleaned, MIME_EXTENSIONS.get((mime or "").split(";")[0].strip().lower(), "")
    extension = extension.lower()
    if extension not in ALLOWED_EXTENSIONS:
        shown = extension or "없음"
        raise AttachmentRejected(
            f"이 형식(.{shown})은 받지 않습니다. 사진(jpg·png·webp), pdf, txt·md·csv·json 만 받습니다."
        )
    stem = (stem or "file")[:60].strip("._-") or "file"
    final = f"{stem}.{extension}"
    if secret_like_name(final):
        raise AttachmentRejected("비밀값이 든 파일처럼 보이는 이름이라 받지 않았습니다. 이름을 바꿔 다시 보내 주세요.")
    return final


def photo_name(name: str, content: bytes) -> str:
    """Telegram 사진은 보통 JPEG 지만 내용 서명으로 확장자를 정한다(서명이 모르는 것이면 그대로 둬 검사에서 거부)."""
    stem = name.rsplit(".", 1)[0]
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return stem + ".png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return stem + ".webp"
    return name


def check_content(name: str, content: bytes) -> str:
    """형식 서명 확인. 통과하면 'image' | 'pdf' | 'text'."""
    if not content:
        raise AttachmentRejected("빈 파일입니다.")
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise AttachmentRejected("20MB 를 넘는 파일은 받지 않습니다.")
    head = content[:16]
    if head.startswith((b"\x7fELF", b"MZ", b"#!", b"PK\x03\x04", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe")):
        raise AttachmentRejected("실행 파일·압축 파일로 보이는 내용이라 받지 않았습니다.")
    extension = name.rsplit(".", 1)[-1].lower()
    if extension in {"jpg", "jpeg"}:
        ok, kind = head.startswith(b"\xff\xd8\xff"), "image"
    elif extension == "png":
        ok, kind = head.startswith(b"\x89PNG\r\n\x1a\n"), "image"
    elif extension == "webp":
        ok, kind = head[:4] == b"RIFF" and head[8:12] == b"WEBP", "image"
    elif extension == "pdf":
        ok, kind = content[:1024].lstrip().startswith(b"%PDF-"), "pdf"
    elif extension in TEXT_EXTENSIONS:
        kind = "text"
        try:
            content.decode("utf-8")
            ok = b"\x00" not in content
        except UnicodeDecodeError:
            ok = False
    else:
        ok, kind = False, ""
    if not ok:
        raise AttachmentRejected(f"확장자(.{extension})와 실제 내용이 맞지 않아 받지 않았습니다.")
    return kind


def store_file(inbox_root: Path, name: str, content: bytes, now: datetime) -> tuple[Path, str]:
    """inbox/<YYYY-MM-DD>/<HHMMSS>-<무작위>-<이름> 에 0600 으로 저장. (경로, sha256)."""
    day = inbox_root / now.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = day / f"{now:%H%M%S}-{secrets.token_hex(3)}-{name}"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
    return target, hashlib.sha256(content).hexdigest()


def describe(item: dict[str, Any]) -> str:
    """대화 모델에 줄 한 줄 메타데이터."""
    size = int(item.get("size") or 0)
    shown = f"{size / 1024:.0f}KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f}MB"
    kind = {"photo": "사진"}.get(str(item.get("kind")), str(item.get("stored_name", "")).rsplit(".", 1)[-1])
    return f"[첨부: {item.get('stored_name')}, {kind}, {shown}]"


def text_preview(item: dict[str, Any], limit: int = TEXT_PREVIEW_CHARS) -> str | None:
    """텍스트 첨부의 마스킹된 앞부분. 텍스트가 아니면 None."""
    name = str(item.get("stored_name") or "")
    if name.rsplit(".", 1)[-1].lower() not in TEXT_EXTENSIONS:
        return None
    try:
        raw = Path(item["stored_path"]).read_bytes()[: limit * 4]
    except OSError:
        return None
    return redact_text(raw.decode("utf-8", errors="replace"), limit)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def stage_into_worktree(items: list[dict[str, Any]], worktree: Path, inbox_root: Path) -> list[dict[str, Any]]:
    """첨부를 worktree/.chief-inbox/ 로 복사(텍스트는 마스킹 사본, 0444)하고 Git 제외 목록에 넣는다."""
    target_dir = worktree / INBOX_DIR
    # 검증 명령(저장소 코드)이 worktree 안에 심볼릭 링크를 만들 수 있으므로, 봇이 쓰기 전에 링크·탈출을 거부한다.
    if target_dir.is_symlink() or (target_dir.exists() and not target_dir.is_dir()):
        raise RuntimeError(".chief-inbox is not a plain directory")
    target_dir.mkdir(exist_ok=True, mode=0o700)
    if target_dir.resolve().parent != worktree.resolve():
        raise RuntimeError(".chief-inbox escapes the worktree")
    staged: list[dict[str, Any]] = []
    for item in items:
        source = Path(str(item["stored_path"]))
        if not _within(source, inbox_root) or source.is_symlink():
            continue
        name = str(item["stored_name"])
        if "/" in name or name.startswith("."):
            continue
        data = source.read_bytes()
        if name.rsplit(".", 1)[-1].lower() in TEXT_EXTENSIONS:
            data = redact_text(data.decode("utf-8", errors="replace"), len(data) + 1).encode("utf-8")
        target = None
        for candidate in (target_dir / name, target_dir / f"{item['id']}-{name}"):
            if candidate.is_symlink():
                continue
            if candidate.is_file() and candidate.read_bytes() == data:
                target = candidate  # 재시도: 이미 같은 사본이 있다
                break
            if candidate.exists():
                continue
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                os.fchmod(handle.fileno(), 0o444)
            target = candidate
            break
        if target is None:
            continue
        staged.append(
            {
                "path": f"{INBOX_DIR}/{target.name}",
                "type": name.rsplit(".", 1)[-1].lower(),
                "size": len(data),
                "caption": item.get("caption") or "",
                "redacted": name.rsplit(".", 1)[-1].lower() in TEXT_EXTENSIONS,
            }
        )
    if staged:
        exclude_inbox(worktree)
    return staged


def exclude_inbox(worktree: Path) -> None:
    """공용 Git 디렉터리의 info/exclude 에 '/.chief-inbox/' 를 한 번만 넣는다."""
    result = subprocess.run(
        ("git", "rev-parse", "--git-common-dir"),
        cwd=worktree,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("Could not locate the Git directory for the worktree")
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = (worktree / common).resolve()
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    line = f"/{INBOX_DIR}/"
    current = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if line not in current.splitlines():
        with exclude.open("a", encoding="utf-8") as handle:
            if current and not current.endswith("\n"):
                handle.write("\n")
            handle.write(line + "\n")

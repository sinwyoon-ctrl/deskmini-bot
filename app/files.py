"""결과물 전송 경로 규칙. 허용 루트 안의 일반 파일만, 허용 확장자만, 비밀처럼 보이는 이름은 거부.

순서: 상대 경로·'..' 거부 → resolve(심볼릭 링크 해소) → 허용 루트 포함 여부 → 숨김·비밀 이름 →
일반 파일 → 확장자 → 하드링크(st_nlink>1) → 크기. 전송 직전에 같은 검사를 다시 하고,
읽을 때는 open_checked 로 O_NOFOLLOW 로 연 fd 를 fstat 해서 검사 때와 같은 파일(st_dev/st_ino)인지 다시 본다.

하드링크는 경로만으로는 ~/work 밖 파일(예: 다른 곳의 비밀 파일)과 구별할 수 없어 링크 수가 1 인 파일만 보낸다.
비밀값처럼 생긴 이름(예: 'sk-ant-…', 봇 토큰 모양)은 마스킹 규칙(contains_secret)으로도 걸러 목록에도 올리지 않는다.
"""
from __future__ import annotations

import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.redact import contains_secret, redact_text


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
PHOTO_MAX_BYTES = 10 * 1024 * 1024
TEXT_INLINE_CHARS = 3_500
TEXT_READ_BYTES = 512 * 1024
VIDEO_EXTENSIONS = {"mp4"}
PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
TEXT_EXTENSIONS = {"txt", "md", "json", "csv", "log"}
DOCUMENT_EXTENSIONS = {"webm", "mov", "gif", "pdf"}
ALLOWED_EXTENSIONS = VIDEO_EXTENSIONS | PHOTO_EXTENSIONS | TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS
# 결과물 목록에 보여 줄 확장자(로그는 /logs 로 본다)
DELIVERABLE_EXTENSIONS = ALLOWED_EXTENSIONS - {"log"}
SECRET_SUBSTRINGS = (
    "secret",
    "token",
    "credential",
    "passw",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "private",
    "apikey",
    "api_key",
    "api-key",
    "htpasswd",
    "netrc",
    "keystore",
    "keychain",
    "cookie",
    "authorized_keys",
    "known_hosts",
    ".env",
)
SECRET_TOKENS = {"env", "dotenv", "key", "keys", "pem", "cert", "certs", "ssh", "gnupg", "p12", "pfx"}
TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
# 미디어 결과물은 이름이 넓은 규칙에 걸리기 쉽다(key-visual.png, cookie-banner.png, private-beta-teaser.mp4).
# 비밀값이 실제로 담기는 건 텍스트 파일이므로, 미디어에는 이 약한 규칙을 적용하지 않는다.
MEDIA_EXTENSIONS = {"mp4", "webm", "mov", "jpg", "jpeg", "png", "gif", "webp", "pdf"}
WEAK_SECRET_SUBSTRINGS = {"private", "cookie"}


@dataclass(frozen=True)
class FileCheck:
    ok: bool
    reason: str = ""
    path: Path | None = None
    size: int = 0
    kind: str = ""  # video | photo | document | text
    too_large: bool = False
    dev: int = 0
    ino: int = 0


def secret_like_name(name: str) -> bool:
    if contains_secret(name):
        return True
    lowered = name.lower()
    media = lowered.rsplit(".", 1)[-1] in MEDIA_EXTENSIONS and "." in lowered
    substrings = [item for item in SECRET_SUBSTRINGS if not (media and item in WEAK_SECRET_SUBSTRINGS)]
    if any(item in lowered for item in substrings):
        return True
    tokens = {token for token in TOKEN_SPLIT_RE.split(lowered) if token}
    if media:
        # key·cert 는 단독이면 디자인 용어(key-visual)일 수 있다. private·api·ssh 와 같이 오면 막는다.
        if tokens & (SECRET_TOKENS - {"key", "keys", "cert", "certs"}):
            return True
        return bool(tokens & {"key", "keys", "cert", "certs"} and tokens & {"private", "api", "ssh", "signing"})
    return bool(tokens & SECRET_TOKENS)


def resolve_allowed(raw: str | Path, roots: Sequence[Path]) -> tuple[Path | None, str]:
    """허용 루트 안의 실제 경로를 돌려준다. 실패하면 (None, 한국어 사유)."""
    text = str(raw)
    if not text or "\x00" in text:
        return None, "경로가 비어 있거나 잘못됐습니다"
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        return None, "절대 경로만 허용합니다"
    if ".." in candidate.parts:
        return None, "상위 경로(..)는 허용하지 않습니다"
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "파일이 없거나 읽을 수 없습니다"
    for root in roots:
        try:
            root_resolved = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_relative_to(root_resolved) or resolved == root_resolved:
            continue
        relative = resolved.relative_to(root_resolved).parts
        # 요청 경로(링크 해소 전)와 실제 경로 모두에서 숨김·비밀 이름을 막는다.
        names = list(relative) + list(candidate.parts[-len(relative):])
        if any(part.startswith(".") for part in names):
            return None, "숨김 파일·폴더는 보내지 않습니다"
        if any(secret_like_name(part) for part in names):
            return None, "비밀값처럼 보이는 이름이라 보내지 않습니다"
        return resolved, ""
    return None, "허용된 폴더(~/work, 봇 증거 폴더) 밖입니다"


def file_kind(path: Path, size: int) -> str:
    extension = path.suffix.lower().lstrip(".")
    if extension in TEXT_EXTENSIONS:
        return "text"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in PHOTO_EXTENSIONS and size <= PHOTO_MAX_BYTES:
        return "photo"
    return "document"


def check_deliverable(
    raw: str | Path, roots: Sequence[Path], max_bytes: int = MAX_UPLOAD_BYTES
) -> FileCheck:
    resolved, reason = resolve_allowed(raw, roots)
    if resolved is None:
        return FileCheck(ok=False, reason=reason)
    try:
        info = resolved.lstat()
    except OSError:
        return FileCheck(ok=False, reason="파일 크기를 읽지 못했습니다")
    if not stat.S_ISREG(info.st_mode):
        return FileCheck(ok=False, reason="일반 파일이 아닙니다")
    extension = resolved.suffix.lower().lstrip(".")
    if extension not in ALLOWED_EXTENSIONS:
        return FileCheck(ok=False, reason=f"허용되지 않은 확장자입니다(.{extension or '없음'})")
    if info.st_nlink > 1:
        return FileCheck(ok=False, reason="하드링크 파일은 보내지 않습니다(허용 폴더 밖 파일일 수 있음)")
    size = info.st_size
    kind = file_kind(resolved, size)
    if size > max_bytes:
        return FileCheck(
            ok=False,
            reason="50MB 를 넘어 텔레그램으로 보낼 수 없습니다",
            path=resolved,
            size=size,
            kind=kind,
            too_large=True,
        )
    return FileCheck(ok=True, path=resolved, size=size, kind=kind, dev=info.st_dev, ino=info.st_ino)


def open_checked(check: FileCheck, roots: Sequence[Path], max_bytes: int = MAX_UPLOAD_BYTES):
    """검사를 통과한 파일을 바꿔치기 없이 연다. 실패하면 OSError.

    마지막 경로 요소는 O_NOFOLLOW 로 열고(심볼릭 링크면 ELOOP), 연 fd 를 fstat 해서
    일반 파일·검사 때와 같은 st_dev/st_ino·st_nlink==1·크기를 다시 확인한다. 중간 폴더가 바뀌었을 수 있으니
    fd 가 가리키는 실제 경로(/proc/self/fd)가 여전히 허용 루트 안인지도 본다. 반드시 돌려준 핸들에서만 읽는다.
    """
    if not check.ok or check.path is None:
        raise OSError("not a checked deliverable")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(check.path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        if (info.st_dev, info.st_ino) != (check.dev, check.ino):
            raise OSError("file changed after check")
        if info.st_nlink != 1:
            raise OSError("hardlinked file")
        if info.st_size != check.size or info.st_size > max_bytes:
            raise OSError("file size changed after check")
        proc_link = Path(f"/proc/self/fd/{fd}")
        if proc_link.exists():
            real, _ = resolve_allowed(os.readlink(proc_link), roots)
            if real is None or real != check.path:
                raise OSError("file moved outside allowed roots")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def read_redacted_text(path: Path, limit_bytes: int = TEXT_READ_BYTES, handle=None) -> tuple[str, bool]:
    """텍스트 파일을 마스킹해서 읽는다. (본문, 잘렸는지). handle 을 주면 그 핸들에서만 읽는다."""
    if handle is not None:
        return redact_text_bytes(handle.read(limit_bytes + 4_096), limit_bytes)
    with path.open("rb") as handle:
        return redact_text_bytes(handle.read(limit_bytes + 4_096), limit_bytes)


def redact_text_bytes(data: bytes, limit_bytes: int = TEXT_READ_BYTES) -> tuple[str, bool]:
    """읽은 바이트를 마스킹한 뒤 자른다(자른 뒤 마스킹하면 경계에 걸친 토큰 앞부분이 남는다)."""
    truncated = len(data) > limit_bytes
    text = redact_text(data.decode("utf-8", errors="replace"), len(data) + 1)
    if truncated:
        text = text[:limit_bytes]
    return text, truncated


def list_deliverables(job_dir: Path, dir_patterns: tuple[str, ...], roots: Sequence[Path], limit: int = 12) -> list[FileCheck]:
    """작업 폴더의 결과물 후보(결과물 하위 폴더 + 작업 폴더 바로 아래). 검사를 통과한 것과 크기 초과만."""
    from app.jobs import deliverable_dirs

    candidates: list[Path] = []
    for folder in [*deliverable_dirs(job_dir, dir_patterns), job_dir]:
        try:
            children = sorted(folder.iterdir())
        except OSError:
            continue
        for child in children:
            extension = child.suffix.lower().lstrip(".")
            if extension in DELIVERABLE_EXTENSIONS and child.is_file():
                candidates.append(child)
    order = {"video": 0, "photo": 1, "document": 2, "text": 3}
    checks = []
    for item in candidates:
        result = check_deliverable(item, roots)
        if result.ok or result.too_large:
            if result.ok and result.size == 0:
                continue
            checks.append(result)
    checks.sort(key=lambda check: (order.get(check.kind, 9), str(check.path)))
    return checks[:limit]

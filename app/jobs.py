"""장시간 작업(jobs) 관찰. 읽기 전용: 마커 파일과 로그의 존재·수정 시각만 본다.

작업 = config/jobs.toml 의 glob 패턴에 맞는 디렉터리 중 STARTED 마커가 있는 것.
상태는 마커와 로그 수정 시각으로만 추정한다(프로세스 생존은 보지 않는다).
"""
from __future__ import annotations

import fnmatch
import glob
import re
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.redact import redact_text


DEFAULT_PATTERNS = ("~/work/*/render/*", "~/work/*/revisions/*/render/*")
DEFAULT_DELIVERABLE_DIRS = ("web*", "out*", "final*", "export*", "deliverable*")
MARKERS = ("STARTED", "COMPLETE", "POSTPROCESS_COMPLETE", "FAILED", "RENDER_VALIDATED")
STATUS_LABELS = {
    "running": "진행 중",
    "stalled": "멈춤 의심",
    "complete": "렌더 완료",
    "postprocess_done": "후처리까지 완료",
    "failed": "실패",
}
STATUS_ICONS = {
    "running": "🔄",
    "stalled": "⏸️",
    "complete": "✅",
    "postprocess_done": "✅",
    "failed": "❌",
}
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
PROGRESS_RE = re.compile(r"(?:\d{1,3}%\s*\|)|[█▉▊▋▌▍▎▏░▒▓]{4,}|^\s*[#=>\-]{10,}\s*$")
# 같은 종류가 연달아 나오면 마지막 줄만 남기는 진행 표시(ffmpeg frame=…, 'N/M' 카운터 등)
COUNTER_RE = re.compile(r"^\s*(?:frame=\s*\d+|size=\s*\S+\s+time=|\[?\s*\d+\s*/\s*\d+\s*\]?\s)")


@dataclass(frozen=True)
class JobsConfig:
    patterns: tuple[str, ...] = DEFAULT_PATTERNS
    stalled_minutes: int = 30
    deliverable_dirs: tuple[str, ...] = DEFAULT_DELIVERABLE_DIRS
    max_jobs: int = 200
    max_deliverables: int = 12

    @classmethod
    def load(cls, path: Path) -> "JobsConfig":
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8")).get("jobs") or {}
        except (OSError, tomllib.TOMLDecodeError):
            raw = {}
        default = cls()

        def strings(name: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
            value = raw.get(name)
            if not isinstance(value, list):
                return fallback
            items = tuple(str(item) for item in value if isinstance(item, str) and item.strip())
            return items or fallback

        return cls(
            patterns=strings("patterns", default.patterns),
            stalled_minutes=max(1, int(raw.get("stalled_minutes", default.stalled_minutes))),
            deliverable_dirs=strings("deliverable_dirs", default.deliverable_dirs),
            max_jobs=max(1, int(raw.get("max_jobs", default.max_jobs))),
            max_deliverables=max(1, int(raw.get("max_deliverables", default.max_deliverables))),
        )


@dataclass(frozen=True)
class JobObservation:
    key: str  # 홈 기준 상대 경로. 알림·DB 의 식별자
    path: str
    label: str  # 사람이 읽는 짧은 이름
    status: str  # running | stalled | complete | postprocess_done | failed
    postprocess_missing: bool
    markers: tuple[str, ...]
    last_activity: float
    has_deliverables: bool

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["markers"] = list(self.markers)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobObservation":
        return cls(
            key=str(data["key"]),
            path=str(data.get("path") or ""),
            label=str(data.get("label") or data["key"]),
            status=str(data.get("status") or "running"),
            postprocess_missing=bool(data.get("postprocess_missing")),
            markers=tuple(data.get("markers") or ()),
            last_activity=float(data.get("last_activity") or 0.0),
            has_deliverables=bool(data.get("has_deliverables")),
        )


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def derive_status(
    marker_times: dict[str, float],
    last_log_activity: float | None,
    now: float,
    stalled_seconds: float,
    *,
    postprocess_log_activity: float | None = None,
) -> tuple[str, bool]:
    """마커 수정 시각 → (상태, 후처리 누락 경고). 순수 함수.

    - FAILED 가 COMPLETE 보다 새것이면 failed
    - STARTED 가 끝 마커보다 새것이면 재실행 중으로 본다
    - POSTPROCESS_COMPLETE → postprocess_done, COMPLETE → complete
    - 그 외(STARTED 만): 최근 로그 활동이 stalled_seconds 안이면 running, 아니면 stalled
    - 후처리 누락: COMPLETE 인데 POSTPROCESS_COMPLETE 없고, 후처리 로그가 있으며 stalled_seconds 동안 멈춤
    """
    started = marker_times.get("STARTED")
    complete = marker_times.get("COMPLETE")
    failed = marker_times.get("FAILED")
    post = marker_times.get("POSTPROCESS_COMPLETE")
    ends = [value for value in (complete, failed, post) if value is not None]
    rerun = started is not None and ends and started > max(ends) + 1
    if not rerun:
        if failed is not None and (complete is None or failed >= complete) and post is None:
            return "failed", False
        if post is not None:
            return "postprocess_done", False
        if complete is not None:
            missing = (
                postprocess_log_activity is not None
                and now - postprocess_log_activity > stalled_seconds
            )
            return "complete", missing
    activity = max(value for value in (started, last_log_activity) if value is not None) if (
        started is not None or last_log_activity is not None
    ) else None
    if activity is not None and now - activity <= stalled_seconds:
        return "running", False
    return "stalled", False


def job_label(path: Path, home: Path) -> str:
    """~/work/<프로젝트>/.../<작업> → '<프로젝트> · <리비전> · <작업>'"""
    try:
        parts = path.relative_to(home / "work").parts
    except ValueError:
        return path.name
    if len(parts) >= 5 and parts[1] == "revisions":
        return f"{parts[0]} · {parts[2]} · {parts[-1]}"
    if len(parts) >= 2:
        return f"{parts[0]} · {parts[-1]}"
    return path.name


def job_key(path: Path, home: Path) -> str:
    try:
        return str(path.relative_to(home))
    except ValueError:
        return str(path)


def _log_files(job_dir: Path) -> list[Path]:
    try:
        return sorted(item for item in job_dir.glob("*.log") if item.is_file() and not item.is_symlink())
    except OSError:
        return []


def deliverable_dirs(job_dir: Path, patterns: tuple[str, ...]) -> list[Path]:
    result = []
    try:
        children = sorted(job_dir.iterdir())
    except OSError:
        return []
    for child in children:
        if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
            continue
        if any(fnmatch.fnmatch(child.name, pattern) for pattern in patterns):
            result.append(child)
    return result


def scan_job(job_dir: Path, home: Path, config: JobsConfig, now: float) -> JobObservation | None:
    from app.files import DELIVERABLE_EXTENSIONS  # 순환 import 회피

    marker_times = {}
    for name in MARKERS:
        stamp = _mtime(job_dir / name)
        if stamp is not None:
            marker_times[name] = stamp
    if "STARTED" not in marker_times:
        return None
    logs = _log_files(job_dir)
    log_times = [stamp for stamp in (_mtime(item) for item in logs) if stamp is not None]
    post_times = [
        stamp
        for item in logs
        if item.name.startswith("postprocess")
        for stamp in (_mtime(item),)
        if stamp is not None
    ]
    status, missing = derive_status(
        marker_times,
        max(log_times) if log_times else None,
        now,
        config.stalled_minutes * 60,
        postprocess_log_activity=max(post_times) if post_times else None,
    )
    has_deliverables = False
    for folder in deliverable_dirs(job_dir, config.deliverable_dirs):
        try:
            if any(
                item.suffix.lower().lstrip(".") in DELIVERABLE_EXTENSIONS and item.is_file()
                for item in folder.iterdir()
            ):
                has_deliverables = True
                break
        except OSError:
            continue
    return JobObservation(
        key=job_key(job_dir, home),
        path=str(job_dir),
        label=job_label(job_dir, home),
        status=status,
        postprocess_missing=missing,
        markers=tuple(sorted(marker_times)),
        last_activity=max([*marker_times.values(), *log_times]),
        has_deliverables=has_deliverables,
    )


def discover_jobs(config: JobsConfig, home: Path, now: float | None = None) -> list[JobObservation]:
    """패턴에 맞는 작업을 최근 활동 순으로 돌려준다. 숨김·심볼릭 링크 디렉터리는 건너뛴다."""
    now = time.time() if now is None else now
    seen: set[str] = set()
    jobs: list[JobObservation] = []
    for pattern in config.patterns:
        expanded = pattern.replace("${HOME}", str(home)).replace("$HOME", str(home))
        if expanded.startswith("~"):
            expanded = str(home) + expanded[1:]
        for raw in sorted(glob.glob(expanded)):
            path = Path(raw)
            if any(part.startswith(".") for part in path.parts) or path.is_symlink() or not path.is_dir():
                continue
            if raw in seen:
                continue
            seen.add(raw)
            job = scan_job(path, home, config, now)
            if job is not None:
                jobs.append(job)
            if len(seen) >= config.max_jobs * 3:
                break
    jobs.sort(key=lambda item: item.last_activity, reverse=True)
    return jobs[: config.max_jobs]


def match_job(
    jobs: list[JobObservation], hint: str, *, prefer_deliverables: bool = False
) -> JobObservation | None:
    """'full-1080', 'message cards' 같은 힌트로 작업 하나를 고른다. 없으면 None.

    prefer_deliverables: 결과물을 받으려는 요청이면 같은 힌트에 맞는 작업 중 결과물이 있는 것을 먼저 고른다
    (결과물 없는 진행 중 작업이 최근 활동이라는 이유로 먼저 잡히지 않게). 그 안에서는 최근 활동 순.
    """
    hint = (hint or "").strip().lower()
    if not jobs:
        return None

    def pick(candidates: list[JobObservation]) -> JobObservation | None:
        if prefer_deliverables:
            for job in candidates:
                if job.has_deliverables:
                    return job
        return candidates[0] if candidates else None

    if not hint:
        return pick(jobs)
    exact = [job for job in jobs if Path(job.key).name.lower() == hint]
    if exact:
        return pick(exact)
    tokens = [token for token in re.split(r"[\s/·,]+", hint) if token]
    if not tokens:
        return None
    return pick([job for job in jobs if all(token in job.key.lower() for token in tokens)])  # 최근 활동 순


def clean_log_lines(raw: str, limit: int = 30, width: int = 200) -> list[str]:
    """ANSI·진행 막대·\\r 덮어쓰기 잡음을 걷어내고 마지막 limit 줄을 마스킹해서 돌려준다."""
    lines: list[str] = []
    for line in raw.splitlines():
        segments = [segment for segment in line.split("\r") if segment.strip()]
        if not segments:
            continue
        text = ANSI_RE.sub("", segments[-1]).replace("\t", "  ").rstrip()
        text = "".join(ch for ch in text if ch.isprintable())
        # 자르기 전에 줄 전체를 마스킹한다(먼저 자르면 줄 끝 토큰 앞부분이 최소 길이에 못 미쳐 새어 나간다).
        text = redact_text(text, len(text) + 1)
        if not text.strip() or PROGRESS_RE.search(text):
            continue
        if lines and lines[-1] == text:
            continue
        if lines and COUNTER_RE.match(text) and COUNTER_RE.match(lines[-1]):
            lines[-1] = text[:width]
            continue
        lines.append(text[:width])
    return [redact_text(line, width) for line in lines[-limit:]]


def tail_text(path: Path, max_bytes: int = 65_536) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read(max_bytes)
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return text

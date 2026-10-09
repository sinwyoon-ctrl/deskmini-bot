"""감시기: 관측값(Observation) + 지난 상태 → 알림 이벤트. 읽기 전용.

detect() 는 순수 함수다. 시뮬레이터는 가짜 Observation 을 만들어 ChiefBot.watch_tick(now, observation=...)
으로 넣으면 된다. 처음 관측은 기준선으로만 저장하고 알리지 않는다(항목별로도 동일: Docker 가 처음 확인되면 기준선).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.alerts import AlertConfig, AlertEvent
from app.collector import bytes_human
from app.jobs import STATUS_LABELS, JobObservation


TASK_ALERT_STATES = {
    "READY": ("info", "봇 작업 검증 통과"),
    "FAILED": ("warn", "봇 작업 실패"),
    "BLOCKED": ("warn", "봇 작업 차단됨"),
    "INTERRUPTED": ("warn", "봇 작업 중단됨(확인 필요)"),
}
TASK_DEDUPE_SECONDS = 7 * 86400


@dataclass
class Observation:
    at: datetime
    containers: dict[str, str] | None = None  # 이름 → docker State(running/exited/…); None=미확인
    disk_free: int | None = None
    disk_total: int | None = None
    mem_available: int | None = None
    mem_total: int | None = None
    tmux: str = "unknown"  # active | inactive | unknown
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)  # id → {"state", "classification"}
    jobs: dict[str, JobObservation] = field(default_factory=dict)


def disk_low(obs: Observation, config: AlertConfig) -> bool | None:
    if obs.disk_free is None or not obs.disk_total:
        return None
    return (
        obs.disk_free / obs.disk_total * 100 < config.disk_free_percent
        or obs.disk_free < config.disk_free_gb * 1024**3
    )


def memory_low(obs: Observation, config: AlertConfig) -> bool | None:
    if obs.mem_available is None or not obs.mem_total:
        return None
    return obs.mem_available / obs.mem_total * 100 < config.memory_available_percent


def _container_up(state: str | None) -> bool:
    return state == "running"


def _job_events(job: JobObservation, previous: dict[str, Any] | None) -> list[AlertEvent]:
    ref = {"job_key": job.key}
    events: list[AlertEvent] = []
    before = (previous or {}).get("status")
    if before != job.status:
        severity, title = {
            "running": ("info", "작업 시작" if previous is None else "작업 다시 진행"),
            "stalled": ("warn", "작업 멈춤 의심"),
            "complete": ("info", "렌더 완료"),
            "postprocess_done": ("info", "후처리까지 완료"),
            "failed": ("warn", "작업 실패"),
        }.get(job.status, ("info", f"작업 상태: {job.status}"))
        body = job.label
        if job.status == "stalled":
            body += " · 로그가 한동안 갱신되지 않음"
        elif before:
            body += f" · {STATUS_LABELS.get(before, before)} → {STATUS_LABELS.get(job.status, job.status)}"
        events.append(
            AlertEvent(key=f"job:{job.key}:{job.status}", severity=severity, kind="job", title=title, body=body, ref=ref)
        )
    if job.postprocess_missing and not (previous or {}).get("postprocess_missing"):
        events.append(
            AlertEvent(
                key=f"job:{job.key}:postprocess_missing",
                severity="warn",
                kind="job",
                title="렌더는 끝났지만 후처리 미완",
                body=f"{job.label} · POSTPROCESS_COMPLETE 없음, 후처리 로그 멈춤",
                ref=ref,
            )
        )
    return events


def detect(
    obs: Observation,
    previous: dict[str, Any] | None,
    config: AlertConfig,
    *,
    busy_tasks: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[AlertEvent], dict[str, Any]]:
    """(이벤트, 저장할 새 상태). previous 가 None 이면 기준선만 만든다.

    busy_tasks: 봇이 결과를 처리 중인 작업(검증·반영 future). 이번에는 비교하지 않고 지난 값을 유지한다.
    """
    baseline = previous is None
    prev = previous or {}
    events: list[AlertEvent] = []
    state: dict[str, Any] = {"baseline_at": prev.get("baseline_at") or obs.at.isoformat()}

    # Docker 컨테이너 (조회 실패 시 지난 값 유지, 알림 없음)
    prev_containers = prev.get("containers")
    if obs.containers is None:
        state["containers"] = prev_containers
    else:
        current = {name: obs.containers.get(name, "missing") for name in config.containers}
        state["containers"] = current
        if not baseline and isinstance(prev_containers, dict):
            for name, now_state in current.items():
                before = prev_containers.get(name)
                if before is None:
                    continue
                if _container_up(before) and not _container_up(now_state):
                    events.append(
                        AlertEvent(
                            key=f"docker:{name}:down",
                            severity="critical",
                            kind="docker",
                            title=f"컨테이너 멈춤: {name}",
                            body=f"상태 {before} → {now_state}. 봇은 컨테이너를 조작하지 않습니다.",
                            ref={"container": name},
                        )
                    )
                elif not _container_up(before) and _container_up(now_state):
                    events.append(
                        AlertEvent(
                            key=f"docker:{name}:up",
                            severity="info",
                            kind="docker",
                            title=f"컨테이너 다시 실행 중: {name}",
                            body=f"상태 {before} → {now_state}",
                            ref={"container": name},
                        )
                    )

    # 디스크
    low = disk_low(obs, config)
    if low is None:
        state["disk_low"] = prev.get("disk_low")
    else:
        state["disk_low"] = low
        before = prev.get("disk_low")
        if not baseline and before is not None and low != before:
            free = f"여유 {bytes_human(obs.disk_free)} / {bytes_human(obs.disk_total)}"
            events.append(
                AlertEvent(
                    key="disk:low" if low else "disk:ok",
                    severity="critical" if low else "info",
                    kind="disk",
                    title="디스크 여유 부족" if low else "디스크 여유 회복",
                    body=free,
                )
            )

    # 메모리 (연속 N회 부족일 때만)
    mem_low = memory_low(obs, config)
    count = int(prev.get("mem_low_count") or 0)
    alerted = bool(prev.get("mem_alerted"))
    if mem_low is not None:
        count = count + 1 if mem_low else 0
        if mem_low and count >= config.memory_consecutive and not alerted:
            alerted = True
            if not baseline:
                events.append(
                    AlertEvent(
                        key="memory:low",
                        severity="critical",
                        kind="memory",
                        title="메모리 여유 부족",
                        body=f"사용 가능 {bytes_human(obs.mem_available)} / {bytes_human(obs.mem_total)} · {count}회 연속",
                    )
                )
        elif not mem_low and alerted:
            alerted = False
            if not baseline:
                events.append(
                    AlertEvent(
                        key="memory:ok",
                        severity="info",
                        kind="memory",
                        title="메모리 여유 회복",
                        body=f"사용 가능 {bytes_human(obs.mem_available)} / {bytes_human(obs.mem_total)}",
                    )
                )
    state["mem_low_count"] = count
    state["mem_alerted"] = alerted

    # Remote Control tmux 세션
    before_tmux = prev.get("tmux")
    if obs.tmux in {"active", "inactive"}:
        state["tmux"] = obs.tmux
        if not baseline and before_tmux in {"active", "inactive"} and before_tmux != obs.tmux:
            gone = obs.tmux == "inactive"
            events.append(
                AlertEvent(
                    key="tmux:gone" if gone else "tmux:back",
                    severity="warn" if gone else "info",
                    kind="tmux",
                    title="Remote Control 세션 사라짐" if gone else "Remote Control 세션 다시 있음",
                    body=f"tmux 세션 '{config.tmux_session}'",
                )
            )
    else:
        state["tmux"] = before_tmux

    # 봇 작업 (완료·실패·차단·중단만, 봇이 이미 보낸 것은 dedupe 로 걸러진다)
    prev_tasks = prev.get("tasks") or {}
    tasks_state: dict[str, str] = {}
    for task_id, info in obs.tasks.items():
        current = str(info.get("state"))
        before = prev_tasks.get(task_id)
        if task_id in busy_tasks:
            if before is not None:
                tasks_state[task_id] = before
            continue
        tasks_state[task_id] = current
        if baseline or before == current or current not in TASK_ALERT_STATES:
            continue
        severity, title = TASK_ALERT_STATES[current]
        if current == "BLOCKED" and info.get("classification") == "LIMIT_BLOCKED":
            title = "봇 작업 사용 한도로 차단"
        events.append(
            AlertEvent(
                key=f"task:{task_id}:{current}",
                severity=severity,
                kind="task",
                title=title,
                body=f"{task_id} · {before or '새 작업'} → {current}",
                ref={"task_id": task_id},
                dedupe_seconds=TASK_DEDUPE_SECONDS,
            )
        )
    state["tasks"] = tasks_state

    # 장시간 작업
    prev_jobs = prev.get("jobs") or {}
    jobs_state: dict[str, Any] = {}
    for key, job in obs.jobs.items():
        jobs_state[key] = {"status": job.status, "postprocess_missing": job.postprocess_missing}
        if baseline:
            continue
        events.extend(_job_events(job, prev_jobs.get(key)))
    state["jobs"] = jobs_state

    state["host"] = {
        "at": obs.at.isoformat(),
        "disk_free": obs.disk_free,
        "disk_total": obs.disk_total,
        "mem_available": obs.mem_available,
        "mem_total": obs.mem_total,
        "containers": state.get("containers"),
        "tmux": state.get("tmux"),
    }
    return events, state

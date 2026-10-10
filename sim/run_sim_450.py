"""450건 검증: 4라운드 기능 + 빈틈 시나리오(v3).

  .venv/bin/python sim/run_sim_450.py events            # 결정적 v3 이벤트(LLM 없음) → out/results_v3_events.jsonl
  .venv/bin/python sim/run_sim_450.py chat --mode claude # v3 대화(claude CLI) → out/results_v3_chat.jsonl
  .venv/bin/python sim/run_sim_450.py chat --mode nollm  # v3 대화 라우팅만(스텁)

시나리오 목록은 실행 때 sim/events_v3.jsonl · sim/cases_v3.jsonl 로도 내보낸다(정의는 이 파일이 원본).
TMPDIR 는 숨김 폴더가 아닌 곳이어야 한다(작업 스캐너가 경로에 '.' 로 시작하는 부분이 있으면 건너뛴다).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

SIM = Path(__file__).resolve().parent
sys.path.insert(0, str(SIM))

import harness300 as H  # noqa: E402
import harness450 as K  # noqa: E402
import run_sim as base  # noqa: E402

from adapters.telegram import TelegramAPI, TelegramError  # noqa: E402
from app.inbox import stage_into_worktree  # noqa: E402
from app.retry import parse_reset_time  # noqa: E402
from app.todo import HubTodo  # noqa: E402

OUT = SIM / "out"
KST = H.KST
REAL_HUB = Path.home() / "agent-hub" / "bin" / "hub"

FAILED_EVIDENCE = {"verdict": "FAIL", "reason": "One or more registered checks failed",
                   "checks": [{"argv": ["python3", "-m", "unittest"], "passed": False, "output": "AssertionError: 1 != 2"}]}
RUN_OK = {"run_state": "COMPLETED", "result": {"classification": "SUCCESS"}}


def limit_run(msg: str, ended: str | None = None) -> dict[str, Any]:
    r = {"run_state": "BLOCKED", "result": {"classification": "LIMIT_BLOCKED", "error": msg}}
    if ended:
        r["ended_at"] = ended
    return r


def kst(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 10, day, hh, mm, tzinfo=KST).astimezone(UTC)


# ================================================================ 컨텍스트


class Ctx:
    def __init__(self, env: H.Env300, *, clock: datetime | None = None, client: Any = None, max_active: int = 1):
        self.env = env
        self.client = client or K.CapturingClient()
        self.bot, self.rec, self.info = env.new_bot(self.client, preset="empty", clock=clock)
        if max_active != 1:
            from dataclasses import replace
            self.bot.settings = replace(self.bot.settings, max_active_tasks=max_active)
        self.tg = K.FakeTG450(self.rec)
        self.log = K.ExecLog()
        self.parts = K.wire(self.bot, self.rec, self.tg, self.log, self.info, self.client)
        env.register_snapshot(self.bot, "healthy")
        self.upd = 1000
        self.fails: list[str] = []
        self.flags: list[str] = []
        self.notes: list[str] = []
        self.restarts = 0
        self.canaries: list[Path] = []

    # ---- 입력
    def send(self, text: str | None, **kw: Any) -> int:
        self.upd += 1
        n = len(self.rec.messages)
        self.bot._handle_update(K.msg_update(self.upd, text, **kw))
        return n

    def raw(self, update: dict[str, Any]) -> int:
        n = len(self.rec.messages)
        self.bot._handle_update(update)
        return n

    def press(self, data: str | None, **kw: Any) -> int:
        n = len(self.rec.messages)
        if data is None:
            self.fail("expected button missing")
            return n
        self.upd += 1
        self.bot._handle_update(K.cb_update(self.upd, data, **kw))
        return n

    def doc(self, name: str | None, data: bytes, *, mime: str | None = None, caption: str | None = None,
            declared: int | None = -1, reported: int | None = None, **kw: Any) -> int:
        self.upd += 1
        fid = f"f{self.upd}"
        self.tg.files[fid] = {"id": fid, "data": data, **({"reported": reported} if reported is not None else {})}
        d: dict[str, Any] = {"file_id": fid}
        if name is not None:
            d["file_name"] = name
        if mime:
            d["mime_type"] = mime
        if declared == -1:
            d["file_size"] = len(data)
        elif declared is not None:
            d["file_size"] = declared
        extra: dict[str, Any] = {"document": d}
        if caption is not None:
            extra["caption"] = caption
        extra.update(kw.pop("extra", {}))
        n = len(self.rec.messages)
        self.bot._handle_update(K.msg_update(self.upd, None, extra=extra, **kw))
        return n

    def photo(self, data: bytes, *, caption: str | None = None, extra: dict[str, Any] | None = None, **kw: Any) -> int:
        self.upd += 1
        small, big = f"s{self.upd}", f"p{self.upd}"
        self.tg.files[small] = {"id": small, "data": K.jpeg(b"small")}
        self.tg.files[big] = {"id": big, "data": data}
        e: dict[str, Any] = {"photo": [{"file_id": small, "file_size": 50, "width": 90},
                                       {"file_id": big, "file_size": len(data), "width": 1280}]}
        if caption is not None:
            e["caption"] = caption
        e.update(extra or {})
        n = len(self.rec.messages)
        self.bot._handle_update(K.msg_update(self.upd, None, extra=e, **kw))
        return n

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            K.tick(self.bot)

    def restart(self) -> None:
        self.bot, self.parts = K.restart(self.bot, self.rec, self.tg, self.log, self.info, self.client, self.env)
        self.restarts += 1
        self.bot._recover_state()

    def set_clock(self, dt: datetime) -> None:
        self.info["clock"]["now"] = dt

    # ---- 판정
    def check(self, cond: Any, msg: str) -> bool:
        if not cond:
            self.fails.append(msg)
        return bool(cond)

    def fail(self, msg: str) -> None:
        self.fails.append(msg)

    def flag(self, msg: str) -> None:
        self.flags.append(msg)

    def text(self, since: int = 0) -> str:
        return K.texts(self.rec, since)

    def btn(self, prefix: str) -> str | None:
        return K.last_button(self.rec, prefix)

    def task_state(self, tid: str) -> str:
        return self.bot.store.get_task(tid)["state"]

    def tasks_created(self) -> list[dict[str, Any]]:
        with K.db(self.bot) as con:
            return [dict(r) for r in con.execute("SELECT id, project_id, state, request_text FROM tasks WHERE owner_user_id=? AND request_text!='sim 작업' ORDER BY created_at, rowid", (H.CHAT_ID,))]

    def canary(self, name: str) -> Path:
        p = self.info["case_dir"] / name
        self.canaries.append(p)
        return p


# ================================================================ 전역 안전 점검


def global_safety(ctx: Ctx, net_before: int, viol_before: int) -> list[str]:
    probs = H.safety_scan(ctx.rec)
    for m in ctx.rec.messages:
        for frag in K.all_leaks(m.get("text") or ""):
            if frag in K.EXTRA_FRAGMENTS:
                probs.append(f"VIOLATION secret fragment {frag} in outgoing text")
    for i, inp in enumerate(ctx.client.inputs):
        leaks = K.all_leaks(inp)
        if leaks:
            probs.append(f"VIOLATION secret in LLM input #{i}: {sorted(set(leaks))}")
        if K.IMG_SENTINEL.decode() in inp:
            probs.append("VIOLATION image bytes in LLM input")
    if len(H.NETWORK_ATTEMPTS) > net_before:
        probs.append(f"VIOLATION network attempts: {H.NETWORK_ATTEMPTS[net_before:]}")
    if len(K.EXEC_VIOLATIONS) > viol_before:
        probs.append(f"VIOLATION repo command: {K.EXEC_VIOLATIONS[viol_before:]}")
    for c in ctx.canaries:
        if c.exists():
            probs.append(f"VIOLATION injected command executed: {c.name} exists")
    # 무승인 실행·이중 실행: 실행 수 ≤ 소비된 승인 수(+시작된 예약)
    with K.db(ctx.bot) as con:
        appr = [dict(r) for r in con.execute("SELECT id, task_id, action, consumed_at FROM approvals WHERE consumed_at IS NOT NULL")]
        sched = [dict(r) for r in con.execute("SELECT task_id, state FROM scheduled_retries WHERE state IN ('STARTED','FAILED')")]
        retry_verify = [dict(r) for r in con.execute("SELECT task_id FROM events WHERE event_type='RETRY_VERIFY'")]
    tasks = {i["task_id"] for i in ctx.log.items} | {r["task_id"] for r in retry_verify}
    for tid in tasks:
        starts = sum(1 for i in ctx.log.items if i["task_id"] == tid and i["kind"] == "worker_start" and not i.get("retry"))
        retries = sum(1 for i in ctx.log.items if i["task_id"] == tid and i["kind"] == "worker_start" and i.get("retry"))
        retries += sum(1 for r in retry_verify if r["task_id"] == tid)
        rel = sum(1 for i in ctx.log.items if i["task_id"] == tid and i["kind"] == "release")
        ok_start = sum(1 for a in appr if a["task_id"] == tid and a["action"] == "start")
        ok_retry = sum(1 for a in appr if a["task_id"] == tid and a["action"] == "retry") + sum(1 for s in sched if s["task_id"] == tid)
        ok_rel = sum(1 for a in appr if a["task_id"] == tid and a["action"] in {"merge", "push", "deploy"})
        if starts > ok_start:
            probs.append(f"VIOLATION worker start without approval or doubled: {tid} starts={starts} approvals={ok_start}")
        if retries > ok_retry:
            probs.append(f"VIOLATION retry without approval or doubled: {tid} retries={retries} approvals+scheduled={ok_retry}")
        if rel > ok_rel:
            probs.append(f"VIOLATION release without approval or doubled: {tid}")
        attempts = [i.get("attempt") for i in ctx.log.items if i["task_id"] == tid and i["kind"] == "worker_start"]
        if len(attempts) != len(set(attempts)):
            probs.append(f"VIOLATION duplicate attempt numbers {attempts}")
    probs += H.html_problems(ctx.rec)
    return probs


# ================================================================ 시나리오 등록

SCENARIOS: list[dict[str, Any]] = []


def sc(sid: str, category: str, desc: str, **opts: Any) -> Callable:
    def deco(fn: Callable) -> Callable:
        SCENARIOS.append({"id": sid, "category": category, "desc": desc, "fn": fn, **opts})
        return fn
    return deco


def failed_task(ctx: Ctx, **kw: Any) -> str:
    kw.setdefault("runs", [{"run_state": "COMPLETED", "result": {"classification": "SUCCESS"}}])
    kw.setdefault("evidence", FAILED_EVIDENCE)
    return K.make_task(ctx.bot, state=kw.pop("state", "FAILED"), **kw)


def wexec(ctx: Ctx, tid: str, retry: bool | None = None) -> int:
    return sum(1 for i in ctx.log.items if i["task_id"] == tid and i["kind"] == "worker_start" and (retry is None or i.get("retry") == retry))


# ---------------------------------------------------------------- A. 재시도


@sc("R01", "재시도", "FAILED(검증 실패) /retry → 이유+버튼 → 누르면 attempt 2 로 1회 실행")
def r01(c: Ctx) -> None:
    tid = failed_task(c)
    n = c.send(f"/retry {tid}")
    t = c.text(n)
    c.check("등록된 검증 명령 실패" in t and "AssertionError" in t, "failure card lacks reason")
    data = c.btn("approve:retry:")
    c.check(data, "no retry button")
    c.press(data)
    c.check(wexec(c, tid, True) == 1, f"retry exec count {wexec(c, tid, True)} != 1")
    att = [i.get("attempt") for i in c.log.items if i["kind"] == "worker_start"]
    c.check(att == [2], f"attempt numbers {att} != [2]")
    c.check(c.task_state(tid) == "RUNNING", f"state {c.task_state(tid)}")
    c.check(any(i.get("previous_attempt") == 1 for i in c.log.items), "previous attempt not passed to launch")


@sc("R02", "재시도", "같은 [다시 시도] 버튼 두 번(다른 update) → 1회만, 두 번째 응답 확인")
def r02(c: Ctx) -> None:
    tid = failed_task(c)
    c.send(f"/retry {tid}")
    data = c.btn("approve:retry:")
    c.press(data)
    n = c.press(data)
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)} != 1")
    second = c.text(n)
    c.notes.append(f"second tap reply: {second[:120]}")
    if "ConflictError" in second or "요청 처리 실패" in second:
        c.flag("double tap answered with generic '요청 처리 실패: ConflictError' instead of '이미 처리된 버튼'")


@sc("R03", "재시도", "같은 callback update 가 두 번 배달(같은 update_id) → 1회, 추가 메시지 없음")
def r03(c: Ctx) -> None:
    tid = failed_task(c)
    c.send(f"/retry {tid}")
    data = c.btn("approve:retry:")
    upd = K.cb_update(7777, data)
    c.raw(upd)
    n = c.raw(upd)
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)} != 1")
    c.check(len(c.rec.messages) == n, "duplicate delivery produced messages")


@sc("R04", "재시도", "재시도 한도 소진(실행 2회, retries=1) → 버튼 없음, 한도 안내")
def r04(c: Ctx) -> None:
    tid = failed_task(c, runs=[RUN_OK, RUN_OK])
    n = c.send(f"/retry {tid}")
    c.check("재시도 한도" in c.text(n), "no budget message")
    c.check(c.btn("approve:retry:") is None, "retry button offered after budget exhausted")


@sc("R05", "재시도", "카드 두 장(같은 버전) → 첫 장 실행 후 둘째 장은 거절(버전 바뀜)")
def r05(c: Ctx) -> None:
    tid = failed_task(c)
    c.send(f"/retry {tid}")
    first = c.btn("approve:retry:")
    c.send(f"/retry {tid}")
    second = c.btn("approve:retry:")
    c.check(first != second, "same approval reused")
    c.press(first)
    n = c.press(second)
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)} != 1")
    c.notes.append(f"stale card reply: {c.text(n)[:120]}")


@sc("R06", "재시도", "/retry 진행 중(RUNNING) 작업 → 거절, 버튼 없음")
def r06(c: Ctx) -> None:
    tid = failed_task(c, state="RUNNING")
    n = c.send(f"/retry {tid}")
    c.check("다시 시도할 수 없습니다" in c.text(n), "no refusal text")
    c.check(c.btn("approve:retry:") is None, "button offered")


@sc("R07", "재시도", "/retry 없는 작업 ID → 찾지 못함")
def r07(c: Ctx) -> None:
    n = c.send("/retry T-20990101-abcdef")
    c.check("찾지 못했습니다" in c.text(n), "no not-found text")


@sc("R08", "재시도", "/retry 인자 없음 → 사용법")
def r08(c: Ctx) -> None:
    n = c.send("/retry")
    c.check("다시 시도할 작업 ID" in c.text(n), "no usage")


@sc("R09", "재시도", "LIMIT_BLOCKED 'resets 3pm (Asia/Seoul)' 10:00 → 예약 버튼(15:05), 누른 뒤 14:59 미실행·15:05 1회")
def r09(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("You've hit your limit · resets 3pm (Asia/Seoul)")], evidence=None)
    n = c.send(f"/retry {tid}")
    t = c.text(n)
    c.check("10/09 15:00" in t, "reset time 15:00 not shown")
    c.check(c.btn("approve:retry:") is None, "immediate retry offered before reset")
    data = c.btn("approve:schedule_retry:")
    labels = [lbl for lbl, d in K.buttons_of(c.rec) if d == data]
    c.check(labels and "15:05" in labels[0], f"schedule label {labels}")
    c.set_clock(kst(9, 11))
    c.tick(2)
    c.check(wexec(c, tid) == 0, "ran before tap")
    c.press(data)
    c.check(len(K.scheduled(c.bot, tid)) == 1, "no scheduled row")
    c.set_clock(kst(9, 14, 59))
    c.tick(2)
    c.check(wexec(c, tid) == 0, "fired before 15:05")
    c.set_clock(kst(9, 15, 5))
    c.tick(3)
    c.check(wexec(c, tid) == 1, f"exec at 15:05 = {wexec(c, tid)}")
    c.set_clock(kst(9, 16, 0))
    c.tick(2)
    c.check(wexec(c, tid) == 1, "fired twice")


R09_CLOCK = kst(9, 10)


@sc("R10", "재시도", "LIMIT_BLOCKED 예약 버튼을 누르지 않으면 풀린 뒤에도 실행 없음")
def r10(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("usage limit · resets 3pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    for hh in (15, 16, 23):
        c.set_clock(kst(9, hh, 30))
        c.tick(2)
    c.check(wexec(c, tid) == 0, "executed without tap")
    c.check(not K.scheduled(c.bot, tid), "schedule row without tap")


@sc("R11", "재시도", "예약 후 재시작 → 시각에 1회만, 다시 재시작해도 추가 실행 없음")
def r11(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("resets 3pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    c.press(c.btn("approve:schedule_retry:"))
    c.restart()
    c.set_clock(kst(9, 15, 0))
    c.tick(2)
    c.check(wexec(c, tid) == 0, "fired before time after restart")
    c.set_clock(kst(9, 15, 6))
    c.tick(2)
    c.restart()
    c.tick(2)
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)} != 1")


@sc("R12", "재시도", "예약 후 작업이 바뀜(사용자 중단 → CANCELLED) → 예약은 SKIPPED, 실행 0")
def r12(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("resets 3pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    c.press(c.btn("approve:schedule_retry:"))
    c.bot.store.transition(tid, expected_state="BLOCKED", new_state="CANCELLED")
    c.set_clock(kst(9, 15, 10))
    c.tick(3)
    c.check(wexec(c, tid) == 0, f"exec {wexec(c, tid)} != 0")
    states = [s["state"] for s in K.scheduled(c.bot, tid)]
    c.check(states == ["SKIPPED"], f"schedule states {states}")


@sc("R24", "재시도", "예약 후 풀린 직후(15:01) /retry → 즉시 [다시 시도]여야 함(같은 시각 재해석 문제)")
def r24(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("resets 3pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    c.set_clock(kst(9, 15, 1))
    n = c.send(f"/retry {tid}")
    c.check(c.btn("approve:retry:") is not None, f"after reset passed, /retry offers another schedule instead: {[l for l, d in K.buttons_of(c.rec) if d.startswith('approve:schedule_retry:')][-1:]}")


@sc("R13", "재시도", "LIMIT_BLOCKED 인데 풀리는 시각 없음 → 즉시 [다시 시도]")
def r13(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("rate limit exceeded")], evidence=None)
    n = c.send(f"/retry {tid}")
    c.check("확인하지 못했습니다" in c.text(n), "no unknown-reset note")
    c.check(c.btn("approve:retry:"), "no immediate retry")


@sc("R14", "재시도", "'resets in 2 hours' → now+2h+5m 예약")
def r14(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("weekly limit · resets in 2 hours")], evidence=None)
    c.send(f"/retry {tid}")
    data = c.btn("approve:schedule_retry:")
    labels = [lbl for lbl, d in K.buttons_of(c.rec) if d == data]
    c.check(labels and "12:05" in labels[0], f"label {labels}")


@sc("R15", "재시도", "'resets Oct 10, 10:30pm (Asia/Seoul)' → 10/10 22:35")
def r15(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("usage limit · resets Oct 10, 10:30pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    data = c.btn("approve:schedule_retry:")
    labels = [lbl for lbl, d in K.buttons_of(c.rec) if d == data]
    c.check(labels and "10/10 22:35" in labels[0], f"label {labels}")


@sc("R16", "재시도", "한도 차단(08:50, 'resets 9am') 후 10:00 에 /retry → 이미 풀렸으니 즉시 [다시 시도]여야 함")
def r16(c: Ctx) -> None:
    ended = kst(9, 8, 50).isoformat(timespec="seconds")
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("You've hit your limit · resets 9am (Asia/Seoul)", ended)], evidence=None)
    n = c.send(f"/retry {tid}")
    sched = c.btn("approve:schedule_retry:")
    labels = [lbl for lbl, d in K.buttons_of(c.rec) if d == sched]
    c.check(c.btn("approve:retry:") is not None, f"no immediate retry after reset passed; offered {labels} ({c.text(n)[:160]!r})")


@sc("R17", "재시도", "예약 버튼 두 번 → 예약 1건, 실행 1회")
def r17(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("resets 3pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    data = c.btn("approve:schedule_retry:")
    c.press(data)
    c.press(data)
    c.check(len(K.scheduled(c.bot, tid)) == 1, f"rows {len(K.scheduled(c.bot, tid))}")
    c.set_clock(kst(9, 15, 6))
    c.tick(3)
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)}")


@sc("R18", "재시도", "[다시 시도] 승인 만료 후 누름 → 실행 없음, 만료 안내")
def r18(c: Ctx) -> None:
    tid = failed_task(c)
    c.send(f"/retry {tid}")
    data = c.btn("approve:retry:")
    K.expire_approval(c.bot, data.split(":", 2)[2])
    n = c.press(data)
    c.check(wexec(c, tid) == 0, "expired approval executed")
    t = c.text(n)
    c.check("만료" in t or "유효시간" in t or "다시 띄" in t, f"expiry not explained clearly: {t[:120]!r}")


@sc("R19", "재시도", "검토 불가(UNVERIFIED, BLOCKED, 작업자 성공) → [검증 다시 시도] → 검증만 1회")
def r19(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", evidence={"verdict": "UNVERIFIED", "review_verdict": "UNVERIFIED", "reason": "Independent review did not pass"})
    c.send(f"/retry {tid}")
    data = c.btn("approve:retry:")
    labels = [lbl for lbl, d in K.buttons_of(c.rec) if d == data]
    c.check(labels and "검증" in labels[0], f"label {labels}")
    c.press(data)
    c.tick(2)
    c.check(wexec(c, tid) == 0, "worker re-ran in verify mode")
    c.check(c.log.count("verify", tid) == 1, f"verify count {c.log.count('verify', tid)}")


@sc("R20", "재시도", "검증 다시 시도는 예산과 무관하게 무제한? (연속 3회)")
def r20(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", evidence={"verdict": "UNVERIFIED", "review_verdict": "UNVERIFIED"})
    c.parts["verifier"].outcome[tid] = "BLOCKED"
    for _ in range(3):
        c.send(f"/retry {tid}")
        c.press(c.btn("approve:retry:"))
        c.tick(1)
    v = c.log.count("verify", tid)
    c.notes.append(f"verify-only retries allowed: {v}")
    if v >= 3:
        c.flag("verify-only retry has no budget (each costs one reviewer LLM call)")


@sc("R21", "재시도", "/retry 로 예약 제안 후 풀린 시각 지나 예약 버튼 누름 → 다음 루프 즉시 1회")
def r21(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("resets 3pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    data = c.btn("approve:schedule_retry:")
    c.set_clock(kst(9, 15, 30))
    c.press(data)
    c.tick(2)
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)}")


@sc("R22", "재시도", "작업자 단계 실패가 reconcile 에서 감지되면 카드가 자동으로 뜸(이유+버튼)")
def r22(c: Ctx) -> None:
    c.send("/new sandbox: 테스트 추가")
    tid = c.tasks_created()[-1]["id"]
    c.press(c.btn("approve:start:"))
    c.parts["runner"].outcomes[tid] = [{"state": "FAILED", "run_state": "FAILED",
                                        "result": {"classification": "EXECUTION_FAILED", "error": "boom: tool crashed"}}]
    n = len(c.rec.messages)
    c.tick(1)
    t = c.text(n)
    c.check("EXECUTION_FAILED" in t and "boom" in t, "auto failure card lacks reason")
    c.check(c.btn("approve:retry:"), "no auto retry button")
    c.tick(3)
    cards = sum(1 for m in c.rec.messages[n:] if "approve:retry:" in json.dumps(m.get("reply_markup") or {}))
    c.check(cards == 1, f"retry card sent {cards} times")


@sc("R23", "재시도", "자동 실패 카드 → 재시도 → 다시 실패 → 한도 소진 카드(버튼 없음)")
def r23(c: Ctx) -> None:
    c.send("/new sandbox: 테스트 추가")
    tid = c.tasks_created()[-1]["id"]
    c.press(c.btn("approve:start:"))
    fail = {"state": "FAILED", "run_state": "FAILED", "result": {"classification": "EXECUTION_FAILED", "error": "x"}}
    c.parts["runner"].outcomes[tid] = [dict(fail)]
    c.tick(1)
    c.press(c.btn("approve:retry:"))
    c.parts["runner"].outcomes[tid] = [dict(fail)]
    n = len(c.rec.messages)
    c.tick(1)
    c.check("재시도 한도" in c.text(n), "no budget-exhausted note on 2nd failure")
    c.check(wexec(c, tid) == 2, f"exec {wexec(c, tid)}")


# ---------------------------------------------------------------- 첨부


def att_rows(c: Ctx) -> list[dict[str, Any]]:
    with K.db(c.bot) as con:
        return [dict(r) for r in con.execute("SELECT * FROM attachments ORDER BY id")]


def inbox_ok(c: Ctx) -> None:
    root = (c.bot.settings.state_dir / "inbox").resolve()
    for r in att_rows(c):
        p = Path(r["stored_path"]).resolve()
        c.check(str(p).startswith(str(root) + "/"), f"stored outside inbox: {p}")
        c.check(stat.S_IMODE(p.stat().st_mode) == 0o600, f"mode {oct(p.stat().st_mode)}")


@sc("A01", "첨부", "사진 + '/new sandbox: …' 캡션 → 발주 카드(sandbox, 첨부 표시), LLM 호출 없음")
def a01(c: Ctx) -> None:
    n = c.photo(K.jpeg(), caption="/new sandbox: 이 화면처럼 README 고쳐")
    t = c.text(n)
    c.check("<b>첨부</b>" in t and "sandbox" in t, "card lacks attachment/project")
    tasks = c.tasks_created()
    c.check(tasks and tasks[-1]["project_id"] == "sandbox", f"tasks {tasks}")
    c.check(len(c.bot.store.attachments_for_task(tasks[-1]["id"])) == 1 if tasks else False, "attachment not linked")
    c.check(c.client.calls == 0, "LLM called for fixed command caption")
    c.check(not c.log.items, "executed without approval")
    inbox_ok(c)


@sc("A02", "첨부", "사진 + 자연어 캡션 → 대화 모델엔 메타데이터만(이미지 바이트 없음)")
def a02(c: Ctx) -> None:
    c.photo(K.jpeg(b"secret-pixels"), caption="이 화면 보고 README 고칠 거 있으면 알려줘")
    c.check(c.client.calls == 1, f"llm calls {c.client.calls}")
    c.check(c.client.inputs and "[첨부:" in c.client.inputs[-1], "no attachment metadata in LLM input")


@sc("A03", "첨부", "캡션 없는 사진 → 저장 + 무엇을 할지 질문, 작업·LLM 없음")
def a03(c: Ctx) -> None:
    n = c.photo(K.jpeg())
    c.check("무엇을 할까요" in c.text(n), "no question")
    c.check(len(att_rows(c)) == 1 and not c.tasks_created(), "unexpected rows/tasks")
    c.check(c.client.calls == 0, "LLM called")


@sc("A04", "첨부", "캡션 없는 문서 → 다음 '/new sandbox: …' 에 첨부 연결")
def a04(c: Ctx) -> None:
    c.doc("table.csv", "a,b\n1,2\n".encode(), mime="text/csv")
    n = c.send("/new sandbox: 첨부한 표를 README 에 정리")
    tid = c.tasks_created()[-1]["id"]
    c.check(len(c.bot.store.attachments_for_task(tid)) == 1, "pending attachment not linked")
    c.check("table.csv" in c.text(n), "card does not list attachment")


@sc("A05", "첨부", "캡션 없는 문서 후 31분 뒤 지시 → 연결하지 않음")
def a05(c: Ctx) -> None:
    c.doc("table.csv", b"a,b\n", mime="text/csv")
    with K.db(c.bot) as con:
        con.execute("UPDATE attachments SET created_at=?", ((datetime.now(UTC) - timedelta(minutes=31)).isoformat(timespec="seconds"),))
    c.send("/new sandbox: 표 정리")
    tid = c.tasks_created()[-1]["id"]
    c.check(not c.bot.store.attachments_for_task(tid), "stale attachment linked")


@sc("A06", "첨부", "선언 크기 25MB → 내려받기 전에 거절")
def a06(c: Ctx) -> None:
    n = c.doc("big.pdf", b"%PDF-1.4 small", declared=25 * H.MB)
    c.check("20MB" in c.text(n), "no size refusal")
    c.check(c.tg.get_file_calls == 0 and c.tg.download_calls == 0, "downloaded anyway")
    c.check(not att_rows(c), "stored")


@sc("A07", "첨부", "선언 크기 없음, getFile 이 25MB 보고 → 내려받기 전 거절")
def a07(c: Ctx) -> None:
    n = c.doc("big.pdf", b"%PDF-1.4 small", declared=None, reported=25 * H.MB)
    c.check("20MB" in c.text(n) and c.tg.download_calls == 0, "not refused before download")


@sc("A08", "첨부", "크기 정보 없음, 실제 21MB → 내려받기 중단(TelegramError) 또는 거절, 저장 없음")
def a08(c: Ctx) -> None:
    n = c.doc("big.txt", b"a" * (21 * H.MB), declared=None, reported=0)
    c.check(not att_rows(c), "stored oversize")
    c.notes.append(c.text(n)[:100])


for _sid, _name, _mime, _data in [
    ("A09", "setup.exe", "application/octet-stream", b"MZ\x90\x00binary"),
    ("A10", "run.sh", "application/x-sh", b"#!/bin/sh\nrm -rf ~\n"),
    ("A11", "bundle.zip", "application/zip", b"PK\x03\x04zip"),
    ("A12", "page.html", "text/html", b"<html><script>alert(1)</script></html>"),
    ("A13", "report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"PK\x03\x04docx"),
    ("A14", "logo.svg", "image/svg+xml", b"<svg onload=alert(1)>"),
]:
    def _mk(name: str = _name, mime: str = _mime, data: bytes = _data) -> Callable:
        def fn(c: Ctx) -> None:
            n = c.doc(name, data, mime=mime, caption="/new sandbox: 이거 반영해")
            c.check("받지 않" in c.text(n), "not refused")
            c.check(not att_rows(c) and not c.tasks_created(), "stored or task created")
            c.check(c.tg.download_calls == 0, "downloaded a disallowed type")
        return fn
    sc(_sid, "첨부", f"허용 안 되는 형식 {_name} → 거절, 저장·발주 없음")(_mk())

for _sid, _name, _data, _desc in [
    ("A15", "photo.png", b"MZ\x90\x00 pe", "png 이름에 실행 파일(MZ)"),
    ("A16", "notes.txt", b"\x7fELF\x02\x01", "txt 이름에 ELF"),
    ("A17", "doc.pdf", b"<html>not pdf</html>", "pdf 이름에 HTML"),
    ("A18", "img.jpg", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "jpg 이름에 PNG 내용"),
    ("A19", "data.json", b"#!/bin/bash\necho pwn\n", "json 이름에 셔뱅 스크립트"),
    ("A20", "bin.txt", b"abc\x00def", "txt 에 NUL"),
    ("A21", "latin.txt", b"\xff\xfe\xfa bad utf8", "txt 가 UTF-8 아님"),
    ("A22", "empty.txt", b"", "빈 파일"),
]:
    def _mk2(name: str = _name, data: bytes = _data) -> Callable:
        def fn(c: Ctx) -> None:
            n = c.doc(name, data, caption="/new sandbox: 반영")
            c.check("받지 않" in c.text(n) or "빈 파일" in c.text(n), f"not refused: {c.text(n)[:80]!r}")
            c.check(not att_rows(c) and not c.tasks_created(), "stored or task created")
        return fn
    sc(_sid, "첨부", f"내용 서명 위조: {_desc} → 거절")(_mk2())


@sc("A23", "첨부", "파일 이름 ../../../.ssh/authorized_keys.txt → 이름만 남기고 inbox 안에 저장")
def a23(c: Ctx) -> None:
    n = c.doc("../../../.ssh/authorized_keys.txt", b"ssh-ed25519 AAAA test\n")
    rows = att_rows(c)
    # 이름만 남겨 inbox 에 저장하거나(경로 제거) 비밀처럼 보이는 이름으로 거절하면 통과
    c.check((len(rows) == 1 and "/" not in rows[0]["stored_name"] and ".." not in rows[0]["stored_name"]) or (not rows and "받지 않" in c.text(n)), f"rows {rows[:1]}")
    c.doc("../../../tmp/evil-notes.txt", b"plain\n")
    rows = att_rows(c)
    c.check(rows and rows[-1]["stored_name"] == "evil-notes.txt", f"traversal name {[r['stored_name'] for r in rows]}")
    inbox_ok(c)
    c.check(not (c.info["home"] / ".ssh" / "authorized_keys.txt").exists(), "escaped to ~/.ssh")


@sc("A24", "첨부", "RTL 덮어쓰기 이름 'invoice\\u202etxt.exe' → .exe 로 판정, 거절")
def a24(c: Ctx) -> None:
    n = c.doc("invoice‮txt.exe", b"MZ\x90", mime="text/plain")
    c.check("받지 않" in c.text(n) and not att_rows(c), "RTL trick accepted")


@sc("A25", "첨부", "아주 긴 이름(300자) → 60자 안쪽으로 줄여 저장")
def a25(c: Ctx) -> None:
    c.doc("가" * 300 + ".txt", b"hello\n")
    rows = att_rows(c)
    c.check(rows and len(rows[0]["stored_name"]) <= 64, f"name len {len(rows[0]['stored_name']) if rows else None}")


for _sid, _name, _mime in [("A26", ".env", "text/plain"), ("A27", "id_rsa", "text/plain"), ("A28", "api_key.txt", None),
                           ("A29", "credentials.json", "application/json"), ("A30", "secrets.md", None)]:
    def _mk3(name: str = _name, mime: str | None = _mime) -> Callable:
        def fn(c: Ctx) -> None:
            n = c.doc(name, b"x=1\n", mime=mime)
            rows = att_rows(c)
            if rows:
                c.fail(f"secret-like name stored as {rows[0]['stored_name']}")
            c.notes.append(c.text(n)[:100])
        return fn
    sc(_sid, "첨부", f"비밀값처럼 보이는 이름 {_name!r} → 거절")(_mk3())


def mini_worktree(c: Ctx) -> tuple[Path, Path]:
    base_dir = c.info["case_dir"] / "gitfix"
    repo = base_dir / "repo"
    repo.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "sim", "GIT_AUTHOR_EMAIL": "sim@example.invalid",
           "GIT_COMMITTER_NAME": "sim", "GIT_COMMITTER_EMAIL": "sim@example.invalid"}
    run = lambda *a, cwd=repo: subprocess.run(a, cwd=cwd, env=env, check=True, capture_output=True, text=True)  # noqa: E731
    run("git", "init", "-q", "-b", "main")
    (repo / "README.md").write_text("x\n")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")
    wt = base_dir / "wt"
    run("git", "worktree", "add", "-q", "-b", "t", str(wt))
    return repo, wt


@sc("A31", "첨부", "비밀값이 든 txt + '/new' → inbox 원본 보관, worktree 사본은 마스킹·0444·커밋 제외, 출력 누출 없음")
def a31(c: Ctx) -> None:
    c.doc("config-notes.txt", K.EXTRA_SECRET_TEXT.encode(), caption="/new sandbox: 설정 정리")
    tid = c.tasks_created()[-1]["id"]
    items = c.bot.store.attachments_for_task(tid)
    _, wt = mini_worktree(c)
    staged = stage_into_worktree(items, wt, c.bot.settings.state_dir / "inbox")
    c.check(len(staged) == 1, f"staged {staged}")
    f = wt / staged[0]["path"]
    body = f.read_text()
    c.check(not K.all_leaks(body), f"secret in worktree copy: {K.all_leaks(body)}")
    c.check(stat.S_IMODE(f.stat().st_mode) == 0o444, f"mode {oct(f.stat().st_mode)}")
    st = subprocess.run(["git", "status", "--porcelain", "--ignored=no"], cwd=wt, capture_output=True, text=True).stdout
    c.check(".chief-inbox" not in st, f"inbox visible to git: {st!r}")
    subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True)
    staged_git = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=wt, capture_output=True, text=True).stdout
    c.check(".chief-inbox" not in staged_git, "inbox staged by git add -A")


@sc("A32", "첨부", "worktree 의 .chief-inbox 가 바깥을 가리키는 심볼릭 링크 → 쓰기 거부, 바깥 무변화")
def a32(c: Ctx) -> None:
    c.doc("a.txt", b"hello\n", caption="/new sandbox: x")
    tid = c.tasks_created()[-1]["id"]
    _, wt = mini_worktree(c)
    outside = c.info["outside"] / "target"
    outside.mkdir()
    (wt / ".chief-inbox").symlink_to(outside)
    try:
        stage_into_worktree(c.bot.store.attachments_for_task(tid), wt, c.bot.settings.state_dir / "inbox")
        c.fail("no error on symlinked .chief-inbox")
    except RuntimeError:
        pass
    c.check(not any(outside.iterdir()), "wrote outside via symlink")


@sc("A33", "첨부", ".chief-inbox 안 같은 이름이 바깥 파일 링크 → 그 링크로 쓰지 않음")
def a33(c: Ctx) -> None:
    c.doc("a.txt", b"hello\n", caption="/new sandbox: x")
    tid = c.tasks_created()[-1]["id"]
    _, wt = mini_worktree(c)
    victim = c.info["outside"] / "victim.txt"
    victim.write_text("original")
    (wt / ".chief-inbox").mkdir()
    (wt / ".chief-inbox" / "a.txt").symlink_to(victim)
    stage_into_worktree(c.bot.store.attachments_for_task(tid), wt, c.bot.settings.state_dir / "inbox")
    c.check(victim.read_text() == "original", "victim overwritten through symlink")


@sc("A34", "첨부", "DB 의 stored_path 가 inbox 밖(변조) → 복사하지 않음")
def a34(c: Ctx) -> None:
    c.doc("a.txt", b"hello\n", caption="/new sandbox: x")
    tid = c.tasks_created()[-1]["id"]
    with K.db(c.bot) as con:
        con.execute("UPDATE attachments SET stored_path=?", (str(c.info["home"] / ".env"),))
    _, wt = mini_worktree(c)
    staged = stage_into_worktree(c.bot.store.attachments_for_task(tid), wt, c.bot.settings.state_dir / "inbox")
    c.check(not staged, f"copied outside file: {staged}")


@sc("A35", "첨부", "Telegram file_path 경로 탈출(../, 절대경로, %2e) → 네트워크 전 거절")
def a35(c: Ctx) -> None:
    api = TelegramAPI("sim-offline")
    before = len(H.NETWORK_ATTEMPTS)
    for p in ("../../etc/passwd", "documents/../../x", "/etc/passwd", "", "a" * 300, "documents/%2e%2e/x\n"):
        try:
            api.download_file(p, 100)
            c.fail(f"accepted {p!r}")
        except TelegramError:
            pass
    c.check(len(H.NETWORK_ATTEMPTS) == before, "network attempted for bad path")


@sc("A36", "첨부", "앨범(media_group) 사진 2장 캡션 없음 → 짧은 확인 2번, 질문 남발 없음")
def a36(c: Ctx) -> None:
    n = c.photo(K.jpeg(), extra={"media_group_id": "g1"})
    c.photo(K.jpeg(b"2"), extra={"media_group_id": "g1"})
    t = c.text(n)
    c.check(t.count("받았습니다") == 2 and "무엇을 할까요" not in t, f"album replies {t[:200]!r}")


@sc("A37", "첨부", "이름 없는 문서 + application/pdf + %PDF → document.pdf 로 저장")
def a37(c: Ctx) -> None:
    c.doc(None, b"%PDF-1.7\n...", mime="application/pdf")
    rows = att_rows(c)
    c.check(rows and rows[0]["stored_name"].endswith(".pdf"), f"rows {[r['stored_name'] for r in rows]}")


@sc("A38", "첨부", "캡션에 토큰 → DB 캡션·출력 모두 마스킹")
def a38(c: Ctx) -> None:
    c.doc("a.txt", b"hi\n", caption=f"/new sandbox: 토큰 {H.FAKE_TG} 넣어서 설정")
    rows = att_rows(c)
    c.check(rows and not K.all_leaks(rows[0]["caption"] or ""), "raw token in DB caption")
    tasks = c.tasks_created()
    c.check(tasks and not K.all_leaks(tasks[-1]["request_text"]), "raw token in task request")


@sc("A39", "첨부", "Telegram 사진인데 내용이 PNG → .png 로 저장(서명 기준)")
def a39(c: Ctx) -> None:
    c.photo(K.png())
    rows = att_rows(c)
    c.check(rows and rows[0]["stored_name"].endswith(".png"), f"rows {[r['stored_name'] for r in rows]}")


@sc("A40", "첨부", "비밀값 txt + 자연어 캡션 → 대화 모델 입력은 마스킹 미리보기(토큰·PEM 없음)")
def a40(c: Ctx) -> None:
    c.doc("notes.txt", K.EXTRA_SECRET_TEXT.encode(), caption="이 메모 요약해줘")
    c.check(c.client.calls == 1 and "[첨부 내용(마스킹)]" in c.client.inputs[-1], "no masked preview")


@sc("A41", "첨부", "마스킹 규칙 밖 형식(AWS·Slack 토큰) txt → 대화 모델 입력에 원문이 가는지(탐침)")
def a41(c: Ctx) -> None:
    body = f"aws={K.PROBE_SECRETS['aws']}\nslack: {K.PROBE_SECRETS['slack']}\n"
    c.doc("probe.txt", body.encode(), caption="이거 요약해줘")
    leaked = [k for k, v in K.PROBE_SECRETS.items() if any(v in i for i in c.client.inputs)]
    if leaked:
        c.flag(f"redaction gap: {leaked} token formats reach chat LLM input unmasked")


@sc("A42", "첨부", "다운로드 실패(TelegramError) → 안내, 저장 없음")
def a42(c: Ctx) -> None:
    c.upd += 1
    fid = "missing"
    extra = {"document": {"file_id": fid, "file_name": "a.txt", "file_size": 3}}
    c.tg.files[fid] = {"id": fid, "data": b"abc", "path": "documents/nomatch.bin", "fail": True}
    n = c.raw(K.msg_update(c.upd, None, extra=extra))
    c.check("내려받지 못했습니다" in c.text(n) and not att_rows(c), f"{c.text(n)[:80]!r}")


# ---------------------------------------------------------------- 할 일(todo)


def fake_hub(c: Ctx) -> Path:
    hub = c.info["case_dir"] / "hubfix"
    (hub / "bin").mkdir(parents=True)
    shutil.copy(REAL_HUB, hub / "bin" / "hub")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=hub, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "sim"], cwd=hub, check=True)
    subprocess.run(["git", "config", "user.email", "sim@example.invalid"], cwd=hub, check=True)
    (hub / "BOARD.md").write_text("# board\n")
    subprocess.run(["git", "add", "-A"], cwd=hub, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=hub, check=True, env=env)
    c.bot._hub_todo = HubTodo(hub)  # type: ignore[attr-defined]
    return hub


def todo_files(hub: Path) -> list[Path]:
    return sorted((hub / "todo").glob("*.md")) if (hub / "todo").is_dir() else []


@sc("T01", "할 일", "/todo codex … → 실제 hub 사본으로 파일 생성·커밋, 확인 답장")
def t01(c: Ctx) -> None:
    hub = fake_hub(c)
    n = c.send("/todo codex 로그인 화면 문구 정리")
    files = todo_files(hub)
    c.check(len(files) == 1 and "로그인 화면 문구 정리" in files[0].read_text() and "assignee: codex" in files[0].read_text(), "todo file wrong")
    log = subprocess.run(["git", "log", "--oneline"], cwd=hub, capture_output=True, text=True).stdout
    c.check("todo: codex" in log, "not committed")
    c.check("남겼습니다" in c.text(n), "no confirmation")


@sc("T02", "할 일", "/todo → 열린 목록")
def t02(c: Ctx) -> None:
    fake_hub(c)
    c.send("/todo claude 테스트 보강")
    n = c.send("/todo")
    c.check("테스트 보강" in c.text(n) and "[claude]" in c.text(n), f"list {c.text(n)[:150]!r}")


@sc("T03", "할 일", "/todo any … → any 로 기록")
def t03(c: Ctx) -> None:
    hub = fake_hub(c)
    c.send("/todo any README 정리")
    files = todo_files(hub)
    c.check(files and "assignee: any" in files[0].read_text(), "not any")


@sc("T04", "할 일", "/todo codex (내용 없음) → 사용법, hub 호출 없음")
def t04(c: Ctx) -> None:
    n = c.send("/todo codex")
    c.check("예:" in c.text(n) and not c.parts["hub"].calls, "no usage or hub called")


@sc("T05", "할 일", "/todo bob 할 일 → 사용법(대상 아님)")
def t05(c: Ctx) -> None:
    n = c.send("/todo bob 할 일")
    c.check(not c.parts["hub"].calls and "codex" in c.text(n), "bob accepted")


for _sid, _payload, _desc in [
    ("T06", "$(touch {p})", "$(…) 명령 치환"),
    ("T07", "`touch {p}`", "백틱"),
    ("T08", "x; touch {p} #", "세미콜론"),
    ("T09", "' && touch {p} && echo '", "따옴표 탈출"),
    ("T10", "$(touch {p})\nassignee: claude", "줄바꿈으로 머리글 위조"),
    ("T11", "${{IFS}}$(touch${{IFS}}{p})", "IFS 변수"),
]:
    def _mk4(payload: str = _payload) -> Callable:
        def fn(c: Ctx) -> None:
            hub = fake_hub(c)
            canary = c.canary("PWNED")
            text = payload.format(p=canary)
            c.send(f"/todo codex {text}")
            files = todo_files(hub)
            if c.check(len(files) == 1, f"files {files}"):
                body = files[0].read_text()
                head = body.split("\n\n", 1)[0].splitlines()
                c.check(head[:1] == ["assignee: codex"] and not any(l.startswith("assignee: claude") for l in body.splitlines()), f"header forged: {body!r}")
                first = text.split("\n")[0]
                c.check(first.strip() in body, f"text not stored literally: {body!r}")
        return fn
    sc(_sid, "할 일", f"할 일 문구 인젝션({_desc}) → 글자 그대로 저장, 실행 안 됨")(_mk4())


@sc("T12", "할 일", "아주 긴 할 일(3000자) → 500자 안쪽 저장")
def t12(c: Ctx) -> None:
    hub = fake_hub(c)
    c.send("/todo codex " + "가" * 3000)
    files = todo_files(hub)
    c.check(files and len(files[0].read_text().split("\n\n", 1)[1]) <= 520, "too long")


@sc("T13", "할 일", "할 일 문구가 '-n' 하나 → 내용이 비지 않고 저장되는지")
def t13(c: Ctx) -> None:
    hub = fake_hub(c)
    c.send("/todo codex -n")
    files = todo_files(hub)
    if files:
        body = files[0].read_text().split("\n\n", 1)[1] if "\n\n" in files[0].read_text() else ""
        if "-n" not in body:
            c.flag(f"hub stores '-n' as empty body (bash echo option): {body!r}")


@sc("T14", "할 일", "/todo done 1 → 닫힘, /todo done ../../x → 사용법(hub 호출 없음)")
def t14(c: Ctx) -> None:
    hub = fake_hub(c)
    c.send("/todo codex 할 일 하나")
    n = c.send("/todo done 1")
    c.check("닫았습니다" in c.text(n) and not todo_files(hub), f"not closed {c.text(n)[:80]!r}")
    n = c.send("/todo done ../../x")
    c.check("예:" in c.text(n), "bad id accepted")


@sc("T15", "할 일", "대화 dispatch_todo(codex) → 바로 기록, 답에 '승인/카드' 주장 없음")
def t15(c: Ctx) -> None:
    c.client.action = {"type": "dispatch_todo", "assignee": "codex", "todo_text": "로그인 문구 정리"}
    c.client.reply = "승인 카드를 띄웠어요"
    n = c.send("코덱스한테 로그인 문구 정리 남겨 둬")
    calls = c.parts["hub"].calls
    c.check(calls and calls[-1][3:6] == ["add", "codex", "로그인 문구 정리"], f"argv {calls}")
    c.check("승인 카드" not in c.text(n), "false approval claim passed through")


@sc("T16", "할 일", "대화 dispatch_todo(assignee=root/빈 문구) → 버림")
def t16(c: Ctx) -> None:
    c.client.action = {"type": "dispatch_todo", "assignee": "root", "todo_text": "rm -rf ~"}
    c.send("루트한테 남겨")
    c.client.action = {"type": "dispatch_todo", "assignee": "codex", "todo_text": "   "}
    c.send("코덱스한테 남겨")
    c.check(not c.parts["hub"].calls, f"hub called {c.parts['hub'].calls}")


@sc("T17", "할 일", "할 일 문구에 토큰 → hub 에 마스킹된 문구만")
def t17(c: Ctx) -> None:
    hub = fake_hub(c)
    c.send(f"/todo codex 토큰 {H.FAKE_TG} 갱신")
    files = todo_files(hub)
    c.check(files and not K.all_leaks(files[0].read_text()), "token committed to hub")


@sc("T18", "할 일", "hub 스크립트 없음 → 실패 안내")
def t18(c: Ctx) -> None:
    c.bot._hub_todo = HubTodo(c.info["case_dir"] / "nohub")  # type: ignore[attr-defined]
    n = c.send("/todo codex x")
    c.check("남기지 못했습니다" in c.text(n), f"{c.text(n)[:80]!r}")


# ---------------------------------------------------------------- 프로젝트 선택


def card_project(c: Ctx, since: int) -> str:
    t = c.text(since)
    import re
    m = re.search(r"프로젝트: <code>([^<]+)</code>", t) or re.search(r"프로젝트: ([\w-]+)", t)
    return m.group(1) if m else ""


@sc("P01", "프로젝트", "/new sandbox: … → sandbox 카드")
def p01(c: Ctx) -> None:
    c.send("/new sandbox: 테스트 추가")
    c.check(c.tasks_created()[-1]["project_id"] == "sandbox", "not sandbox")


@sc("P02", "프로젝트", "/new 내용(프로젝트 없음) → deskmini 기본, 카드에 표시")
def p02(c: Ctx) -> None:
    n = c.send("/new README 오타 고쳐")
    c.check(c.tasks_created()[-1]["project_id"] == "deskmini" and "deskmini" in c.text(n), "default not shown")


@sc("P03", "프로젝트", "/new unknownproj: … → 모르는 프로젝트를 기본으로 몰래 돌리지 않아야 함")
def p03(c: Ctx) -> None:
    c.send("/new unknownproj: 기능 추가")
    tasks = c.tasks_created()
    if tasks:
        c.fail(f"unknown project silently routed to {tasks[-1]['project_id']} (request={tasks[-1]['request_text']!r})")


@sc("P04", "프로젝트", "/new Sandbox: …(대문자) → sandbox 로 인식되는지")
def p04(c: Ctx) -> None:
    c.send("/new Sandbox: 테스트 추가")
    p = c.tasks_created()[-1]["project_id"]
    if p != "sandbox":
        c.flag(f"'Sandbox:' (capitalised) routed to {p}")


@sc("P05", "프로젝트", "/new sandbox 테스트 추가(콜론 없음) → sandbox")
def p05(c: Ctx) -> None:
    c.send("/new sandbox 테스트 추가")
    c.check(c.tasks_created()[-1]["project_id"] == "sandbox", "not sandbox")


@sc("P06", "프로젝트", "개발: sandbox: … → sandbox")
def p06(c: Ctx) -> None:
    c.send("개발: sandbox: 테스트 추가")
    c.check(c.tasks_created()[-1]["project_id"] == "sandbox", "not sandbox")


@sc("P07", "프로젝트", "대화 create_task project_id=null → deskmini 카드, 실행 없음")
def p07(c: Ctx) -> None:
    c.client.action = {"type": "create_task", "project_id": None, "objective": "README 정리", "output_format": "변경",
                       "boundaries": [], "acceptance": []}
    n = c.send("README 좀 정리해줘")
    c.check(c.tasks_created() and c.tasks_created()[-1]["project_id"] == "deskmini" and "deskmini" in c.text(n), "default card missing")
    c.check(not c.log.items, "executed")


@sc("P08", "프로젝트", "대화 create_task project_id='evil' → 발주 안 함")
def p08(c: Ctx) -> None:
    c.client.action = {"type": "create_task", "project_id": "evil", "objective": "x", "output_format": "y", "boundaries": [], "acceptance": []}
    c.send("evil 저장소에 기능 추가")
    c.check(not c.tasks_created(), "unregistered project task created")


@sc("P09", "프로젝트", "/new sandbox: (빈 내용) → 사용법")
def p09(c: Ctx) -> None:
    n = c.send("/new sandbox:")
    c.check(not c.tasks_created(), "empty task created")
    c.notes.append(c.text(n)[:80])


# ---------------------------------------------------------------- 재시작 복구


@sc("X01", "재시작", "RUNNING 중 재시작 → 재실행 없음")
def x01(c: Ctx) -> None:
    c.send("/new sandbox: x")
    tid = c.tasks_created()[-1]["id"]
    c.press(c.btn("approve:start:"))
    c.restart()
    c.tick(3)
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)}")


@sc("X02", "재시작", "VERIFYING 중 재시작 → 검증 1회")
def x02(c: Ctx) -> None:
    tid = failed_task(c, state="VERIFYING", evidence=None)
    c.restart()
    c.tick(3)
    c.restart()
    c.tick(2)
    c.check(c.log.count("verify", tid) == 1, f"verify {c.log.count('verify', tid)}")


@sc("X03", "재시작", "착수 승인 소비 직후 크래시 → 재시작 때 1회 착수, 다시 재시작해도 1회")
def x03(c: Ctx) -> None:
    c.send("/new sandbox: x")
    tid = c.tasks_created()[-1]["id"]
    aid = c.btn("approve:start:").split(":", 2)[2]
    c.bot.store.consume_approval(approval_id=aid, action="start", owner_user_id=H.CHAT_ID, chat_id=H.CHAT_ID)
    c.restart()
    c.restart()
    c.tick(2)
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)}")


@sc("X04", "재시작", "재시도 승인 소비 직후 크래시 → 재시작 때 1회, 두 번째 재시작 0")
def x04(c: Ctx) -> None:
    tid = failed_task(c)
    c.send(f"/retry {tid}")
    aid = c.btn("approve:retry:").split(":", 2)[2]
    c.bot.store.consume_approval(approval_id=aid, action="retry", owner_user_id=H.CHAT_ID, chat_id=H.CHAT_ID)
    c.restart()
    c.restart()
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)}")


@sc("X05", "재시작", "[다시 시도] 눌렀으나 '다른 작업 진행 중'으로 실패 안내 → 나중 재시작 때 몰래 실행되면 안 됨")
def x05(c: Ctx) -> None:
    c.send("/new sandbox: 먼저 하는 일")
    t1 = c.tasks_created()[-1]["id"]
    c.press(c.btn("approve:start:"))
    t2 = failed_task(c)
    c.send(f"/retry {t2}")
    n = c.press(c.btn("approve:retry:"))
    c.check("다시 시도하지 못했습니다" in c.text(n), "no failure notice")
    c.parts["runner"].outcomes[t1] = [{"state": "FAILED", "run_state": "FAILED", "result": {"classification": "EXECUTION_FAILED", "error": "x"}}]
    c.tick(1)
    c.restart()
    c.check(wexec(c, t2) == 0, f"retry silently executed at restart after user was told it failed (exec={wexec(c, t2)})")


@sc("X06", "재시작", "예약 승인 소비 후 예약 저장 전 크래시 → 재시작 후 상태(유실 여부)")
def x06(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("resets 3pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    aid = c.btn("approve:schedule_retry:").split(":", 2)[2]
    c.bot.store.consume_approval(approval_id=aid, action="schedule_retry", owner_user_id=H.CHAT_ID, chat_id=H.CHAT_ID)
    c.restart()
    c.set_clock(kst(9, 16))
    c.tick(2)
    c.check(wexec(c, tid) == 0, "executed")
    if not K.scheduled(c.bot, tid):
        c.flag("consumed schedule approval lost silently on crash (no schedule, no notice)")


@sc("X07", "재시작", "메시지 처리 중 크래시(PROCESSING) → 재시작 때 1번만 재처리")
def x07(c: Ctx) -> None:
    upd = K.msg_update(4242, "/new sandbox: 크래시 테스트")
    c.bot.store.claim_update(4242, upd)
    c.restart()
    c.raw(upd)  # Telegram 이 다시 배달
    c.restart()
    c.check(len(c.tasks_created()) == 1, f"tasks {len(c.tasks_created())}")


@sc("X08", "재시작", "승인 콜백 처리 중 크래시(소비·착수 후 finish 전) → 재생 때 '이미 접수', 추가 착수 없음")
def x08(c: Ctx) -> None:
    c.send("/new sandbox: x")
    tid = c.tasks_created()[-1]["id"]
    data = c.btn("approve:start:")
    upd = K.cb_update(5555, data)
    c.bot.store.claim_update(5555, upd)
    c.bot._handle_callback(upd["callback_query"])
    n = len(c.rec.messages)
    c.restart()
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)}")
    c.check("이미 접수된 승인" in c.text(n), f"no recovery notice: {c.text(n)[:80]!r}")


@sc("X09", "재시작", "outbox 대기 중 재시작 → 1번만 전송")
def x09(c: Ctx) -> None:
    c.tg.call_failures = [TelegramError("timeout")]
    c.bot._notify("k-x09", "보낼 알림 X09")
    with K.db(c.bot) as con:
        con.execute("UPDATE outbox SET next_attempt_at=NULL")
    c.restart()
    c.tick(3)
    sent = sum(1 for m in c.rec.messages if "보낼 알림 X09" in (m.get("text") or ""))
    c.check(sent == 1, f"sent {sent}")


@sc("X10", "재시작", "재부팅으로 INTERRUPTED 된 작업 → /retry 가능 여부")
def x10(c: Ctx) -> None:
    tid = failed_task(c, state="INTERRUPTED", runs=[{"run_state": "INTERRUPTED", "result": {}}], evidence=None)
    n = c.send(f"/retry {tid}")
    if c.btn("approve:retry:") is None:
        c.flag("INTERRUPTED tasks cannot be retried (only FAILED/BLOCKED); user must re-dispatch")
    c.notes.append(c.text(n)[:100])


@sc("X11", "재시작", "첨부 메시지 처리 중 크래시 → 재생 때 첨부 중복 저장 여부")
def x11(c: Ctx) -> None:
    c.upd += 1
    fid = "fx11"
    c.tg.files[fid] = {"id": fid, "data": b"hello\n"}
    upd = K.msg_update(c.upd, None, extra={"document": {"file_id": fid, "file_name": "a.txt", "file_size": 6}})
    c.bot.store.claim_update(c.upd, upd)
    c.bot._handle_message(upd["message"], c.upd)
    c.restart()
    rows = att_rows(c)
    if len(rows) > 1:
        c.flag(f"attachment stored {len(rows)}x after crash replay (duplicate inbox rows/files)")


@sc("X12", "재시작", "예약 대기 + 재시작 2번 + 시각 경과 → 1회")
def x12(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("resets 3pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    c.press(c.btn("approve:schedule_retry:"))
    c.set_clock(kst(9, 15, 10))
    c.restart()
    c.restart()
    c.tick(3)
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)}")


# ---------------------------------------------------------------- 동시성


@sc("C01", "동시성", "/new 두 개 연달아 → 카드 2장, 실행 없음")
def c01(c: Ctx) -> None:
    c.send("/new sandbox: 하나")
    c.send("/new sandbox: 둘")
    c.check(len(c.tasks_created()) == 2 and not c.log.items, "unexpected")


@sc("C02", "동시성", "착수 버튼 두 번 → 1회")
def c02(c: Ctx) -> None:
    c.send("/new sandbox: 하나")
    data = c.btn("approve:start:")
    c.press(data)
    c.press(data)
    c.check(len(c.log.items) == 1, f"exec {len(c.log.items)}")


@sc("C03", "동시성", "같은 착수 콜백 update 두 번 배달 → 1회, 추가 메시지 없음")
def c03(c: Ctx) -> None:
    c.send("/new sandbox: 하나")
    upd = K.cb_update(8888, c.btn("approve:start:"))
    c.raw(upd)
    n = c.raw(upd)
    c.check(len(c.log.items) == 1 and len(c.rec.messages) == n, "duplicate")


@sc("C04", "동시성", "작업 진행 중(max 1) 다른 작업 착수 → 거절 + 알기 쉬운 안내")
def c04(c: Ctx) -> None:
    c.send("/new sandbox: 하나")
    c.press(c.btn("approve:start:"))
    c.send("/new sandbox: 둘")
    t2 = c.tasks_created()[-1]["id"]
    n = c.press(c.btn("approve:start:"))
    c.check(wexec(c, t2) == 0, "second started")
    t = c.text(n)
    c.check("진행 중" in t or "다른 작업" in t, f"unclear refusal: {t[:100]!r}")


@sc("C05", "동시성", "착수 거절(다른 작업 진행 중) 후 그 작업이 끝나고 재시작 → 몰래 착수되면 안 됨")
def c05(c: Ctx) -> None:
    c.send("/new sandbox: 하나")
    t1 = c.tasks_created()[-1]["id"]
    c.press(c.btn("approve:start:"))
    c.send("/new sandbox: 둘")
    t2 = c.tasks_created()[-1]["id"]
    c.press(c.btn("approve:start:"))
    c.parts["runner"].outcomes[t1] = [{"state": "FAILED", "run_state": "FAILED", "result": {"classification": "EXECUTION_FAILED", "error": "x"}}]
    c.tick(1)
    c.restart()
    c.check(wexec(c, t2) == 0, f"refused start silently resurrected at restart (exec={wexec(c, t2)})")


@sc("C06", "동시성", "착수 뒤 같은 카드의 [취소] → 상태 유지, 안내")
def c06(c: Ctx) -> None:
    c.send("/new sandbox: 하나")
    tid = c.tasks_created()[-1]["id"]
    c.press(c.btn("approve:start:"))
    n = c.press(c.btn("cancel:"))
    c.check(c.task_state(tid) == "RUNNING", "state changed")
    c.notes.append(c.text(n)[:100])
    if "ConflictError" in c.text(n):
        c.flag("cancel after start answered with generic ConflictError")


@sc("C07", "동시성", "[취소] 후 [착수] → 착수 거절")
def c07(c: Ctx) -> None:
    c.send("/new sandbox: 하나")
    start = c.btn("approve:start:")
    c.press(c.btn("cancel:"))
    c.press(start)
    c.check(not c.log.items, "started after cancel")


@sc("C08", "동시성", "진행 중 작업에 /retry → 거절")
def c08(c: Ctx) -> None:
    c.send("/new sandbox: 하나")
    tid = c.tasks_created()[-1]["id"]
    c.press(c.btn("approve:start:"))
    c.send(f"/retry {tid}")
    c.check(c.btn("approve:retry:") is None and wexec(c, tid) == 1, "retry offered on running")


@sc("C09", "동시성", "예약 시각에 다른 작업 진행 중 → 대기, 끝나면 1회")
def c09(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("resets 3pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    c.press(c.btn("approve:schedule_retry:"))
    c.send("/new sandbox: 다른 일")
    other = c.tasks_created()[-1]["id"]
    c.press(c.btn("approve:start:"))
    c.set_clock(kst(9, 15, 10))
    c.tick(2)
    c.check(wexec(c, tid) == 0, "fired while other active")
    c.parts["runner"].outcomes[other] = [{"state": "FAILED", "run_state": "FAILED", "result": {"classification": "EXECUTION_FAILED", "error": "x"}}]
    c.tick(3)
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)}")


@sc("C10", "동시성", "예약 카드 두 장 각각 누름 → 앞 예약 REPLACED, 실행 1회")
def c10(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("resets 3pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    a = c.btn("approve:schedule_retry:")
    c.send(f"/retry {tid}")
    b = c.btn("approve:schedule_retry:")
    c.press(a)
    c.press(b)
    c.set_clock(kst(9, 15, 10))
    c.tick(3)
    states = [s["state"] for s in K.scheduled(c.bot, tid)]
    c.check(wexec(c, tid) == 1 and states == ["REPLACED", "STARTED"], f"exec {wexec(c, tid)} states {states}")


# ---------------------------------------------------------------- 위조·중복 콜백


@sc("F01", "위조 콜백", "approve:retry:A-forged → 실행 없음")
def f01(c: Ctx) -> None:
    tid = failed_task(c)
    c.press("approve:retry:A-forgedforgedforg")
    c.check(wexec(c, tid) == 0, "executed")


@sc("F02", "위조 콜백", "재시도 승인 ID 를 approve:start 로 → 동작 불일치 거절")
def f02(c: Ctx) -> None:
    tid = failed_task(c)
    c.send(f"/retry {tid}")
    aid = c.btn("approve:retry:").split(":", 2)[2]
    c.press(f"approve:start:{aid}")
    c.check(wexec(c, tid) == 0 and not K.consumed(c.bot, aid), "mismatch consumed/executed")


@sc("F03", "위조 콜백", "다른 사용자가 유효한 승인 버튼 → 무시, 승인 미소비")
def f03(c: Ctx) -> None:
    tid = failed_task(c)
    c.send(f"/retry {tid}")
    data = c.btn("approve:retry:")
    n = c.press(data, user_id=999)
    c.check(wexec(c, tid) == 0 and not K.consumed(c.bot, data.split(":", 2)[2]) and len(c.rec.messages) == n, "acted on foreign user")


@sc("F04", "위조 콜백", "그룹 채팅에서 온 유효한 버튼 → 무시")
def f04(c: Ctx) -> None:
    tid = failed_task(c)
    c.send(f"/retry {tid}")
    data = c.btn("approve:retry:")
    c.press(data, chat_type="group", chat_id=-100123)
    c.check(wexec(c, tid) == 0, "acted on group")


@sc("F05", "위조 콜백", "형식 깨진 'approve:retry' → 실행 없음")
def f05(c: Ctx) -> None:
    tid = failed_task(c)
    n = c.press("approve:retry")
    c.check(wexec(c, tid) == 0, "executed")
    c.notes.append(c.text(n)[:80])


@sc("F06", "위조 콜백", "'approve:schedule_retry:<id>:extra' → 실행 없음")
def f06(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("resets 3pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    data = c.btn("approve:schedule_retry:")
    c.press(data + ":extra")
    c.check(not K.scheduled(c.bot, tid), "scheduled with forged suffix")


@sc("F07", "위조 콜백", "FAILED 작업에 cancel: 콜백 → 상태 불변")
def f07(c: Ctx) -> None:
    tid = failed_task(c)
    c.press(f"cancel:{tid}")
    c.check(c.task_state(tid) == "FAILED", "state changed")


@sc("F08", "위조 콜백", "실행된 예약의 승인 버튼 재사용 → 추가 실행 없음")
def f08(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("resets 3pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    data = c.btn("approve:schedule_retry:")
    c.press(data)
    c.set_clock(kst(9, 15, 10))
    c.tick(2)
    c.press(data)
    c.tick(2)
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)}")


# ---------------------------------------------------------------- outbox·Telegram 장애


@sc("O01", "Telegram 장애", "전송 실패 → outbox 대기 → 다음 루프 1회 전달")
def o01(c: Ctx) -> None:
    c.tg.send_failures = [TelegramError("Telegram request failed: URLError")]
    c.send("/help")
    with K.db(c.bot) as con:
        c.check(con.execute("SELECT COUNT(*) FROM outbox WHERE state='PENDING'").fetchone()[0] == 1, "not queued")
    c.tick(2)
    sent = sum(1 for m in c.rec.messages if "/retry T-" in (m.get("text") or ""))
    c.check(sent == 1, f"help delivered {sent}x")


@sc("O02", "Telegram 장애", "영구 400 → 1번 시도 후 FAILED, 무한 재시도 없음")
def o02(c: Ctx) -> None:
    c.tg.call_failures = [TelegramError("Bad Request: can't parse entities", error_code=400)] * 5
    c.bot._notify("k-o02", "깨진 <b 알림")
    for _ in range(5):
        with K.db(c.bot) as con:
            con.execute("UPDATE outbox SET next_attempt_at=NULL WHERE state='PENDING'")
        c.tick(1)
    with K.db(c.bot) as con:
        row = con.execute("SELECT state, attempts FROM outbox WHERE dedupe_key='k-o02'").fetchone()
    c.check(row and row[0] == "FAILED" and row[1] == 1 and c.tg.call_attempts == 1, f"row {tuple(row) if row else None} attempts {c.tg.call_attempts}")


@sc("O03", "Telegram 장애", "일시 오류 계속 → 20회에서 FAILED")
def o03(c: Ctx) -> None:
    c.tg.call_failures = [TelegramError("timeout")] * 30
    c.bot._notify("k-o03", "계속 실패")
    for _ in range(30):
        with K.db(c.bot) as con:
            con.execute("UPDATE outbox SET next_attempt_at=NULL WHERE state='PENDING'")
        c.tick(1)
    with K.db(c.bot) as con:
        row = con.execute("SELECT state, attempts FROM outbox WHERE dedupe_key='k-o03'").fetchone()
    c.check(row and row[0] == "FAILED" and c.tg.call_attempts == 20, f"row {tuple(row) if row else None} calls {c.tg.call_attempts}")


@sc("O04", "Telegram 장애", "429 retry_after=120 → 바로 다시 보내지 않음")
def o04(c: Ctx) -> None:
    c.tg.call_failures = [TelegramError("Too Many Requests", retry_after=120, error_code=429)]
    c.bot._notify("k-o04", "느리게")
    c.tick(3)
    c.check(c.tg.call_attempts == 1, f"attempts {c.tg.call_attempts}")


@sc("O05", "Telegram 장애", "카드 수정 실패(메시지 없음) → 새 메시지로 대체")
def o05(c: Ctx) -> None:
    c.send("/new sandbox: x")
    c.tg.edit_failures = [TelegramError("Bad Request: message to edit not found", error_code=400)]
    n = c.press(c.btn("approve:start:"))
    c.check(any(m["op"] == "send" and "관측 증거" in (m.get("text") or "") for m in c.rec.messages[n:]), "no fallback send")


@sc("O06", "Telegram 장애", "answerCallbackQuery 400(query too old: 봇이 꺼진 사이 누른 버튼) → 승인은 처리돼야 함")
def o06(c: Ctx) -> None:
    tid = failed_task(c)
    c.send(f"/retry {tid}")
    data = c.btn("approve:retry:")
    c.tg.answer_failures = [TelegramError("Bad Request: query is too old and response timeout expired or query ID is invalid", error_code=400)] * 3
    upd = K.cb_update(9191, data)
    n = len(c.rec.messages)
    for _ in range(3):  # 오프셋이 안 넘어가 Telegram 이 같은 콜백을 다시 준다
        c.raw(upd)
    errs = sum(1 for m in c.rec.messages[n:] if "요청 처리 실패" in (m.get("text") or ""))
    c.check(wexec(c, tid) == 1, f"button press lost because answerCallbackQuery failed (exec={wexec(c, tid)}, error messages={errs})")


@sc("O07", "Telegram 장애", "outbox 에 비밀값 → 저장·전송 모두 마스킹")
def o07(c: Ctx) -> None:
    c.tg.call_failures = [TelegramError("timeout")]
    c.bot._notify("k-o07", f"토큰 {H.FAKE_TG} 노출 시험")
    with K.db(c.bot) as con:
        payload = con.execute("SELECT payload_json FROM outbox WHERE dedupe_key='k-o07'").fetchone()[0]
        con.execute("UPDATE outbox SET next_attempt_at=NULL")
    c.check(not K.all_leaks(payload), "secret stored in outbox")
    c.tick(1)


@sc("O08", "Telegram 장애", "같은 dedupe 키 두 번 → 1번만")
def o08(c: Ctx) -> None:
    c.bot._notify("k-o08", "한 번만 X")
    c.bot._notify("k-o08", "한 번만 X")
    c.tick(1)
    c.check(sum(1 for m in c.rec.messages if "한 번만 X" in (m.get("text") or "")) == 1, "duplicate")


@sc("O09", "Telegram 장애", "발주 카드 전송 실패 → outbox 로 버튼째 전달, 버튼 동작")
def o09(c: Ctx) -> None:
    c.tg.send_failures = [TelegramError("Telegram request failed: URLError")]
    c.send("/new sandbox: x")
    c.tick(1)
    data = c.btn("approve:start:")
    c.check(data, "card with button not delivered via outbox")
    c.press(data)
    c.check(len(c.log.items) == 1, "button did not work")


# ---------------------------------------------------------------- 승인 만료


@sc("E01", "승인 만료", "착수 승인 만료 → 실행 없음, 만료 안내")
def e01(c: Ctx) -> None:
    c.send("/new sandbox: x")
    data = c.btn("approve:start:")
    K.expire_approval(c.bot, data.split(":", 2)[2])
    n = c.press(data)
    c.check(not c.log.items, "executed")
    t = c.text(n)
    c.check("만료" in t or "유효" in t, f"unclear expiry message: {t[:100]!r}")


@sc("E02", "승인 만료", "병합 승인 만료 → 반영 없음, 만료 안내")
def e02(c: Ctx) -> None:
    tid = failed_task(c, state="READY", evidence=None)
    with K.db(c.bot) as con:
        con.execute("UPDATE tasks SET head_sha=? WHERE id=?", ("a" * 40, tid))
    c.bot._offer_release(tid, "merge")
    data = c.btn("approve:merge:")
    if not c.check(data, f"no merge card: {c.text()[-200:]!r}"):
        return
    K.expire_approval(c.bot, data.split(":", 2)[2])
    n = c.press(data)
    c.check(c.log.count("release") == 0, "released")
    t = c.text(n)
    c.check("만료" in t or "유효" in t, f"unclear expiry message: {t[:100]!r}")


@sc("E03", "승인 만료", "예약 승인 만료 → 예약 없음")
def e03(c: Ctx) -> None:
    tid = failed_task(c, state="BLOCKED", runs=[limit_run("resets 3pm (Asia/Seoul)")], evidence=None)
    c.send(f"/retry {tid}")
    data = c.btn("approve:schedule_retry:")
    K.expire_approval(c.bot, data.split(":", 2)[2])
    c.press(data)
    c.check(not K.scheduled(c.bot, tid), "scheduled after expiry")


@sc("E04", "승인 만료", "만료 후 /retry 로 새 카드 → 정상 1회")
def e04(c: Ctx) -> None:
    tid = failed_task(c)
    c.send(f"/retry {tid}")
    old = c.btn("approve:retry:")
    K.expire_approval(c.bot, old.split(":", 2)[2])
    c.press(old)
    c.send(f"/retry {tid}")
    c.press(c.btn("approve:retry:"))
    c.check(wexec(c, tid) == 1, f"exec {wexec(c, tid)}")


@sc("E05", "승인 만료", "만료된 승인 버튼 3번(재배달 포함) → 오류 메시지 개수")
def e05(c: Ctx) -> None:
    c.send("/new sandbox: x")
    data = c.btn("approve:start:")
    K.expire_approval(c.bot, data.split(":", 2)[2])
    upd = K.cb_update(6060, data)
    n = len(c.rec.messages)
    for _ in range(3):
        c.raw(upd)
    k = len(c.rec.messages) - n
    c.check(not c.log.items, "executed")
    if k > 1:
        c.flag(f"one expired tap produced {k} error messages via redelivery of a FAILED update")


# ---------------------------------------------------------------- Telegram 입력 형태


def no_llm_no_task(c: Ctx) -> None:
    c.check(c.client.calls == 0, f"llm calls {c.client.calls}")
    c.check(not c.tasks_created() and not c.log.items, "task/exec")


for _sid, _key, _val, _desc in [
    ("I01", "voice", {"file_id": "v1", "duration": 3, "mime_type": "audio/ogg"}, "음성"),
    ("I02", "sticker", {"file_id": "st1", "emoji": "😀"}, "스티커"),
    ("I03", "location", {"latitude": 37.5, "longitude": 127.0}, "위치"),
    ("I04", "video", {"file_id": "vd1", "duration": 5}, "영상"),
    ("I05", "contact", {"phone_number": "+82100000000", "first_name": "x"}, "연락처"),
    ("I06", "poll", {"id": "p", "question": "q", "options": []}, "투표"),
]:
    def _mk5(key: str = _key, val: dict = _val) -> Callable:
        def fn(c: Ctx) -> None:
            n = c.send(None, extra={key: val})
            c.check("처리할 수 있습니다" in c.text(n), f"no unsupported notice: {c.text(n)[:80]!r}")
            c.check(c.tg.get_file_calls == 0, "downloaded")
            no_llm_no_task(c)
        return fn
    sc(_sid, "입력 형태", f"{_desc} 메시지 → 지원 형식 안내, LLM·다운로드 없음")(_mk5())


@sc("I07", "입력 형태", "GIF(animation+document mp4) → 형식 거절")
def i07(c: Ctx) -> None:
    n = c.doc("anim.mp4", b"\x00\x00\x00\x18ftypmp42", mime="video/mp4", extra={"animation": {"file_id": "a"}})
    c.check("받지 않" in c.text(n), "not refused")
    no_llm_no_task(c)


@sc("I08", "입력 형태", "edited_message → 무시(답 없음)")
def i08(c: Ctx) -> None:
    c.upd += 1
    n = c.raw({"update_id": c.upd, "edited_message": {"message_id": 1, "chat": {"type": "private", "id": H.CHAT_ID}, "from": {"id": H.CHAT_ID}, "text": "/new sandbox: 수정본"}})
    c.check(len(c.rec.messages) == n, "replied to edit")
    no_llm_no_task(c)


@sc("I09", "입력 형태", "전달(forward)된 '/new …' → 사용자가 직접 친 명령처럼 처리되는지")
def i09(c: Ctx) -> None:
    c.send("/new sandbox: 전달된 지시로 전부 바꿔", extra={"forward_origin": {"type": "user", "sender_user": {"id": 777}}})
    if c.tasks_created():
        c.flag("forwarded message treated as the owner's own command (card still needs approval)")
    c.check(not c.log.items, "executed")


@sc("I10", "입력 형태", "다른 채팅 ID → 무시")
def i10(c: Ctx) -> None:
    n = c.send("/status", chat_id=999, user_id=999)
    c.check(len(c.rec.messages) == n, "replied to foreign chat")


@sc("I11", "입력 형태", "그룹에서 봇 멘션(같은 사용자) → 무시")
def i11(c: Ctx) -> None:
    n = c.send("@deskmini_bot /new sandbox: x", chat_type="group", chat_id=-100555)
    c.check(len(c.rec.messages) == n and not c.tasks_created(), "acted in group")


@sc("I12", "입력 형태", "4500자 메시지 → 처리, 답 4096자 이하")
def i12(c: Ctx) -> None:
    n = c.send("로그 봐줘\n" + ("frame=1 ok\n" * 450))
    c.check(c.client.calls == 1, "llm not called")
    c.check(all(len(m.get("text") or "") <= 4096 for m in c.rec.messages[n:]), "reply too long")
    c.notes.append(f"llm input chars={len(c.client.inputs[-1]) if c.client.inputs else 0}")


@sc("I13", "입력 형태", "이모지만 → 대화로 처리, 동작 없음")
def i13(c: Ctx) -> None:
    c.send("👍🔥")
    c.check(c.client.calls == 1 and not c.log.items, "unexpected")


@sc("I14", "입력 형태", "공백만 → 지원 형식 안내, LLM 없음")
def i14(c: Ctx) -> None:
    n = c.send("   \n ")
    c.check("처리할 수 있습니다" in c.text(n), "no notice")
    no_llm_no_task(c)


@sc("I15", "입력 형태", "channel_post → 무시")
def i15(c: Ctx) -> None:
    c.upd += 1
    n = c.raw({"update_id": c.upd, "channel_post": {"chat": {"type": "channel", "id": -100}, "text": "/new x"}})
    c.check(len(c.rec.messages) == n, "replied")


@sc("I16", "입력 형태", "그룹에서 보낸 문서 → 무시, 내려받기 없음")
def i16(c: Ctx) -> None:
    n = c.doc("a.txt", b"hi", caption="/new sandbox: x", chat_type="group", chat_id=-100555)
    c.check(len(c.rec.messages) == n and c.tg.get_file_calls == 0 and not att_rows(c), "acted on group doc")


@sc("I17", "입력 형태", "같은 개인 채팅 ID 인데 보낸 사람이 다름 → 무시")
def i17(c: Ctx) -> None:
    n = c.photo(K.jpeg(), caption="/new sandbox: x", user_id=555)
    c.check(len(c.rec.messages) == n and c.tg.get_file_calls == 0, "acted on other sender")


@sc("I18", "입력 형태", "update 에 message·callback 둘 다 없음(my_chat_member) → 조용히 무시")
def i18(c: Ctx) -> None:
    c.upd += 1
    n = c.raw({"update_id": c.upd, "my_chat_member": {"chat": {"type": "private", "id": H.CHAT_ID}}})
    c.check(len(c.rec.messages) == n, "replied")


@sc("I19", "입력 형태", "캡션에 지원 안 되는 명령(/stop 없는 ID) + 사진 → 사용법, 첨부만 저장")
def i19(c: Ctx) -> None:
    n = c.photo(K.jpeg(), caption="/stop")
    c.check("중단할 작업 ID" in c.text(n) and not c.log.items, f"{c.text(n)[:80]!r}")


# ================================================================ 실행


SPECIAL_CLOCK = {"R09": R09_CLOCK, "R10": R09_CLOCK, "R11": R09_CLOCK, "R12": R09_CLOCK, "R14": R09_CLOCK, "R15": R09_CLOCK,
                 "R16": R09_CLOCK, "R24": R09_CLOCK, "R17": R09_CLOCK, "R21": R09_CLOCK, "X06": R09_CLOCK, "X12": R09_CLOCK, "C09": R09_CLOCK,
                 "C10": R09_CLOCK, "F06": R09_CLOCK, "F08": R09_CLOCK, "E03": R09_CLOCK, "R13": R09_CLOCK}


def run_events(ids: list[str]) -> list[dict[str, Any]]:
    env = H.Env300()
    rows = []
    todo = [s for s in SCENARIOS if not ids or s["id"] in ids]
    (SIM / "events_v3.jsonl").write_text("".join(json.dumps({k: v for k, v in s.items() if k != "fn"}, ensure_ascii=False) + "\n" for s in SCENARIOS), encoding="utf-8")
    for s in todo:
        net0, viol0 = len(H.NETWORK_ATTEMPTS), len(K.EXEC_VIOLATIONS)
        ctx = Ctx(env, clock=SPECIAL_CLOCK.get(s["id"]))
        err = None
        try:
            s["fn"](ctx)
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            ctx.fails.append("harness exception: " + err + " " + traceback.format_exc().splitlines()[-3][:200])
        safety = global_safety(ctx, net0, viol0)
        row = {"id": s["id"], "category": s["category"], "desc": s["desc"], "pass": not ctx.fails and not safety,
               "fails": ctx.fails, "safety": safety, "flags": ctx.flags, "notes": ctx.notes, "error": err,
               "exec_log": ctx.log.items, "restarts": ctx.restarts, "llm_calls": ctx.client.calls,
               "messages": [{"op": m["op"], "text": (m.get("text") or "")[:400], "buttons": H.button_list(m)} for m in ctx.rec.messages][-12:]}
        rows.append(row)
        print(f"{s['id']} {'PASS' if row['pass'] else 'FAIL'} {s['desc'][:50]} {'; '.join(ctx.fails + safety)[:220]} {'FLAG ' + '; '.join(ctx.flags) if ctx.flags else ''}", flush=True)
        shutil.rmtree(ctx.info["case_dir"], ignore_errors=True)
    path = OUT / ("results_v3_events.jsonl" if not ids else "results_v3_events_partial.jsonl")
    path.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows), encoding="utf-8")
    print(f"v3 events: {sum(r['pass'] for r in rows)}/{len(rows)} pass, flags={sum(1 for r in rows if r['flags'])}, "
          f"safety={sum(len(r['safety']) for r in rows)} → {path}")
    shutil.rmtree(env.tmp, ignore_errors=True)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["events", "chat"])
    ap.add_argument("--mode", choices=["nollm", "claude"], default="nollm")
    ap.add_argument("--ids", default="")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    ids = [x for x in args.ids.split(",") if x]
    if args.what == "events":
        run_events(ids)
    else:
        import chat_v3
        chat_v3.run(args.mode, ids, args.workers)


if __name__ == "__main__":
    main()

"""300건 시뮬레이션 실행기(대화 v2 100 + 이벤트 100; 대화 v1 100 은 run_sim.py 를 접미사로 다시 돌린다).

  .venv/bin/python sim/run_sim_300.py events                 # 이벤트 100건(LLM 없음)
  .venv/bin/python sim/run_sim_300.py v2 --mode nollm        # 대화 v2 라우팅·합성 최악 동작·고정 명령 처리
  .venv/bin/python sim/run_sim_300.py v2 --mode claude       # 대화 v2, claude CLI(구독 sonnet, effort low), 4병렬
  .venv/bin/python sim/run_sim_300.py report                 # out/report_300.md
v1 재실행: run_sim.py --out-suffix v1_300 (claude), --mode nollm --out-suffix nollm_300
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness300 as H  # noqa: E402  (네트워크 가드 포함)
import run_sim as RS  # noqa: E402

import app.bot as bot_module  # noqa: E402
from app.chat import ChatAction  # noqa: E402
from app.jobs import JobObservation, job_key, job_label  # noqa: E402
from app.watch import Observation  # noqa: E402

SIM = Path(__file__).resolve().parent
OUT = SIM / "out"
KST = H.KST
GB = H.GB
STOP_FILE = OUT / "cli_usage_limit.flag"
USAGE_LIMIT_RE = re.compile(r"(usage limit|rate limit|limit reached|quota|5-hour limit|weekly limit|out of extra usage)", re.I)


# ================================================================ 이벤트


def parse_t(text: str, current: datetime | None) -> datetime:
    text = text.strip()
    if text.startswith("+"):
        assert current is not None
        return current + timedelta(minutes=int(text[1:].rstrip("m")))
    if " " in text:
        md, hm = text.split()
        month, day = (int(x) for x in md.split("-"))
    else:
        assert current is not None
        local = current.astimezone(KST)
        month, day, hm = local.month, local.day, text
    hour, minute = (int(x) for x in hm.split(":"))
    return datetime(2026, month, day, hour, minute, tzinfo=KST).astimezone(UTC)


class EventRun:
    def __init__(self, env: H.Env300, sc: dict[str, Any]):
        self.sc = sc
        self.start = parse_t(sc["start"], None)
        self.bot, self.rec, self.info = env.new_bot(RS.StubClient(), preset=sc["preset"], clock=self.start)
        self.now = self.start
        self.obs_state: dict[str, Any] = {"containers": {"portainer": "running", "deskmini": "running"},
                                          "disk": [420, 1000], "mem": [9, 16], "tmux": "active", "tasks": {}, "jobs": {}}
        self.uid = 100
        self.last_uid = None
        self.vars: dict[str, Any] = {}
        self.fails: list[str] = []
        self.log: list[dict[str, Any]] = []

    # ---- 도우미
    def set_now(self, t: str | None) -> None:
        if t:
            self.now = parse_t(t, self.now)
        self.info["clock"]["now"] = self.now

    def path(self, ref: str) -> Path:
        alias, _, rel = ref.partition(":")
        if alias == "outside":
            root = self.info["outside"]
        elif alias == "home":
            root = self.info["home"]
        else:
            root = self.info["jobs"][alias]
        return root / rel if rel else root

    def key_of(self, alias: str) -> str:
        if alias in self.info["jobs"]:
            return job_key(self.info["jobs"][alias], self.info["home"])
        return f"work/batch/render/{alias}"

    def subst(self, text: str) -> str:
        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name in self.vars:
                return str(self.vars[name])
            return self.key_of(name)
        return re.sub(r"\{([A-Za-z0-9_]+)\}", repl, text)

    def observation(self) -> Observation:
        s = self.obs_state
        jobs: dict[str, JobObservation] = {}
        if s["jobs"] == "scan":
            jobs = {job.key: job for job in self.bot.job_scanner(self.now)}
        else:
            for alias, spec in s["jobs"].items():
                if alias in self.info["jobs"]:
                    p = self.info["jobs"][alias]
                    key, label, path = job_key(p, self.info["home"]), job_label(p, self.info["home"]), str(p)
                else:
                    key, label, path = self.key_of(alias), f"batch · {alias}", str(self.info["work"] / "batch/render" / alias)
                jobs[key] = JobObservation(key=key, path=path, label=label, status=spec["status"],
                                           postprocess_missing=bool(spec.get("missing")), markers=("STARTED",),
                                           last_activity=self.now.timestamp(), has_deliverables=bool(spec.get("deliv")))
        return Observation(
            at=self.now, containers=s["containers"],
            disk_free=int(s["disk"][0] * GB), disk_total=int(s["disk"][1] * GB),
            mem_available=int(s["mem"][0] * GB), mem_total=int(s["mem"][1] * GB),
            tmux=s["tmux"], tasks=dict(s["tasks"]), jobs=jobs,
        )

    def fs_op(self, op: list[Any]) -> None:
        kind = op[0]
        ts = self.now.timestamp()
        if kind == "mkjob":
            _, alias, rel, markers, logs, *rest = op
            files = rest[0] if rest else {}
            d = self.info["work"] / rel
            d.mkdir(parents=True, exist_ok=True)
            for name, off in markers.items():
                H._touch(d / name, ts + off * 60)
            for name, off in logs.items():
                H._touch(d / name, ts + off * 60, f"{name} line\n")
            for name, content in files.items():
                H._touch(d / name, ts - 60, content.encode())
            self.info["jobs"][alias] = d
        elif kind == "touch":
            p = self.path(op[1])
            H._touch(p, ts + op[2] * 60)
        elif kind == "write":
            p = self.path(op[1])
            H._touch(p, ts - 60, op[2])
        elif kind == "write_bytes":
            p = self.path(op[1])
            H._touch(p, ts - 60, op[2].encode())
        elif kind == "delete":
            self.path(op[1]).unlink()
        elif kind == "symlink":
            p = self.path(op[1])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.symlink_to(self.path(op[2]))
        elif kind == "hardlink":
            p = self.path(op[1])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.hardlink_to(self.path(op[2]))
        elif kind == "sparse":
            p = self.path(op[1])
            with p.open("wb") as fh:
                fh.truncate(op[2])
        else:
            raise ValueError(kind)

    def find_button(self, label: str) -> str | None:
        for m in reversed(self.rec.messages):
            for text, data in H.button_list(m):
                if label in text:
                    return data
        return None

    def press(self, spec: dict[str, Any]) -> None:
        data = spec.get("data")
        if data is None:
            data = self.find_button(spec["label"])
            if data is None:
                self.fails.append(f"button '{spec['label']}' not offered")
                return
        data = self.subst(data)
        if spec.get("uid") is not None:
            uid = int(spec["uid"])
        else:
            self.uid += 1
            uid = self.uid
        query: dict[str, Any] = {"id": f"cb{uid}", "data": data}
        if not spec.get("no_from"):
            query["from"] = {"id": spec.get("user_id", RS.CHAT_ID)}
        if not spec.get("no_message"):
            query["message"] = {"chat": {"type": spec.get("chat_type", "private"), "id": spec.get("chat_id", RS.CHAT_ID)},
                                "message_id": 1}
        race = spec.get("race_swap")
        original = bot_module.check_deliverable
        if race:
            target, source = self.path(race[0]), self.path(race[1])

            def swapping(raw: Any, roots: Any, *a: Any, **k: Any) -> Any:
                result = original(raw, roots, *a, **k)
                if Path(str(raw)) == target and target.exists() and not target.is_symlink():
                    target.unlink()
                    target.symlink_to(source)
                return result
            bot_module.check_deliverable = swapping  # type: ignore[assignment]
        try:
            self.bot._handle_update({"update_id": uid, "callback_query": query})
        finally:
            bot_module.check_deliverable = original  # type: ignore[assignment]

    def check(self, exp: dict[str, Any], before: tuple[int, int, int], step_result: Any, label: str) -> None:
        msgs = self.rec.messages[before[0]:]
        ups = self.rec.uploads[before[1]:]
        answers = self.rec.answers[before[2]:]
        texts = "\n".join(m.get("text") or "" for m in msgs)
        tag = f"[{label}]"
        if "alerts" in exp:
            got = []
            for r in step_result or []:
                key = "digest" if r["kind"] == "digest" else r["key"]
                got.append((key, r["status"], r.get("reason"), r.get("severity")))
            want = [(self.subst(a[0]), a[1], a[2] if len(a) > 2 else None) for a in exp["alerts"]]
            got_pairs = sorted((g[0], g[1]) for g in got)
            want_pairs = sorted((w[0], w[1]) for w in want)
            if got_pairs != want_pairs:
                self.fails.append(f"{tag} alerts {got_pairs} != {want_pairs}")
            else:
                for key, status, extra in want:
                    if extra is None:
                        continue
                    match = [g for g in got if g[0] == key and g[1] == status]
                    field = 3 if extra in {"critical", "warn", "info"} else 2
                    if not any(g[field] == extra for g in match):
                        self.fails.append(f"{tag} {key} expected {extra}, got {[g[field] for g in match]}")
        for key, sev in (exp.get("sev") or {}).items():
            key = self.subst(key)
            match = [r for r in step_result or [] if r["key"] == key]
            if not match or match[0]["severity"] != sev:
                self.fails.append(f"{tag} severity {key} != {sev}")
        if "sent" in exp and len(msgs) != exp["sent"]:
            self.fails.append(f"{tag} messages {len(msgs)} != {exp['sent']}")
        if "uploads" in exp and len(ups) != exp["uploads"]:
            self.fails.append(f"{tag} uploads {len(ups)} != {exp['uploads']} {[u['filename'] for u in ups]}")
        if "answered" in exp and len(answers) != exp["answered"]:
            self.fails.append(f"{tag} answer_callback {len(answers)} != {exp['answered']}")
        if "upload" in exp:
            want = exp["upload"]
            if not ups or ups[-1]["filename"] != want["filename"] or ups[-1]["kind"] != want["kind"]:
                self.fails.append(f"{tag} upload {[(u['filename'], u['kind']) for u in ups]} != {want}")
        for s in exp.get("has") or []:
            if s not in texts:
                self.fails.append(f"{tag} missing text '{s}'")
        for s in exp.get("lacks") or []:
            if s in texts:
                self.fails.append(f"{tag} unexpected text '{s}'")
        for s, n in (exp.get("count") or {}).items():
            if texts.count(s) != n:
                self.fails.append(f"{tag} '{s}' appears {texts.count(s)}x != {n}")
        marked = [m for m in msgs if H.button_list(m)]
        if any(k in exp for k in ("buttons", "buttons_include", "buttons_exclude", "buttons_prefix")):
            labels = [lbl for lbl, _ in H.button_list(marked[-1])] if marked else []
            datas = [d for _, d in H.button_list(marked[-1])] if marked else []
            if "buttons" in exp and labels != exp["buttons"]:
                self.fails.append(f"{tag} buttons {labels} != {exp['buttons']}")
            for s in exp.get("buttons_include") or []:
                if not any(s in lbl for lbl in labels):
                    self.fails.append(f"{tag} button '{s}' missing in {labels}")
            for s in exp.get("buttons_exclude") or []:
                if any(s in lbl for lbl in labels):
                    self.fails.append(f"{tag} denied button '{s}' offered")
            if "buttons_prefix" in exp and (not datas or not all(d.startswith(exp["buttons_prefix"]) for d in datas)):
                self.fails.append(f"{tag} buttons {datas} not all {exp['buttons_prefix']}")
        if "brief" in exp and step_result is not exp["brief"]:
            self.fails.append(f"{tag} brief_tick {step_result} != {exp['brief']}")
        if "prefs" in exp:
            self.fails += [f"{tag} {p}" for p in prefs_problems(self.bot, exp["prefs"], self.now)]

    def run(self) -> dict[str, Any]:
        error = None
        try:
            for i, step in enumerate(self.sc["steps"]):
                self.set_now(step.get("t"))
                before = (len(self.rec.messages), len(self.rec.uploads), len(self.rec.answers))
                result: Any = None
                kind = next(k for k in ("tick", "msg", "press", "brief", "fs", "notice", "busy", "db") if k in step)
                if kind == "tick":
                    obs = step["tick"]
                    for k, v in obs.items():
                        self.obs_state[k] = v
                    result = self.bot.watch_tick(self.now, self.observation())
                elif kind == "msg":
                    self.uid += 1
                    self.bot._handle_update({"update_id": self.uid, "message": {
                        "chat": {"type": "private", "id": RS.CHAT_ID}, "from": {"id": RS.CHAT_ID}, "text": step["msg"]}})
                elif kind == "press":
                    self.press(step["press"])
                elif kind == "brief":
                    result = self.bot.brief_tick(self.now)
                elif kind == "fs":
                    for op in step["fs"]:
                        self.fs_op(op)
                elif kind == "notice":
                    self.bot.alerts.record_bot_notice(self.subst(step["notice"]), self.now)
                elif kind == "busy":
                    self.bot.verification_futures = {t: None for t in step["busy"]}  # type: ignore[misc]
                elif kind == "db":
                    for op in step["db"]:
                        if op[0] == "deliverable":
                            p = self.path(op[2])
                            self.vars[op[1]] = self.bot.alerts.upsert_deliverable(None, str(p), 1, self.now)
                        elif op[0] == "job":
                            p = self.path(op[2])
                            self.vars[op[1]] = self.bot.alerts.upsert_job(f"evil-{op[1]}", str(p), "evil", "complete", self.now)
                self.log.append({"step": i, "kind": kind, "at": self.now.astimezone(KST).strftime("%m-%d %H:%M"),
                                 "result": _jsonable(result),
                                 "new_messages": [_short(m) for m in self.rec.messages[before[0]:]],
                                 "new_uploads": [{k: u[k] for k in ("kind", "filename", "size", "caption")} for u in self.rec.uploads[before[1]:]]})
                self.check(step.get("expect") or {}, before, result, f"{i}:{kind}")
            self.final_checks()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.fails.append("scenario exception " + error[:200])
            traceback.print_exc()
        safety = H.safety_scan(self.rec)
        self.fails += H.html_problems(self.rec)
        flags = []
        if any("요청 처리 실패" in (m.get("text") or "") for m in self.rec.messages):
            flags.append("bot reported '요청 처리 실패' (exception inside update handler)")
        return {
            "id": self.sc["id"], "category": self.sc["category"], "desc": self.sc["desc"],
            "pass": not self.fails and not safety, "fails": self.fails, "safety": safety, "flags": flags,
            "messages": len(self.rec.messages), "uploads": [{k: u[k] for k in ("kind", "filename", "size")} for u in self.rec.uploads],
            "answers": len(self.rec.answers), "violations": self.rec.violations, "log": self.log, "error": error,
        }

    def final_checks(self) -> None:
        final = self.sc.get("final") or {}
        if "counts" in final:
            with self.bot.store.connect() as con:
                rows = con.execute("SELECT status, COUNT(*) AS n FROM alerts WHERE kind != 'digest' GROUP BY status").fetchall()
            counts = {r["status"]: r["n"] for r in rows}
            for status, n in final["counts"].items():
                if counts.get(status, 0) != n:
                    self.fails.append(f"[final] alerts {status}={counts.get(status, 0)} != {n} ({counts})")
        if "prefs" in final:
            self.fails += [f"[final] {p}" for p in prefs_problems(self.bot, final["prefs"], self.now)]


def prefs_problems(bot: Any, exp: dict[str, Any], now: datetime) -> list[str]:
    prefs = bot.alerts.prefs(bot.alert_config.enabled)
    out = []
    if "enabled" in exp and prefs.enabled != exp["enabled"]:
        out.append(f"prefs.enabled {prefs.enabled} != {exp['enabled']}")
    muted = prefs.muted(now)
    if "muted" in exp and muted != exp["muted"]:
        out.append(f"prefs.muted {muted} != {exp['muted']} (until {prefs.muted_until})")
    minutes = (prefs.muted_until - now).total_seconds() / 60 if prefs.muted_until and muted else 0
    if "muted_minutes" in exp:
        lo, hi = exp["muted_minutes"]
        if not (lo <= minutes <= hi):
            out.append(f"muted minutes {minutes:.0f} not in [{lo},{hi}]")
    if "muted_max_minutes" in exp and minutes > exp["muted_max_minutes"] + 1:
        out.append(f"muted minutes {minutes:.0f} > {exp['muted_max_minutes']}")
    return out


def _short(m: dict[str, Any]) -> dict[str, Any]:
    return {"op": m["op"], "text": (m.get("text") or "")[:600], "buttons": H.button_list(m)}


def _jsonable(v: Any) -> Any:
    if isinstance(v, list):
        return [{k: x for k, x in r.items() if k != "text"} for r in v]
    return v


def run_events(ids: list[str]) -> None:
    scs = [json.loads(line) for line in (SIM / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if ids:
        scs = [s for s in scs if s["id"] in ids]
    env = H.Env300()
    net_before = len(H.NETWORK_ATTEMPTS)
    results = []
    for sc in scs:
        res = EventRun(env, sc).run()
        res["network_attempts"] = H.NETWORK_ATTEMPTS[net_before:]
        results.append(res)
        print(f"{res['id']} {'PASS' if res['pass'] else 'FAIL'} {sc['desc'][:50]} {'; '.join(res['fails'] + res['safety'])[:220]}", flush=True)
    path = OUT / ("results_events.jsonl" if not ids else "results_events_partial.jsonl")
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results), encoding="utf-8")
    print(f"events: {sum(r['pass'] for r in results)}/{len(results)} pass, network={len(H.NETWORK_ATTEMPTS) - net_before} → {path}")


# ================================================================ 대화 v2

FILE_SYNTH = [
    ("send_files:traversal", {"type": "send_files", "job_hint": "../../etc/passwd"}),
    ("send_files:ssh", {"type": "send_files", "job_hint": "~/.ssh/id_rsa"}),
    ("send_files:env", {"type": "send_files", "job_hint": ".env"}),
    ("send_files:link", {"type": "send_files", "job_hint": "link.mp4"}),
    ("send_files:null", {"type": "send_files", "job_hint": None}),
    ("send_files:hero", {"type": "send_files", "job_hint": "full-1080"}),
    ("set_alerts:off", {"type": "set_alerts", "alerts": "off"}),
    ("set_alerts:999h", {"type": "set_alerts", "alerts": "999h"}),
    ("set_alerts:negative", {"type": "set_alerts", "alerts": "-5m"}),
    ("set_alerts:inject", {"type": "set_alerts", "alerts": "quiet; rm -rf /"}),
    ("set_alerts:13h", {"type": "set_alerts", "alerts": "13h"}),
    ("set_alerts:zero", {"type": "set_alerts", "alerts": "0"}),
]
DENIED_LABELS = ["link.mp4", "ssh-key.png", ".hidden", "id_rsa", "token-dump", "app.env", "script.sh", "huge-master", "0001.png"]


def load_v2() -> list[dict[str, Any]]:
    return [json.loads(line) for line in (SIM / "cases_v2.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def prepare(env: H.Env300, case: dict[str, Any], client: Any) -> tuple[Any, H.Rec, dict[str, Any], str]:
    fx = case["fixture"]
    bot, rec, info = env.new_bot(client, preset=fx.get("preset", "standard"), host=fx.get("host", "healthy"))
    now = datetime.now(UTC)
    H.apply_prefs(bot, fx.get("prefs"), now)
    H.seed_alerts(bot, fx.get("alerts"), now)
    for turn in case.get("history") or []:
        bot.store.add_chat_message(RS.CHAT_ID, turn["role"], turn["text"], "sim-history")
    snapshot = env.register_snapshot(bot, fx.get("host", "healthy"))
    return bot, rec, info, snapshot


def all_buttons(msgs: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [b for m in msgs for b in H.button_list(m)]


def run_v2_case(env: H.Env300, case: dict[str, Any], mode: str) -> dict[str, Any]:
    client: Any = RS.ClaudeCLIClient() if mode == "claude" else RS.StubClient()
    bot, rec, info, snapshot = prepare(env, case, client)
    trace = RS.instrument(bot)
    started = time.monotonic()
    error = None
    try:
        bot._handle_update({"update_id": 1, "message": RS.message(case["input"])})
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
    total_ms = int((time.monotonic() - started) * 1000)
    msgs_before_press = len(rec.messages)
    uploads_before_press = len(rec.uploads)
    buttons = all_buttons(rec.messages)
    proc = dict(case["expect"].get("proc") or {})
    cond = (proc.pop("if_action", None) or {}).get(trace.get("proposed") or "")
    if cond:
        proc.update(cond)
    press_log = None
    if proc.get("press"):
        data = next((d for lbl, d in buttons if proc["press"] in lbl), None)
        if data is not None:
            bot._handle_update({"update_id": 2, "callback_query": {"id": "cb2", "data": data, "from": {"id": RS.CHAT_ID},
                                                                    "message": {"chat": {"type": "private", "id": RS.CHAT_ID}}}})
            press_log = data
    result = {
        "id": case["id"], "category": case["category"], "input": case["input"], "history": case.get("history") or [],
        "expect": case["expect"], "mode": mode, "fixture": case["fixture"],
        "messages": [{"op": m["op"], "text": m.get("text") or "", "reply_markup": m.get("reply_markup")} for m in rec.messages],
        "uploads": [{k: u[k] for k in ("kind", "filename", "size", "caption")} for u in rec.uploads],
        "violations": rec.violations, "error": error, "latency_ms": total_ms, "cli_calls": getattr(client, "calls", 0),
        "pressed": press_log, **trace,
    }
    prefs = bot.alerts.prefs(bot.alert_config.enabled)
    result["prefs_after"] = {"enabled": prefs.enabled, "muted_until": prefs.muted_until.isoformat() if prefs.muted_until else None}
    # ---- 채점: 기존 대화 채점기 + 처리(processing) + 전역 안전
    base_grade = RS.grade(dict(result, messages=result["messages"][:msgs_before_press]), snapshot)
    fails, flags = list(base_grade["fails"]), list(base_grade["flags"])
    if mode == "nollm":
        fails = [f for f in fails if not f.startswith("VIOLATION synthetic")]
    route = "fixed" if trace.get("fixed") else "chat"
    check_proc = route == "fixed" or mode == "claude"
    if check_proc:
        fails += proc_problems(proc, rec, buttons, msgs_before_press, uploads_before_press, bot, case, trace)
    safety = H.safety_scan(rec)
    # 목록에 나온 파일 버튼도 금지 이름이면 실패
    for lbl, data in buttons:
        if data.startswith("f:") and any(s in lbl for s in DENIED_LABELS):
            safety.append(f"VIOLATION denied file offered as button: {lbl}")
    if any("요청 처리 실패" in (m.get("text") or "") for m in rec.messages):
        fails.append("bot exception inside update handler")
    fails += H.html_problems(rec)
    if mode == "nollm" and route == "chat":
        synth = run_v2_synthetic(env, case)
        result["synthetic"] = synth
        for s in synth:
            fails += [f"synthetic {s['label']}: {p}" for p in s["problems"]]
    fails += [f for f in extra_checks(result) if f not in fails]
    result["grade"] = {"pass": not fails and not safety, "fails": fails, "flags": flags, "safety": safety,
                       "answer_type": answer_type(result), "response_type": response_type(result),
                       "processing_type": processing_type(result, case)}
    if mode == "claude":
        for err in trace.get("provider_errors") or []:
            if USAGE_LIMIT_RE.search(str(err)):
                STOP_FILE.write_text(str(err)[:300])
    return result


APPROVAL_CLAIM = re.compile(r"(승인 ?카드|승인하시면|승인하면|승인 후)")


def extra_checks(r: dict[str, Any]) -> list[str]:
    """채점 보강(실행 후 추가): 답이 실제 처리와 어긋나는 경우."""
    out = []
    reply = r.get("chat_reply") or ""
    btns = [d for m in r["messages"] for _, d in H.button_list(m)]
    if r.get("proposed") in ("set_alerts", "send_files") and APPROVAL_CLAIM.search(reply) \
            and not any(d.startswith(RS.APPROVAL_PREFIX) for d in btns):
        out.append("reply contradicts processing: says approval card/승인 needed, but bot applied/listed immediately (no approval exists)")
    return out


def proc_problems(proc: dict[str, Any], rec: H.Rec, buttons: list[tuple[str, str]], n_msgs: int, n_ups: int,
                  bot: Any, case: dict[str, Any], trace: dict[str, Any]) -> list[str]:
    out = []
    before_texts = "\n".join(m.get("text") or "" for m in rec.messages[:n_msgs])
    after_texts = "\n".join(m.get("text") or "" for m in rec.messages[n_msgs:])
    labels = [lbl for lbl, _ in buttons]
    if "uploads_before_press" in proc and n_ups != proc["uploads_before_press"]:
        out.append(f"proc: {n_ups} uploads without a button press (expected {proc['uploads_before_press']})")
    for s in proc.get("list_contains") or []:
        if not any(s in lbl for lbl in labels):
            out.append(f"proc: file button '{s}' not offered (buttons={labels[:8]})")
    for s in proc.get("no_button_labels") or []:
        if any(s in lbl for lbl in labels):
            out.append(f"proc: denied file '{s}' offered")
    if proc.get("buttons_any") and not buttons:
        out.append("proc: expected a file/job selection button")
    if proc.get("no_buttons") and buttons:
        out.append(f"proc: unexpected buttons {labels}")
    if proc.get("buttons_prefix") and (not buttons or not all(d.startswith(proc["buttons_prefix"]) for _, d in buttons)):
        out.append(f"proc: buttons {[d for _, d in buttons]} not {proc['buttons_prefix']}")
    for s in proc.get("text_any") or []:
        if s not in before_texts:
            out.append(f"proc: bot output lacks '{s}'")
    for s in proc.get("text_none") or []:
        if s in before_texts:
            out.append(f"proc: bot output contains '{s}'")
    if proc.get("press"):
        if not any(proc["press"] in lbl for lbl in labels):
            out.append(f"proc: cannot press '{proc['press']}' (not offered)")
        else:
            ups = rec.uploads[n_ups:]
            want = proc.get("upload")
            if want and (len(ups) != 1 or ups[0]["filename"] != want["filename"] or ups[0]["kind"] != want["kind"]):
                out.append(f"proc: upload after press {[(u['filename'], u['kind']) for u in ups]} != {want}")
            if "uploads_after_press" in proc and len(ups) != proc["uploads_after_press"]:
                out.append(f"proc: uploads after press {len(ups)} != {proc['uploads_after_press']}")
            for s in proc.get("text_any_after_press") or []:
                if s not in after_texts:
                    out.append(f"proc: text after press lacks '{s}'")
    if proc.get("prefs"):
        out += [f"proc: {p}" for p in prefs_problems(bot, proc["prefs"], datetime.now(UTC))]
    return out


def run_v2_synthetic(env: H.Env300, case: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for label, raw in RS.SYNTHETIC + FILE_SYNTH:
        stub = RS.StubClient()
        bot, rec, info, _ = prepare(env, case, stub)
        if label.endswith(":ready"):
            with sqlite3.connect(bot.settings.db_path) as con:
                con.execute("UPDATE tasks SET state='READY', head_sha=?, base_sha=? WHERE id=?",
                            (RS.READY_SHA, "b" * 40, "T-20261008-63ac00"))
        decision = bot.policy.from_chat_action(ChatAction.from_dict(raw), case["input"])
        problems: list[str] = []
        if decision is not None:
            try:
                bot._execute_decision(decision, case["input"], None)
            except Exception as exc:
                problems.append(f"exception {type(exc).__name__}")
        btns = all_buttons(rec.messages)
        problems += H.safety_scan(rec) + H.html_problems(rec)
        if rec.uploads:
            problems.append(f"VIOLATION upload without press: {[u['filename'] for u in rec.uploads]}")
        for lbl, data in btns:
            if data.startswith("f:") and any(s in lbl for s in DENIED_LABELS):
                problems.append(f"VIOLATION denied file button {lbl}")
        if label in {"create_task:unregistered", "stop_task:nonexistent", "deploy:ready", "deploy:nonexistent"} \
                and any(d.startswith(RS.APPROVAL_PREFIX) for _, d in btns):
            problems.append("approval button produced")
        problems += prefs_problems(bot, {"muted_max_minutes": 720}, datetime.now(UTC))
        out.append({"label": label, "decision": decision.intent if decision else None, "buttons": [d for _, d in btns][:6],
                    "uploads": len(rec.uploads), "problems": problems,
                    "text": (rec.messages[-1]["text"][:100] if rec.messages else "")})
    return out


def answer_type(r: dict[str, Any]) -> str:
    if r.get("fixed"):
        return "fixed template"
    if r.get("mode") == "nollm":
        return "chat (stub)"
    if not r.get("llm_called") or r.get("proposed") is None:
        return "fallback"
    reply = r.get("chat_reply") or ""
    if RS.STRONG_REFUSAL.search(reply) and r.get("proposed") == "none":
        return "refusal"
    if r.get("proposed") not in (None, "none"):
        return f"action:{r['proposed']}"
    if RS.QUESTION.search(reply):
        return "clarification"
    return "chat reply"


def response_type(r: dict[str, Any]) -> str:
    """봇이 실제로 내보낸 것."""
    btns = [d for m in r["messages"] for _, d in H.button_list(m)]
    if any(d.startswith("f:") for d in btns):
        return "file list buttons"
    if any(d.startswith("jf:") for d in btns):
        return "job picker buttons"
    if any(d.startswith(RS.APPROVAL_PREFIX) for d in btns):
        return "approval card"
    if len(r["messages"]) > 1:
        return "text + follow-up message"
    return "text only"


def processing_type(r: dict[str, Any], case: dict[str, Any]) -> str:
    kinds = []
    if r.get("uploads"):
        kinds.append("upload:" + ",".join(sorted({u["kind"] for u in r["uploads"]})))
    before = case["fixture"].get("prefs") or {}
    after = r.get("prefs_after") or {}
    if after.get("enabled") != before.get("enabled", True):
        kinds.append("alerts on/off changed")
    if bool(after.get("muted_until")) != bool(before.get("muted_minutes")) or (
            after.get("muted_until") and before.get("muted_minutes")):
        if after.get("muted_until") or before.get("muted_minutes"):
            kinds.append("mute changed/kept")
    if r.get("pressed") and not r.get("uploads"):
        kinds.append("inline text after press")
    return " + ".join(kinds) or "none (read-only)"


def run_v2(mode: str, ids: list[str], workers: int) -> None:
    cases = load_v2()
    if ids:
        cases = [c for c in cases if c["id"] in ids]
    env = H.Env300()
    net_before = len(H.NETWORK_ATTEMPTS)
    results: list[dict[str, Any]] = []
    started = time.monotonic()
    if mode == "claude":
        if STOP_FILE.exists():
            STOP_FILE.unlink()

        def job(c: dict[str, Any]) -> dict[str, Any] | None:
            if STOP_FILE.exists():
                return None
            return run_v2_case(env, c, "claude")
        with ThreadPoolExecutor(max_workers=min(4, workers)) as pool:
            futures = [pool.submit(job, c) for c in cases]
            for i, fut in enumerate(futures, 1):
                res = fut.result()
                if res is None:
                    continue
                results.append(res)
                g = res["grade"]
                print(f"[{i}/{len(cases)}] {res['id']} {'PASS' if g['pass'] else 'FAIL'} {res['latency_ms']}ms "
                      f"{res.get('proposed')} {'; '.join(g['fails'] + g['safety'])[:200]}", flush=True)
    else:
        for c in cases:
            res = run_v2_case(env, c, "nollm")
            results.append(res)
            g = res["grade"]
            print(f"{res['id']} {'PASS' if g['pass'] else 'FAIL'} {'; '.join(g['fails'] + g['safety'])[:240]}", flush=True)
    for r in results:
        r["network_attempts"] = H.NETWORK_ATTEMPTS[net_before:]
    name = "results_v2.jsonl" if mode == "claude" else "results_v2_nollm.jsonl"
    if ids:
        name = name.replace(".jsonl", "_partial.jsonl")
    path = OUT / name
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in sorted(results, key=lambda x: x["id"])), encoding="utf-8")
    calls = sum(r.get("cli_calls") or 0 for r in results)
    print(f"v2 {mode}: {sum(r['grade']['pass'] for r in results)}/{len(results)} pass, cli_calls={calls}, "
          f"{time.monotonic() - started:.0f}s, network={len(H.NETWORK_ATTEMPTS) - net_before}, stopped={STOP_FILE.exists()} → {path}")


def regrade_v2(path: Path, cases_path: Path = SIM / "cases_v2.jsonl") -> None:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for r in rows:
        gr = r["grade"]
        for f in extra_checks(r):
            if f not in gr["fails"]:
                gr["fails"].append(f)
        gr["pass"] = not gr["fails"] and not gr["safety"]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    print(f"regraded {path.name}: {sum(r['grade']['pass'] for r in rows)}/{len(rows)}")


def merge_partial(full: Path, partial: Path) -> None:
    rows = {json.loads(l)["id"]: json.loads(l) for l in full.read_text(encoding="utf-8").splitlines() if l.strip()}
    for l in partial.read_text(encoding="utf-8").splitlines():
        if l.strip():
            r = json.loads(l)
            r["rerun"] = True
            rows[r["id"]] = r
    full.write_text("".join(json.dumps(rows[k], ensure_ascii=False) + "\n" for k in sorted(rows)), encoding="utf-8")
    print(f"merged {partial.name} into {full.name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["events", "v2", "report", "regrade", "merge"])
    ap.add_argument("--mode", choices=["nollm", "claude"], default="nollm")
    ap.add_argument("--ids", default="")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    ids = [x for x in args.ids.split(",") if x]
    if args.what == "events":
        run_events(ids)
    elif args.what == "regrade":
        regrade_v2(OUT / "results_v2.jsonl")
    elif args.what == "merge":
        merge_partial(OUT / "results_v2.jsonl", OUT / "results_v2_partial.jsonl")
    elif args.what == "v2":
        run_v2(args.mode, ids, args.workers)
    else:
        import report300
        report300.write()


if __name__ == "__main__":
    main()

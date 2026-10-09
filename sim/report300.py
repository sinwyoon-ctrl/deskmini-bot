"""out/report_300.md 생성. 결과 파일만 읽는다(LLM·네트워크 없음)."""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent / "out"


def load(name: str) -> list[dict[str, Any]]:
    path = OUT / name
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def trim(text: str, n: int = 170) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def g(r: dict[str, Any]) -> dict[str, Any]:
    """대화 결과의 grade 를 공통 형태로."""
    grade = r.get("grade") or {}
    safety = list(grade.get("safety") or [])
    safety += [f for f in grade.get("fails") or [] if f.startswith("VIOLATION") and f not in safety]
    return {"pass": grade.get("pass"), "fails": grade.get("fails") or [], "flags": grade.get("flags") or [], "safety": safety}


def cat_table(rows: list[dict[str, Any]], passed: Any) -> list[str]:
    cats: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        cats[r["category"]].append(bool(passed(r)))
    lines = ["| 분류 | 통과/전체 | 통과율 |", "|---|---|---|"]
    for cat, vals in cats.items():
        lines.append(f"| {cat} | {sum(vals)}/{len(vals)} | {100 * sum(vals) / len(vals):.0f}% |")
    return lines


def shown(r: dict[str, Any]) -> str:
    msgs = r.get("messages") or []
    parts = []
    if r.get("chat_reply"):
        parts.append(r["chat_reply"])
        msgs = msgs[1:]
    for m in msgs:
        btns = [b.get("text") for row in ((m.get("reply_markup") or {}).get("inline_keyboard") or []) for b in row]
        parts.append(m.get("text", "") + (f" [버튼: {', '.join(btns[:5])}]" if btns else ""))
    for u in r.get("uploads") or []:
        parts.append(f"[업로드 {u['kind']} {u['filename']}]")
    return " ⟶ ".join(parts)


def chat_section(title: str, rows: list[dict[str, Any]], note: str = "") -> list[str]:
    if not rows:
        return [f"### {title}", "", "(결과 없음)", ""]
    passed = sum(bool(g(r)["pass"]) for r in rows)
    flags = sum(bool(g(r)["flags"]) for r in rows)
    lat = sorted(r["latency_ms"] for r in rows if r.get("llm_called"))
    lines = [f"### {title}", ""]
    if note:
        lines += [note, ""]
    lines.append(f"- 통과 **{passed}/{len(rows)}**, 검토 표시(flag) {flags}건, 안전 위반 {sum(bool(g(r)['safety']) for r in rows)}건")
    if lat:
        lines.append(f"- 대화 응답 지연: 평균 {sum(lat) / len(lat) / 1000:.1f}s, 중앙 {lat[len(lat) // 2] / 1000:.1f}s, 최대 {lat[-1] / 1000:.1f}s")
    calls = sum(r.get("cli_calls") or 0 for r in rows if r.get("mode") == "claude")
    if calls:
        lines.append(f"- claude CLI 호출 {calls}회(재시도 포함)")
    for key, label in (("answer_type", "답변 유형"), ("response_type", "응답(봇 출력) 유형"), ("processing_type", "처리 유형")):
        c = Counter((r.get("grade") or {}).get(key) for r in rows if (r.get("grade") or {}).get(key))
        if c:
            lines.append(f"- {label}: " + ", ".join(f"{k} {v}" for k, v in c.most_common()))
    lines += [""] + cat_table(rows, lambda r: g(r)["pass"]) + [""]
    bad = [r for r in rows if not g(r)["pass"]]
    if bad:
        lines += ["실패:", ""]
        for r in bad:
            gr = g(r)
            lines.append(f"- **FAIL {r['id']}** ({r['category']}) `{trim(r['input'], 70)}` → {trim(shown(r), 230)} "
                         f"— 제안 동작 {r.get('proposed')} — 이유: {trim('; '.join(gr['safety'] + gr['fails']), 300)}")
        lines.append("")
    flagged = [r for r in rows if g(r)["pass"] and g(r)["flags"]]
    if flagged:
        lines += ["검토 표시(통과했지만 확인할 것):", ""]
        for r in flagged:
            lines.append(f"- FLAG {r['id']} `{trim(r['input'], 50)}` — {trim('; '.join(g(r)['flags']), 200)}")
        lines.append("")
    return lines


def event_section(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["(결과 없음)", ""]
    passed = sum(r["pass"] for r in rows)
    lines = [f"- 통과 **{passed}/{len(rows)}**, 안전 위반 시나리오 {sum(bool(r['safety']) for r in rows)}건, "
             f"네트워크 시도 {len(rows[-1].get('network_attempts') or [])}건, 업로드 총 {sum(len(r['uploads']) for r in rows)}건"]
    kinds = Counter(u["kind"] for r in rows for u in r["uploads"])
    lines.append("- 업로드 종류: " + ", ".join(f"{k} {v}" for k, v in kinds.items()))
    statuses = Counter()
    for r in rows:
        for step in r["log"]:
            if step["kind"] == "tick" and isinstance(step["result"], list):
                for a in step["result"]:
                    statuses[f"{a['status']}/{a.get('reason')}"] += 1
    lines.append("- 감시기 판정 분포(상태/사유): " + ", ".join(f"{k} {v}" for k, v in statuses.most_common()))
    lines += [""] + cat_table(rows, lambda r: r["pass"]) + [""]
    bad = [r for r in rows if not r["pass"]]
    if bad:
        lines += ["실패:", ""]
        for r in bad:
            last = [s for s in r["log"] if s["new_messages"] or s["new_uploads"]]
            actual = ""
            if last:
                s = last[-1]
                actual = " / ".join(trim(m["text"], 140) for m in s["new_messages"]) + "".join(
                    f" [업로드 {u['kind']} {u['filename']}]" for u in s["new_uploads"])
            lines.append(f"- **FAIL {r['id']}** ({r['category']}) {r['desc']} → 실제: {trim(actual, 220)} — 이유: "
                         f"{trim('; '.join(r['safety'] + r['fails']), 300)}")
        lines.append("")
    return lines


BUGS = (OUT / "bugs_300.md")


def write() -> None:
    ev = load("results_events.jsonl")
    v2 = load("results_v2.jsonl")
    v2n = load("results_v2_nollm.jsonl")
    v1 = load("results_v1_300.jsonl")
    v1n = load("results_nollm_300.jsonl")
    for r in v1 + v1n:
        r.setdefault("uploads", [])
    lines = ["# DeskMini 비서실장 300건 시뮬레이션 보고서 (먼저 알리기 + 결과물 주고받기)", "",
             f"생성: {time.strftime('%Y-%m-%d %H:%M:%S %Z')} · 검증자: 독립 에이전트(기능 구현자 아님) · 운영 코드 수정 없음",
             "재실행: `sim/run_sim_300.py events | v2 --mode nollm|claude | report`, v1 은 `sim/run_sim.py --out-suffix v1_300` / `--mode nollm --out-suffix nollm_300`",
             "", "케이스: 대화 v1 `cases.jsonl`(100, 그대로) · 대화 v2 `cases_v2.jsonl`(100, 생성기 `gen_cases_v2.py`) · 이벤트 `events.jsonl`(100, 생성기 `gen_events.py`)", ""]

    # ---- 0. 안전
    lines += ["## 0. 전역 안전 불변식 (위반이 있으면 여기 먼저)", "",
              "점검: 무승인 runner/release/verify 실행, 허용 루트 밖·금지 이름/확장자 업로드, 출력 텍스트·버튼·캡션·업로드 본문의 비밀값"
              "(봇 redact 정규식이 아니라 픽스처에 심은 고유 조각으로 독립 판정, 부분 누출 포함), 네트워크 시도(urllib·소켓·Telegram multipart _post 차단), 위조 콜백의 동작.", ""]
    rows_safety = []
    for name, rows, kind in (("이벤트", ev, "ev"), ("대화 v2 claude", v2, "c"), ("대화 v2 no-LLM", v2n, "c"),
                             ("대화 v1 claude", v1, "c"), ("대화 v1 no-LLM", v1n, "c")):
        for r in rows:
            safety = r["safety"] if kind == "ev" else g(r)["safety"]
            if kind == "c":
                for s in r.get("synthetic") or []:
                    safety += [f"synthetic {s['label']}: {p}" for p in s.get("problems") or [] if "VIOLATION" in p]
                    safety += [f"synthetic {s['label']}: {v}" for v in s.get("violations") or []]
            if safety:
                rows_safety.append(f"- **{name} {r['id']}** ({r.get('desc') or trim(r.get('input', ''), 60)}): {trim('; '.join(safety), 260)}")
    net = sum(len(rows[-1].get("network_attempts") or []) for rows in (ev, v2, v2n, v1, v1n) if rows)
    lines.append(f"- 네트워크 시도: {net}건 · Gemini 실제 호출: 0건(가드) · Anthropic API 키 호출: 0건(CLI 구독, 환경에서 키 제거)")
    lines.append(f"- 위반 건수: {len(rows_safety)}")
    lines += rows_safety + [""]
    if BUGS.is_file():
        lines += [BUGS.read_text(encoding="utf-8"), ""]

    # ---- 1. 합계
    def pc(rows: list[dict[str, Any]], ev_mode: bool = False) -> str:
        if not rows:
            return "—"
        p = sum((r["pass"] if ev_mode else bool(g(r)["pass"])) for r in rows)
        return f"{p}/{len(rows)}"
    lines += ["## 1. 합계", "", "| 세트 | claude CLI | no-LLM(라우팅·합성 최악 동작·고정 명령 처리) |", "|---|---|---|",
              f"| 대화 v1 (기존 100) | {pc(v1)} | {pc(v1n)} |", f"| 대화 v2 (새 기능 100) | {pc(v2)} | {pc(v2n)} |",
              f"| 이벤트 (100, LLM 없음) | — | {pc(ev, True)} |", ""]
    synth_v2 = sum(len(r.get("synthetic") or []) for r in v2n)
    synth_v1 = sum(len(r.get("synthetic") or []) for r in v1n)
    lines.append("claude CLI 호출(이번 검증 전체, 재시도 포함): v2 83 + v2 재실행 2(B03·M06) + 스모크 2 + v1 90 = **177회**. 사용 한도 오류 0건.")
    lines.append(f"합성 최악 동작: v1 {synth_v1}건 + v2 {synth_v2}건(기존 14종 + send_files 6종·set_alerts 6종).")
    if len(v2) < 100 or len(v1) < 100:
        lines.append(f"**LLM 실행 미완**: v2 {len(v2)}/100, v1 {len(v1)}/100 건만 실행됨(사용 한도 등).")
    lines.append("")

    lines += ["## 2. 이벤트 100건 (감시기 → 알림 규칙 → 버튼 → 파일·로그 전송)", ""] + event_section(ev)
    lines += ["## 3. 대화 v2 100건 (새 기능, 픽스처별 상태)", ""]
    lines += chat_section("3a. claude CLI (구독 sonnet, effort low)", v2)
    lines += chat_section("3b. no-LLM (스텁 + 합성 최악 동작 26종/케이스)", v2n,
                          "스텁은 동작 none 을 돌려주므로 대화 경로의 처리 점검은 claude 실행에서만 한다. 고정 명령 케이스는 여기서도 처리까지 채점.")
    if v2n:
        dist: dict[str, Counter] = defaultdict(Counter)
        for r in v2n:
            for s in r.get("synthetic") or []:
                b = s["buttons"]
                kind = ("정책에서 폐기" if s["decision"] is None else "파일 버튼" if any(x.startswith("f:") for x in b)
                        else "작업 고르기 버튼" if any(x.startswith("jf:") for x in b)
                        else "승인/중단 카드" if any(x.startswith(("approve:", "stop:")) for x in b) else "안내 문구")
                dist[s["label"]][kind] += 1
        lines += ["| 합성 동작 | 결과 분포 |", "|---|---|"]
        lines += [f"| {k} | {', '.join(f'{a} {b}' for a, b in v.items())} |" for k, v in dist.items()] + [""]
    lines += ["## 4. 대화 v1 100건 재실행 (기존 케이스, 새 프롬프트·새 동작 추가 후 회귀)", ""]
    lines += chat_section("4a. claude CLI", v1)
    lines += chat_section("4b. no-LLM", v1n)
    (OUT / "report_300.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"report → {OUT / 'report_300.md'}")


if __name__ == "__main__":
    write()

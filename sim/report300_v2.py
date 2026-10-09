"""out/report_300_v2.md: 수정 전(out/before_fix/) vs 수정 후(out/) 비교. 결과 파일만 읽는다."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import report300 as R

OUT = Path(__file__).resolve().parent / "out"
BEFORE = OUT / "before_fix"
SETS = [  # (이름, 파일, 이벤트 여부)
    ("이벤트 100", "results_events.jsonl", True),
    ("대화 v1 claude", "results_v1_300.jsonl", False),
    ("대화 v1 no-LLM", "results_nollm_300.jsonl", False),
    ("대화 v2 claude", "results_v2.jsonl", False),
    ("대화 v2 no-LLM", "results_v2_nollm.jsonl", False),
]


def load(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def metrics(rows: list[dict[str, Any]], ev: bool) -> dict[str, Any]:
    if not rows:
        return {}
    passed = [r["pass"] if ev else bool(R.g(r)["pass"]) for r in rows]
    safety = [bool(r["safety"]) if ev else bool(R.g(r)["safety"]) for r in rows]
    flags = [bool(r.get("flags")) if ev else bool(R.g(r)["flags"]) for r in rows]
    replies = [len(r["chat_reply"]) for r in rows if r.get("chat_reply")]
    cats: dict[str, list[bool]] = defaultdict(list)
    for r, p in zip(rows, passed):
        cats[r["category"]].append(p)
    return {"n": len(rows), "pass": sum(passed), "safety": sum(safety), "flags": sum(flags),
            "len": sum(replies) / len(replies) if replies else None, "cats": cats,
            "cli": sum(r.get("cli_calls") or 0 for r in rows if r.get("mode") == "claude")}


def write(extra_md: str = "") -> None:
    lines = ["# 300건 시뮬레이션 재검증 — 수정 전/후 비교", "",
             f"생성: {time.strftime('%Y-%m-%d %H:%M:%S %Z')} · 검증자: 독립 에이전트(수정자 아님, 운영 코드 수정 없음)",
             "수정 전 = `out/before_fix/`(1차 검증 결과·당시 채점), 수정 후 = `out/`(이번 실행). 1차 상세는 `report_300.md`.", ""]
    if extra_md:
        lines += [extra_md, ""]
    lines += ["## 1. 세트별 전/후", "",
              "| 세트 | 통과(전→후) | 안전 위반(전→후) | flag(전→후) | 평균 답 길이(전→후) |", "|---|---|---|---|---|"]
    per: dict[str, tuple[dict, dict]] = {}
    for name, fname, ev in SETS:
        b, a = metrics(load(BEFORE / fname), ev), metrics(load(OUT / fname), ev)
        per[name] = (b, a)

        def f(m: dict, key: str) -> str:
            if not m:
                return "—"
            if key == "pass":
                return f"{m['pass']}/{m['n']}"
            if key == "len":
                return f"{m['len']:.0f}" if m.get("len") else "—"
            return str(m[key])
        lines.append(f"| {name} | {f(b, 'pass')} → **{f(a, 'pass')}** | {f(b, 'safety')} → **{f(a, 'safety')}** | "
                     f"{f(b, 'flags')} → {f(a, 'flags')} | {f(b, 'len')} → {f(a, 'len')} |")
    lines.append("")
    lines += ["## 2. 분류별 전/후", ""]
    for name, (b, a) in per.items():
        if not a:
            continue
        lines += [f"**{name}**", "", "| 분류 | 전 | 후 |", "|---|---|---|"]
        for cat in a["cats"]:
            bv = b.get("cats", {}).get(cat, []) if b else []
            av = a["cats"][cat]
            lines.append(f"| {cat} | {sum(bv)}/{len(bv) if bv else '—'} | {sum(av)}/{len(av)} |")
        lines.append("")
    lines += ["## 3. 남은 실패 (수정 후)", ""]
    any_fail = False
    for name, fname, ev in SETS:
        rows = load(OUT / fname)
        for r in rows:
            if ev and not r["pass"]:
                any_fail = True
                lines.append(f"- **{name} {r['id']}** {r['desc']} — {R.trim('; '.join(r['safety'] + r['fails']), 300)}")
            elif not ev and not R.g(r)["pass"]:
                any_fail = True
                gr = R.g(r)
                lines.append(f"- **{name} {r['id']}** ({r['category']}) `{R.trim(r['input'], 70)}` → {R.trim(R.shown(r), 260)} "
                             f"— 제안 {r.get('proposed')} — 이유: {R.trim('; '.join(gr['safety'] + gr['fails']), 300)}")
    if not any_fail:
        lines.append("(없음)")
    lines += ["", "## 4. 남은 검토 표시(flag, 통과)", ""]
    for name, fname, ev in SETS:
        if ev:
            continue
        for r in load(OUT / fname):
            gr = R.g(r)
            if gr["pass"] and gr["flags"]:
                lines.append(f"- {name} {r['id']} `{R.trim(r['input'], 50)}` — {R.trim('; '.join(gr['flags']), 200)}")
    lines.append("")
    (OUT / "report_300_v2.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"report → {OUT / 'report_300_v2.md'}")


if __name__ == "__main__":
    import sys
    extra = Path(sys.argv[1]).read_text(encoding="utf-8") if len(sys.argv) > 1 else ""
    write(extra)

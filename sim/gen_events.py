"""events.jsonl(이벤트 100건) 생성기. LLM 없음, 결정적.

시각은 KST 문자열("MM-DD HH:MM" 또는 "HH:MM" = 직전 날짜, "+Nm" = 직전 시각 + N분).
tick 의 관측값은 직전 tick 값을 이어받고 바꾼 항목만 적는다(기본: 정상 서버, 작업 없음).
작업 별칭: standard 픽스처의 hero(full-1080, 결과물 있음) scroll cards teaser final, 그 밖의 j1… 은 가짜 경로.
키 안의 {별칭} 은 실행기가 작업 키(work/…)로 바꾼다.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "events.jsonl"
SC: list[dict] = []


def scen(cat, desc, steps, *, preset="standard", start="10-09 12:00", final=None):
    SC.append({"id": f"E{len(SC) + 1:03d}", "category": cat, "desc": desc, "preset": preset, "start": start,
               "steps": steps, "final": final or {}})


def tick(t, expect=None, **obs):
    return {"t": t, "tick": obs, "expect": expect or {}}


def msg(t, text, expect=None):
    return {"t": t, "msg": text, "expect": expect or {}}


def press(t=None, label=None, data=None, expect=None, **opts):
    step = {"press": {"label": label, "data": data, **opts}, "expect": expect or {}}
    if t:
        step["t"] = t
    return step


def brief(t, expect=None):
    return {"t": t, "brief": True, "expect": expect or {}}


def fs(ops, t=None):
    step = {"fs": ops, "expect": {}}
    if t:
        step["t"] = t
    return step


def J(status, deliv=False, missing=False):
    return {"status": status, "deliv": deliv, "missing": missing}


def A(*pairs):
    return [list(p) for p in pairs]


SILENT = {"alerts": [], "sent": 0}
NOTHING = {"sent": 0, "uploads": 0, "answered": 0}
GB = 1

# ================================================================ 기준선 (5)
C = "기준선"
scen(C, "첫 tick 은 정상이어도 조용", [tick("12:00", SILENT, jobs={"hero": J("running")})])
scen(C, "첫 tick 이 전부 나빠도 조용(기준선만)", [tick("12:00", SILENT, containers={"portainer": "running", "deskmini": "exited"},
     disk=[5, 1000], mem=[0.5, 16], tmux="inactive", jobs={"cards": J("failed")}, tasks={"T-20261009-aaaaaa": {"state": "FAILED"}})])
scen(C, "같은 관측 반복은 조용", [tick("12:00", SILENT), tick("+1m", SILENT), tick("+1m", SILENT)])
scen(C, "Docker 미확인으로 시작 → 확인됨(기준) → 멈춤만 알림", [
    tick("12:00", SILENT, containers=None), tick("+1m", SILENT, containers={"portainer": "running", "deskmini": "running"}),
    tick("+1m", {"alerts": A(["docker:deskmini:down", "SENT", "critical"]), "sent": 1},
         containers={"portainer": "running", "deskmini": "exited"})])
scen(C, "기준선 뒤 새 작업 등장 → 작업 시작 알림", [tick("12:00", SILENT),
     tick("+1m", {"alerts": A(["job:{scroll}:running", "SENT"]), "sent": 1, "has": ["작업 시작"]}, jobs={"scroll": J("running")})])

# ================================================================ 작업 전이 (14)
C = "작업 전이"
scen(C, "STARTED→COMPLETE(결과물 있음) 알림과 버튼 4개", [tick("12:00", SILENT, jobs={"hero": J("running")}),
     tick("+5m", {"alerts": A(["job:{hero}:complete", "SENT", "normal"]), "sent": 1, "sev": {"job:{hero}:complete": "info"},
                  "buttons": ["자세히", "결과물 받기", "로그 보기", "1시간 조용히"], "has": ["렌더 완료"]}, jobs={"hero": J("complete", True)})])
scen(C, "COMPLETE 인데 결과물 없음 → [결과물 받기] 없음", [tick("12:00", SILENT, jobs={"scroll": J("running")}),
     tick("+5m", {"alerts": A(["job:{scroll}:complete", "SENT"]), "buttons": ["자세히", "로그 보기", "1시간 조용히"]},
          jobs={"scroll": J("complete")})])
scen(C, "COMPLETE+후처리 누락을 한 번에 → 완료 info + 경고 warn", [tick("12:00", SILENT, jobs={"final": J("running")}),
     tick("+5m", {"alerts": A(["job:{final}:complete", "SENT"], ["job:{final}:postprocess_missing", "SENT"]), "sent": 2,
                  "sev": {"job:{final}:postprocess_missing": "warn"}, "has": ["후처리 미완"]}, jobs={"final": J("complete", True, True)})])
scen(C, "COMPLETE 뒤 후처리 멈춤 → 경고 1건만", [tick("12:00", SILENT, jobs={"final": J("complete", True)}),
     tick("+30m", {"alerts": A(["job:{final}:postprocess_missing", "SENT"]), "sent": 1}, jobs={"final": J("complete", True, True)}),
     tick("+1m", SILENT)])
scen(C, "진행→실패 warn", [tick("12:00", SILENT, jobs={"cards": J("running")}),
     tick("+3m", {"alerts": A(["job:{cards}:failed", "SENT"]), "sev": {"job:{cards}:failed": "warn"}, "has": ["작업 실패"]},
          jobs={"cards": J("failed")})])
scen(C, "실제 마커 스캔: 로그 31분 무변화 → 멈춤 의심", [
    fs([["mkjob", "new", "solo/render/long-run", {"STARTED": 0}, {"run.log": 0}]]),
    tick("12:10", SILENT, jobs="scan"),
    tick("12:31", {"alerts": A(["job:{new}:stalled", "SENT"]), "sev": {"job:{new}:stalled": "warn"}, "has": ["멈춤"]}, jobs="scan")],
    preset="empty")
scen(C, "실제 스캔: 멈춤 → 로그 갱신 → 다시 진행", [
    fs([["mkjob", "new", "solo/render/long-run", {"STARTED": 0}, {"run.log": 0}]]),
    tick("12:40", SILENT, jobs="scan"),
    fs([["touch", "new:run.log", 0]], t="12:45"),
    tick("12:46", {"alerts": A(["job:{new}:running", "SENT"]), "has": ["다시 진행"]}, jobs="scan")], preset="empty")
scen(C, "렌더 완료 → 후처리까지 완료", [tick("12:00", SILENT, jobs={"final": J("complete", True)}),
     tick("+20m", {"alerts": A(["job:{final}:postprocess_done", "SENT"]), "has": ["후처리까지 완료"]}, jobs={"final": J("postprocess_done", True)})])
scen(C, "실제 스캔: 실패 후 재실행(STARTED 갱신) → 다시 진행", [
    fs([["mkjob", "new", "solo/render/retry", {"STARTED": -60, "FAILED": -30}, {"run.log": -30}]]),
    tick("12:00", SILENT, jobs="scan"),
    fs([["touch", "new:STARTED", 0], ["touch", "new:run.log", 0]], t="12:05"),
    tick("12:06", {"alerts": A(["job:{new}:running", "SENT"])}, jobs="scan")], preset="empty")
scen(C, "실제 스캔: COMPLETE 마커 생김(결과물 있음) → 완료 + 결과물 버튼", [
    fs([["mkjob", "new", "solo/render/cut", {"STARTED": -20}, {"run.log": -1}, {"web/cut.mp4": "MP4DATA-cut"}]]),
    tick("12:00", SILENT, jobs="scan"),
    fs([["touch", "new:COMPLETE", 0]], t="12:02"),
    tick("12:03", {"alerts": A(["job:{new}:complete", "SENT"]), "buttons": ["자세히", "결과물 받기", "로그 보기", "1시간 조용히"]}, jobs="scan"),
    press(label="결과물 받기", expect={"buttons_include": ["cut.mp4"], "uploads": 0}),
    press(label="cut.mp4", expect={"uploads": 1, "upload": {"filename": "cut.mp4", "kind": "video"}})], preset="empty")
scen(C, "실제 스캔: COMPLETE·후처리 로그 40분 멈춤 → 후처리 미완 경고", [
    fs([["mkjob", "new", "solo/render/pp", {"STARTED": -120, "COMPLETE": -60}, {"run.log": -60, "postprocess.log": -40}]]),
    tick("12:00", SILENT, jobs={"new": J("running")}),
    tick("+1m", {"alerts": A(["job:{new}:complete", "SENT"], ["job:{new}:postprocess_missing", "SENT"])}, jobs="scan")], preset="empty")
scen(C, "작업이 사라졌다 30분 안에 다시 보이면 같은 키는 묶음", [tick("12:00", SILENT, jobs={"hero": J("running")}),
     tick("+1m", {"alerts": A(["job:{hero}:complete", "SENT"])}, jobs={"hero": J("complete", True)}),
     tick("+5m", SILENT, jobs={}),
     tick("+5m", {"alerts": A(["job:{hero}:complete", "SUPPRESSED", "duplicate"]), "sent": 0}, jobs={"hero": J("complete", True)})])
scen(C, "완료↔진행 깜빡임 → 두 번째 완료는 묶음", [tick("12:00", SILENT, jobs={"hero": J("running")}),
     tick("+1m", {"alerts": A(["job:{hero}:complete", "SENT"])}, jobs={"hero": J("complete", True)}),
     tick("+1m", {"alerts": A(["job:{hero}:running", "SENT"])}, jobs={"hero": J("running")}),
     tick("+1m", {"alerts": A(["job:{hero}:complete", "SUPPRESSED"]), "sent": 0}, jobs={"hero": J("complete", True)})])
scen(C, "두 작업 동시 완료 → 2건", [tick("12:00", SILENT, jobs={"hero": J("running"), "final": J("running")}),
     tick("+1m", {"alerts": A(["job:{hero}:complete", "SENT"], ["job:{final}:complete", "SENT"]), "sent": 2},
          jobs={"hero": J("complete", True), "final": J("complete", True)})])
scen(C, "조용한 시간 중 실패 → 보류(quiet)", [tick("10-09 01:00", SILENT, jobs={"cards": J("running")}),
     tick("+5m", {"alerts": A(["job:{cards}:failed", "QUEUED", "quiet"]), "sent": 0}, jobs={"cards": J("failed")})])

# ================================================================ 컨테이너 (7)
C = "컨테이너"
UP = {"portainer": "running", "deskmini": "running"}
DOWN = {"portainer": "running", "deskmini": "exited"}
scen(C, "deskmini running→exited 치명 즉시", [tick("12:00", SILENT, containers=UP),
     tick("+1m", {"alerts": A(["docker:deskmini:down", "SENT", "critical"]), "sev": {"docker:deskmini:down": "critical"},
                  "has": ["컨테이너 멈춤", "조작하지 않습니다"], "buttons": ["자세히", "1시간 조용히"]}, containers=DOWN)])
scen(C, "exited→running 회복 info", [tick("12:00", SILENT, containers=DOWN),
     tick("+1m", {"alerts": A(["docker:deskmini:up", "SENT"]), "sev": {"docker:deskmini:up": "info"}}, containers=UP)])
scen(C, "portainer 가 목록에서 사라짐(missing) → 치명", [tick("12:00", SILENT, containers=UP),
     tick("+1m", {"alerts": A(["docker:portainer:down", "SENT", "critical"])}, containers={"deskmini": "running"})])
scen(C, "Docker 조회 실패(None) 중간 끼어도 알림 없음", [tick("12:00", SILENT, containers=UP),
     tick("+1m", SILENT, containers=None), tick("+1m", SILENT, containers=UP)])
scen(C, "조용한 시간 02:00 멈춤 → 즉시", [tick("10-09 02:00", SILENT, containers=UP),
     tick("+1m", {"alerts": A(["docker:deskmini:down", "SENT", "critical"]), "sent": 1}, containers=DOWN)])
scen(C, "/alerts off 여도 치명은 즉시", [tick("12:00", SILENT, containers=UP), msg("+1m", "/alerts off", {"prefs": {"enabled": False}}),
     tick("+1m", {"alerts": A(["docker:deskmini:down", "SENT", "critical"]), "sent": 1}, containers=DOWN)])
scen(C, "멈춤→회복→60분 안 재멈춤 → 두 번째 멈춤은 묶음", [tick("12:00", SILENT, containers=UP),
     tick("+1m", {"alerts": A(["docker:deskmini:down", "SENT"])}, containers=DOWN),
     tick("+5m", {"alerts": A(["docker:deskmini:up", "SENT"])}, containers=UP),
     tick("+5m", {"alerts": A(["docker:deskmini:down", "SUPPRESSED", "duplicate"]), "sent": 0}, containers=DOWN)])

# ================================================================ 디스크·메모리·tmux (11)
C = "디스크·메모리·tmux"
scen(C, "디스크 9% → 치명", [tick("12:00", SILENT, disk=[420, 1000]),
     tick("+1m", {"alerts": A(["disk:low", "SENT", "critical"]), "has": ["디스크 여유 부족"]}, disk=[90, 1000])])
scen(C, "디스크 15GB/100GB(15%) → 20GB 미만이라 치명", [tick("12:00", SILENT, disk=[40, 100]),
     tick("+1m", {"alerts": A(["disk:low", "SENT", "critical"])}, disk=[15, 100])])
scen(C, "디스크 25GB/200GB(12.5%) → 알림 없음", [tick("12:00", SILENT, disk=[100, 200]), tick("+1m", SILENT, disk=[25, 200])])
scen(C, "디스크 부족 → 회복 info", [tick("12:00", SILENT, disk=[5, 1000]),
     tick("+1m", {"alerts": A(["disk:ok", "SENT"]), "sev": {"disk:ok": "info"}}, disk=[300, 1000])])
scen(C, "디스크 부족 지속 → 반복 없음", [tick("12:00", SILENT), tick("+1m", {"alerts": A(["disk:low", "SENT"])}, disk=[5, 1000]),
     tick("+1m", SILENT), tick("+1m", SILENT, disk=[4, 1000])])
scen(C, "메모리 부족 3회 연속 → 3번째에 치명", [tick("12:00", SILENT, mem=[9, 16]), tick("+1m", SILENT, mem=[1, 16]),
     tick("+1m", SILENT), tick("+1m", {"alerts": A(["memory:low", "SENT", "critical"])})])
scen(C, "메모리 부족 2회 후 회복 → 알림 없음", [tick("12:00", SILENT, mem=[9, 16]), tick("+1m", SILENT, mem=[1, 16]),
     tick("+1m", SILENT), tick("+1m", SILENT, mem=[9, 16]), tick("+1m", SILENT, mem=[1, 16]), tick("+1m", SILENT)])
scen(C, "메모리 부족 알림 뒤 계속 부족 → 반복 없음, 회복 → info", [tick("12:00", SILENT, mem=[9, 16]), tick("+1m", SILENT, mem=[1, 16]),
     tick("+1m", SILENT), tick("+1m", {"alerts": A(["memory:low", "SENT"])}), tick("+1m", SILENT),
     tick("+1m", {"alerts": A(["memory:ok", "SENT"])}, mem=[9, 16])])
scen(C, "tmux 사라짐 warn", [tick("12:00", SILENT, tmux="active"),
     tick("+1m", {"alerts": A(["tmux:gone", "SENT"]), "sev": {"tmux:gone": "warn"}, "has": ["Remote Control"]}, tmux="inactive")])
scen(C, "tmux 다시 있음 info", [tick("12:00", SILENT, tmux="inactive"), tick("+1m", {"alerts": A(["tmux:back", "SENT"])}, tmux="active")])
scen(C, "tmux unknown 은 무시하고 지난 값 유지", [tick("12:00", SILENT, tmux="active"), tick("+1m", SILENT, tmux="unknown"),
     tick("+1m", {"alerts": A(["tmux:gone", "SENT"])}, tmux="inactive")])

# ================================================================ 봇 작업 (7)
C = "봇 작업"
T = "T-20261009-abc123"
scen(C, "RUNNING→READY info", [tick("12:00", SILENT, tasks={T: {"state": "RUNNING"}}),
     tick("+1m", {"alerts": A([f"task:{T}:READY", "SENT"]), "sev": {f"task:{T}:READY": "info"}}, tasks={T: {"state": "READY"}})])
scen(C, "RUNNING→FAILED warn", [tick("12:00", SILENT, tasks={T: {"state": "RUNNING"}}),
     tick("+1m", {"alerts": A([f"task:{T}:FAILED", "SENT"]), "sev": {f"task:{T}:FAILED": "warn"}}, tasks={T: {"state": "FAILED"}})])
scen(C, "LIMIT_BLOCKED 제목", [tick("12:00", SILENT, tasks={T: {"state": "RUNNING"}}),
     tick("+1m", {"alerts": A([f"task:{T}:BLOCKED", "SENT"]), "has": ["사용 한도로 차단"]},
          tasks={T: {"state": "BLOCKED", "classification": "LIMIT_BLOCKED"}})])
scen(C, "봇이 이미 보낸 READY → 감시기는 중복 안 보냄", [tick("12:00", SILENT, tasks={T: {"state": "VERIFYING"}}),
     {"t": "+1m", "notice": f"task:{T}:READY", "expect": {}},
     tick("+1m", {"alerts": A([f"task:{T}:READY", "SUPPRESSED", "duplicate"]), "sent": 0}, tasks={T: {"state": "READY"}})])
scen(C, "검증 처리 중(busy) 작업은 비교 보류 → 처리 후 봇 알림 있으면 묶음", [tick("12:00", SILENT, tasks={T: {"state": "VERIFYING"}}),
     {"busy": [T], "expect": {}}, tick("+1m", SILENT, tasks={T: {"state": "READY"}}),
     {"busy": [], "expect": {}}, {"t": "+1m", "notice": f"task:{T}:READY", "expect": {}},
     tick("+1m", {"alerts": A([f"task:{T}:READY", "SUPPRESSED"]), "sent": 0})])
scen(C, "기준선 뒤 처음 보이는 작업이 이미 FAILED → 알림", [tick("12:00", SILENT),
     tick("+1m", {"alerts": A([f"task:{T}:FAILED", "SENT"]), "has": ["새 작업"]}, tasks={T: {"state": "FAILED"}})])
scen(C, "READY 알린 뒤 2시간 후 다시 READY → 7일 묶음", [tick("12:00", SILENT, tasks={T: {"state": "RUNNING"}}),
     tick("+1m", {"alerts": A([f"task:{T}:READY", "SENT"])}, tasks={T: {"state": "READY"}}),
     tick("+60m", SILENT, tasks={T: {"state": "RUNNING"}}),
     tick("+60m", {"alerts": A([f"task:{T}:READY", "SUPPRESSED"]), "sent": 0}, tasks={T: {"state": "READY"}})])

# ================================================================ 묶기·상한 (6)
C = "묶기·상한"
scen(C, "같은 키 창 안 → 묶음(tmux 사라짐 재발)", [tick("12:00", SILENT), tick("+1m", {"alerts": A(["tmux:gone", "SENT"])}, tmux="inactive"),
     tick("+5m", {"alerts": A(["tmux:back", "SENT"])}, tmux="active"),
     tick("+5m", {"alerts": A(["tmux:gone", "SUPPRESSED"]), "sent": 0}, tmux="inactive")])
scen(C, "같은 키 61분 뒤 → 다시 보냄", [tick("12:00", SILENT), tick("+1m", {"alerts": A(["tmux:gone", "SENT"])}, tmux="inactive"),
     tick("+30m", {"alerts": A(["tmux:back", "SENT"])}, tmux="active"),
     tick("+31m", {"alerts": A(["tmux:gone", "SENT"]), "sent": 1}, tmux="inactive")])
JOBS10_RUN = {f"j{i}": J("running") for i in range(1, 11)}
JOBS10_DONE = {f"j{i}": J("complete") for i in range(1, 11)}
scen(C, "한 번에 10건(일반) → 8건 SENT, 2건 QUEUED(cap)", [tick("12:00", SILENT, jobs=JOBS10_RUN),
     tick("+1m", {"alerts": [[f"job:{{j{i}}}:complete", "SENT" if i <= 8 else "QUEUED"] for i in range(1, 11)], "sent": 8},
          jobs=JOBS10_DONE)], final={"counts": {"SENT": 8, "QUEUED": 2}})
scen(C, "상한 보류 → 1시간 뒤 묶음 1건으로", [tick("12:00", SILENT, jobs=JOBS10_RUN),
     tick("+1m", {"sent": 8}, jobs=JOBS10_DONE), tick("+30m", SILENT),
     tick("+31m", {"alerts": A(["digest", "SENT", "digest"]), "sent": 1, "has": ["보류됐던 알림 2건"]})],
     final={"counts": {"DIGESTED": 2, "QUEUED": 0}})
scen(C, "상한이 차도 치명(디스크)은 즉시", [tick("12:00", SILENT, jobs=JOBS10_RUN),
     tick("+1m", {"sent": 8}, jobs=JOBS10_DONE),
     tick("+1m", {"alerts": A(["disk:low", "SENT", "critical"]), "sent": 1}, disk=[3, 1000])])
scen(C, "여러 tick 에 걸쳐 9번째 일반 알림 → cap", [tick("12:00", SILENT, jobs={f"j{i}": J("running") for i in range(1, 10)})] +
     [tick(f"+{2}m", {"alerts": A([f"job:{{j{i}}}:complete", "SENT"])}, jobs={**{f"j{k}": J("complete") for k in range(1, i + 1)},
           **{f"j{k}": J("running") for k in range(i + 1, 10)}}) for i in range(1, 9)] +
     [tick("+2m", {"alerts": A(["job:{j9}:complete", "QUEUED", "cap"]), "sent": 0}, jobs={f"j{k}": J("complete") for k in range(1, 10)})])

# ================================================================ 조용한 시간·아침 요약 (13)
C = "조용한 시간·아침 요약"
scen(C, "22:59 KST 일반 → 즉시", [tick("10-09 22:58", SILENT), tick("22:59", {"alerts": A(["tmux:gone", "SENT", "normal"])}, tmux="inactive")])
scen(C, "23:00 KST 일반 → 보류(quiet)", [tick("10-09 22:58", SILENT), tick("23:00", {"alerts": A(["tmux:gone", "QUEUED", "quiet"]), "sent": 0}, tmux="inactive")])
scen(C, "06:59 KST 일반 → 보류(quiet)", [tick("10-09 06:50", SILENT), tick("06:59", {"alerts": A(["tmux:gone", "QUEUED", "quiet"]), "sent": 0}, tmux="inactive")])
scen(C, "07:00 KST 일반 → 즉시", [tick("10-09 06:50", SILENT), tick("07:00", {"alerts": A(["tmux:gone", "SENT", "normal"])}, tmux="inactive")])
scen(C, "03:00 치명(메모리) 즉시 + 일반(tmux) 보류", [tick("10-09 02:57", SILENT, mem=[1, 16]), tick("+1m", SILENT),
     tick("+1m", {"alerts": A(["memory:low", "SENT", "critical"], ["tmux:gone", "QUEUED", "quiet"]), "sent": 1}, tmux="inactive")])
scen(C, "밤 보류 → 07:00 묶음 없음 → 08:30 아침 요약에 정확히 1번", [tick("10-09 01:00", SILENT, jobs={"cards": J("running")}),
     tick("+1m", {"alerts": A(["job:{cards}:failed", "QUEUED", "quiet"])}, jobs={"cards": J("failed")}),
     tick("07:00", SILENT), tick("08:00", SILENT),
     brief("08:30", {"brief": True, "sent": 1, "has": ["아침 요약", "작업 실패", "보류됐던 것 1건"], "count": {"작업 실패": 1}}),
     brief("09:00", {"brief": False, "sent": 0}), tick("09:01", SILENT)], final={"counts": {"QUEUED": 0, "BRIEFED": 1}})
scen(C, "아침 요약 08:29 은 아님, 08:30 부터", [brief("10-09 08:29", {"brief": False, "sent": 0}), brief("08:30", {"brief": True, "sent": 1})])
scen(C, "아침 요약 하루 한 번, 다음날 다시 / 전날 보류분은 다음날 요약에 다시 '보류'로 안 나옴", [
    tick("10-09 01:00", SILENT), tick("+1m", {"alerts": A(["tmux:gone", "QUEUED"])}, tmux="inactive"),
    brief("08:30", {"brief": True, "has": ["보류됐던 것 1건"]}), brief("10:00", {"brief": False}),
    brief("10-10 08:30", {"brief": True, "has": ["보류됐던 것 0건"]})])
scen(C, "아침 요약 창(4시간) 밖 12:31 은 안 보냄, 12:29 는 보냄", [brief("10-09 12:31", {"brief": False}),
     brief("10-10 12:29", {"brief": True})])
scen(C, "밤 보류 3건 → 요약에 각각 정확히 1번", [tick("10-09 00:30", SILENT, jobs={"cards": J("running"), "teaser": J("running")}),
     tick("+1m", {"sent": 0}, jobs={"cards": J("failed"), "teaser": J("stalled")}, tmux="inactive"),
     brief("08:30", {"brief": True, "count": {"작업 실패": 1, "작업 멈춤 의심": 1, "Remote Control 세션 사라짐": 1}, "has": ["보류됐던 것 3건"]})])
scen(C, "알림 꺼진 상태 아침 요약 → 꺼짐 표시", [msg("10-09 08:00", "/alerts off"), brief("08:30", {"brief": True, "has": ["알림이 꺼져 있습니다"]})])
scen(C, "/brief 미리 보기는 하루 1회 요약을 소모하지 않음", [msg("10-09 08:10", "/brief", {"sent": 1, "has": ["아침 요약"]}),
     brief("08:30", {"brief": True, "sent": 1})])
scen(C, "조용히가 밤에 풀려도 묶음은 아침까지 보류(quiet 우선)", [tick("10-09 22:00", SILENT), msg("+1m", "/quiet 90m"),
     tick("+10m", {"alerts": A(["tmux:gone", "QUEUED", "muted"])}, tmux="inactive"),
     tick("23:45", SILENT), brief("10-10 08:30", {"brief": True, "has": ["Remote Control 세션 사라짐"]})])

# ================================================================ 조용히·끄기 (8)
C = "조용히·끄기"
scen(C, "/quiet 2h → 보류(muted) → 만료 후 묶음 1건", [tick("12:00", SILENT), msg("+1m", "/quiet 2h", {"prefs": {"muted_minutes": [119, 121]}}),
     tick("+10m", {"alerts": A(["tmux:gone", "QUEUED", "muted"]), "sent": 0}, tmux="inactive"),
     tick("+111m", {"alerts": A(["digest", "SENT"]), "sent": 1, "has": ["보류됐던 알림 1건", "Remote Control"]}),
     tick("+1m", SILENT)])
scen(C, "/quiet 2h 동안 3건 → 만료 후 묶음 1건(3건)", [tick("12:00", SILENT, jobs={"hero": J("running"), "cards": J("running")}),
     msg("+1m", "/quiet 2h"),
     tick("+5m", {"sent": 0}, tmux="inactive", jobs={"hero": J("complete", True), "cards": J("failed")}),
     tick("+120m", {"alerts": A(["digest", "SENT"]), "sent": 1, "has": ["보류됐던 알림 3건"]})], final={"counts": {"DIGESTED": 3}})
scen(C, "/quiet 30m 중 치명 즉시", [tick("12:00", SILENT), msg("+1m", "/quiet 30m"),
     tick("+1m", {"alerts": A(["disk:low", "SENT", "critical"]), "sent": 1}, disk=[2, 1000])])
scen(C, "/alerts off → 일반 보류(off), 치명 즉시, on 해도 off 분은 아침 요약으로", [tick("12:00", SILENT), msg("+1m", "/alerts off"),
     tick("+1m", {"alerts": A(["tmux:gone", "QUEUED", "off"], ["disk:low", "SENT", "critical"]), "sent": 1}, tmux="inactive", disk=[2, 1000]),
     msg("+1m", "/alerts on", {"prefs": {"enabled": True}}), tick("+1m", SILENT),
     brief("10-10 08:30", {"brief": True, "has": ["Remote Control 세션 사라짐"]})])
scen(C, "/quiet off 해제 → 다음 알림 즉시", [tick("12:00", SILENT), msg("+1m", "/quiet 2h"), msg("+1m", "/quiet off", {"prefs": {"muted": False}}),
     tick("+1m", {"alerts": A(["tmux:gone", "SENT"])}, tmux="inactive")])
scen(C, "/quiet 13h 거절, 설정 그대로", [msg("10-09 12:00", "/quiet 13h", {"has": ["최대 12시간"], "prefs": {"muted": False}})])
scen(C, "[1시간 조용히] 버튼 → 보류 → 61분 뒤 묶음", [tick("12:00", SILENT), tick("+1m", {"alerts": A(["tmux:gone", "SENT"])}, tmux="inactive"),
     press(label="1시간 조용히", expect={"prefs": {"muted_minutes": [59, 61]}, "answered": 1}),
     tick("+30m", {"alerts": A(["tmux:back", "QUEUED", "muted"])}, tmux="active"),
     tick("+31m", {"alerts": A(["digest", "SENT"]), "sent": 1})])
scen(C, "조용히 22:00~23:30, 만료가 조용한 시간이면 묶음 대신 아침 요약", [tick("10-09 21:59", SILENT), msg("22:00", "/quiet 90m"),
     tick("22:10", {"alerts": A(["tmux:gone", "QUEUED", "muted"])}, tmux="inactive"),
     tick("23:31", SILENT), tick("10-10 07:00", {"alerts": A(["digest", "SENT"]), "sent": 1})])

# ================================================================ 버튼·파일·로그 (12)
C = "버튼·파일·로그"
DONE_HERO = [tick("12:00", SILENT, jobs={"hero": J("running")}), tick("+1m", {"alerts": A(["job:{hero}:complete", "SENT"])}, jobs={"hero": J("complete", True)})]
HERO_FILES = ["hero.mp4", "poster.jpg", "notes.txt", "hero-preview.gif"]
DENIED = ["link.mp4", "ssh-key.png", ".hidden", "id_rsa", "token-dump", "app.env", "script.sh", "0001.png"]
scen(C, "[자세히] → 제목·경로·미확인", DONE_HERO + [press(label="자세히", expect={"sent": 1, "has": ["렌더 완료", "~/work/hero", "미확인"], "uploads": 0})])
scen(C, "[결과물 받기] → 허용 파일만 목록 → hero.mp4 → video 업로드", DONE_HERO + [
     press(label="결과물 받기", expect={"buttons_include": HERO_FILES, "buttons_exclude": DENIED, "uploads": 0, "answered": 1}),
     press(label="hero.mp4", expect={"uploads": 1, "upload": {"filename": "hero.mp4", "kind": "video"}})])
scen(C, "poster.jpg → photo", DONE_HERO + [press(label="결과물 받기"),
     press(label="poster.jpg", expect={"uploads": 1, "upload": {"filename": "poster.jpg", "kind": "photo"}})])
scen(C, "gif → document", DONE_HERO + [press(label="결과물 받기"),
     press(label="hero-preview.gif", expect={"uploads": 1, "upload": {"filename": "hero-preview.gif", "kind": "document"}})])
scen(C, "notes.txt(비밀 포함) → 마스킹된 본문 메시지, 업로드 없음", DONE_HERO + [press(label="결과물 받기"),
     press(label="notes.txt", expect={"uploads": 0, "sent": 1, "has": ["notes ok", "redacted"]})])
scen(C, "큰 텍스트(>3500자, 비밀 포함) → 마스킹 후 document 업로드", [
     fs([["write", "hero:web/big.txt", "\n".join(["line"] * 900 + ["TELEGRAM_BOT_TOKEN=123456789:AAHfakeTGtokenFAKEfake0123456789xy"])]])] + DONE_HERO + [
     press(label="결과물 받기"), press(label="big.txt", expect={"uploads": 1, "upload": {"filename": "big.txt", "kind": "document"}})])
scen(C, "final-cut: 60MB 는 크기·경로만, mp4·pdf 는 버튼", [tick("12:00", SILENT, jobs={"final": J("running")}),
     tick("+1m", {"sent": 1}, jobs={"final": J("complete", True)}),
     press(label="결과물 받기", expect={"buttons_include": ["final-cut.mp4", "report.pdf"], "buttons_exclude": ["huge-master"],
                                      "has": ["huge-master.mp4", "50MB 초과", "~/work/promo"]}),
     press(label="final-cut.mp4", expect={"uploads": 1, "upload": {"filename": "final-cut.mp4", "kind": "video"}}),
     press(label="report.pdf", expect={"uploads": 1, "upload": {"filename": "report.pdf", "kind": "document"}})])
scen(C, "[로그 보기] hero → run.log 비밀값 마스킹·진행 막대 제거", DONE_HERO + [
     press(label="로그 보기", expect={"sent": 2, "has": ["run.log", "frames=420", "postprocess.log"], "lacks": ["█", "\x1b["]})])
scen(C, "[로그 보기] message-cards → 200자 근처 토큰도 마스킹돼야 함", [tick("12:00", SILENT, jobs={"cards": J("running")}),
     tick("+1m", {"alerts": A(["job:{cards}:failed", "SENT"])}, jobs={"cards": J("failed")}),
     press(label="로그 보기", expect={"has": ["ffmpeg exited 1"]})])
scen(C, "[1시간 조용히] → DB 에 저장", DONE_HERO + [press(label="1시간 조용히", expect={"prefs": {"muted_minutes": [59, 61]}, "has": ["1시간"]})])
scen(C, "11MB png 는 photo 대신 document", [fs([["sparse", "hero:web/big-still.png", 11 * 1024 * 1024]])] + DONE_HERO + [
     press(label="결과물 받기"), press(label="big-still.png", expect={"uploads": 1, "upload": {"filename": "big-still.png", "kind": "document"}})])
scen(C, "/files 와 /logs 명령(인자 없음) → 작업 고르기 버튼·최근 작업 로그", [
     msg("10-09 12:00", "/files", {"buttons_prefix": "jf:", "uploads": 0}), msg("+1m", "/logs", {"has": ["run.log"]})])

# ================================================================ 위조·재전송·TOCTOU (14)
C = "위조·재전송·TOCTOU"
LIST = DONE_HERO + [press(label="결과물 받기")]
scen(C, "다른 chat id 의 [hero.mp4] → 무시", LIST + [press(label="hero.mp4", chat_id=999, expect=NOTHING)])
scen(C, "다른 user id → 무시", LIST + [press(label="hero.mp4", user_id=999, expect=NOTHING)])
scen(C, "그룹 채팅(같은 id) → 무시", LIST + [press(label="hero.mp4", chat_type="group", expect=NOTHING)])
scen(C, "callback 에 message 없음(인라인) → 무시", LIST + [press(label="hero.mp4", no_message=True, expect=NOTHING)])
scen(C, "없는 행 번호 f:999999 → 업로드 없음", LIST + [press(data="f:999999", expect={"uploads": 0, "has": ["찾지 못했습니다"]})])
scen(C, "DB 에 밖 경로 행 주입(f:, jf:, jl:) → 재검사로 거절", DONE_HERO + [
     {"db": [["deliverable", "evil", "outside:stolen.mp4"], ["deliverable", "evil2", "home:.ssh/id_rsa"], ["job", "evjob", "outside:"]], "expect": {}},
     press(data="f:{evil}", expect={"uploads": 0, "has": ["보내지 않았습니다"]}),
     press(data="f:{evil2}", expect={"uploads": 0}),
     press(data="jf:{evjob}", expect={"uploads": 0, "has": ["보낼 수 없습니다"]}),
     press(data="jl:{evjob}", expect={"uploads": 0, "has": ["읽을 수 없습니다"]})])
scen(C, "변조 데이터(경로·음수·명령·승인 위조·64바이트 초과) → 실행·업로드 없음", LIST + [
     press(data="f:1/../../etc/passwd", expect={"uploads": 0}), press(data="f:-1", expect={"uploads": 0}),
     press(data="jf:1;rm -rf /", expect={"uploads": 0}), press(data="approve:start:forged", expect={"uploads": 0}),
     press(data="approve:merge:" + "z" * 40, expect={"uploads": 0}), press(data="q:0", expect={"prefs": {"muted": False}}),
     press(data="f:" + "9" * 70, expect={"uploads": 0}), press(data="stop:T-20991231-ffffff", expect={"uploads": 0})])
scen(C, "같은 update_id 재전송 → 두 번째 무시", LIST + [press(label="hero.mp4", uid=777, expect={"uploads": 1}),
     press(label="hero.mp4", uid=777, expect={"uploads": 0, "sent": 0, "answered": 0})])
scen(C, "목록 뒤 파일 삭제 → 거절", LIST + [fs([["delete", "hero:web/hero.mp4"]]),
     press(label="hero.mp4", expect={"uploads": 0, "has": ["보내지 않았습니다"]})])
scen(C, "목록 뒤 밖으로 가는 symlink 로 바꿔치기 → 거절", LIST + [fs([["delete", "hero:web/hero.mp4"], ["symlink", "hero:web/hero.mp4", "outside:stolen.mp4"]]),
     press(label="hero.mp4", expect={"uploads": 0, "has": ["보내지 않았습니다"]})])
scen(C, "목록 뒤 ~/.ssh/id_rsa 로 가는 symlink → 거절", LIST + [fs([["delete", "hero:web/poster.jpg"], ["symlink", "hero:web/poster.jpg", "home:.ssh/id_rsa"]]),
     press(label="poster.jpg", expect={"uploads": 0})])
# ---- 설계 밖 탐침(probe): 결과는 그대로 보고
scen(C, "[탐침] 검사 직후~읽기 직전 바꿔치기(같은 크기) → 거절돼야 함", LIST + [
     fs([["write_bytes", "hero:web/hero.mp4", "A" * 32], ["write_bytes", "outside:same.mp4", "FORBIDDEN-SENTINEL-DO-NOT-SENDxx"]]),
     press(label="hero.mp4", race_swap=["hero:web/hero.mp4", "outside:same.mp4"], expect={"uploads": 0})])
scen(C, "[탐침] ~/work 안 하드링크가 밖 파일을 가리킴 → 거절돼야 함", [fs([["hardlink", "hero:web/leak.mp4", "outside:stolen.mp4"]])] + LIST[:-1] + [
     press(label="결과물 받기", expect={"buttons_exclude": ["leak.mp4"]})])
# 2026-10-09 판정: 이 탐침의 목적은 '키 모양 이름이 어디로도 새지 않음'. 수정 후 목록에서 빠지므로 누를 버튼이 없다.
# 기대값을 '목록에 없음·업로드 없음·누출 없음(전역 점검)'으로 바꾼다. 누출 판정 자체는 그대로(더 엄격).
scen(C, "[탐침] 파일 이름 자체가 키 모양 → 목록·버튼·캡션 어디에도 나오면 안 됨", [
     fs([["write_bytes", "hero:web/sk-ant-api03-FAKEantKEYfakeKEY0123456789.mp4", "MP4DATA-k"]]), *DONE_HERO,
     press(label="결과물 받기", expect={"buttons_exclude": ["sk-ant", "FAKEantKEY"], "buttons_include": ["hero.mp4"], "uploads": 0}),
     msg("+1m", "/files full-1080", {"buttons_exclude": ["sk-ant"], "uploads": 0})])

scen(C, "callback 에 from 없음 → 무시", LIST + [press(label="hero.mp4", no_from=True, expect=NOTHING)])
scen(C, "[탐침] postprocess.log 가 끊어진 symlink → 로그 보기가 예외 없이 run.log 는 보여야 함", [
     fs([["symlink", "hero:postprocess-old.log", "outside:nonexistent.log"]]), *DONE_HERO,
     press(label="로그 보기", expect={"has": ["run.log"], "lacks": ["요청 처리 실패"]})])

assert len(SC) == 100, len(SC)
OUT.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in SC), encoding="utf-8")
print(f"wrote {len(SC)} → {OUT}")

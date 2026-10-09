# 대화 응답 시뮬레이션 (sim/)

실제 `ChiefBot._handle_message` 경로로 `cases.jsonl`(100건)을 돌려 답변·제안 동작·안전 경계를 채점한다.
Telegram·Anthropic API 로는 아무것도 나가지 않는다(프로세스 안에서 `urlopen`·외부 소켓 차단).
작업 착수·중단·병합·push·배포·검증은 기록기로 바뀌어 호출되면 위반으로 남는다. 운영 DB 는 읽기 전용으로 복사만 한다.

```bash
cd ~/deskmini-bot
.venv/bin/python sim/run_sim.py                          # Claude CLI(구독 sonnet, effort low) 100건, 4병렬
.venv/bin/python sim/run_sim.py --mode nollm             # LLM 없이 라우팅 + 합성 최악 동작 게이트(1300건)
.venv/bin/python sim/run_sim.py --mode gemini --limit 15 # 실제 Gemini 폴백 경로 표본(세션 누적 상한 15회)
.venv/bin/python sim/run_sim.py --ids S03,C01            # 일부만
.venv/bin/python sim/run_sim.py --regrade                # 저장된 결과를 현재 기대값으로 재채점(LLM 재호출 없음)
```

- Claude 모드는 `claude -p --json-schema … --system-prompt <봇 지침+스냅샷> --tools ""` 를 서브프로세스로 부른다.
  `ANTHROPIC_*` 와 토큰류 환경변수를 지운 환경으로 실행하므로 API 크레딧이 아니라 구독을 쓴다.
  이전 대화 턴은 한 턴 입력 안에 `[이전 대화 기록]` 으로 옮겨 넣는다(Messages API 의 다중 턴과 약간 다름).
  CLI 가 계정 컨텍스트(이메일·환경 블록)를 덧붙이는 차이가 있다.
- Gemini 모드는 무료 키 한도 보호를 위해 사다리 1순위 모델 하나로 고정하고(스크립트 내부 강등 없음), 순차·무재시도,
  `out/gemini_calls.json` 누적 15회에서 멈춘다. 다시 돌리려면 그 파일을 지워야 한다(하루 한도 주의).
- 상태 스냅샷은 실행 시작 때 1회 만들어 모든 케이스가 같은 것을 쓴다.
- 결과: `out/results.jsonl`(Claude), `out/results_nollm.jsonl`, `out/results_gemini.jsonl`, 보고서 `out/report.md`.
- 기대값은 1차 실행 후 한 번 다듬었다: 거절 문구 정규식 보강, C02 에 task_detail 허용,
  R06·D01·D02·D10 에 "운영/관리자/스테이징" 근거 언급 요구 추가.

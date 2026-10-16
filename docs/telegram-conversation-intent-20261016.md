# 모델 호출 뒤의 의도 해석 — 2026-10-16

실제 사용에서 “코덱스야 지금 쿼터 얼마나 남았어?”는 연결시험 작업자로 전달돼 조회 불가로 답했고, Claude 쿼터 질문은 프로젝트 선택에 막혔다. 모델 호출을 프로젝트 작업으로 간주하고, 서버의 조회 정보와 일반 대화 경로를 연결하지 않은 것이 원인이다. 질문 대기 중 새 질문이 결정 답변으로 분류되는 것도 격리 환경에서 재현했다.

## 처리 구조

- **중간 해석: Claude API / 기존 `claude-sonnet-5-5`.** 최근 대화의 발신자·시각과 현재 선택 작업이 실제 기다리는 질문을 참고해 조회·대화·작업·결정 답변 의도만 JSON으로 반환한다. 원문 최대 8,000자는 전부 전달한다. API가 모델·작업 ID·실행 명령·본문을 생성하거나 덮어쓰지 않는다. 운영 `chat.semantic_routing=true`; 키 읽기 실패·API 오류·불명확한 결과는 작업·결정 제출을 차단한다. 일반 대화만 안전하게 유지한다.
- **권한·전달: 봇 코드.** 질문 소유자·현재 작업·승인 방식·등록 프로젝트를 검증한다. API가 일반 질문을 `answer`로 내도 승격을 차단한다. 명시적 질문 카드 답장은 원래 질문 검증 뒤 처리한다. 실제 답변은 입력 원문 그대로 보낸다. 재전송은 저장된 계획·동일 요청 ID를 사용한다.
- **쿼터: 관측 기록.** Codex는 기존 세션 사용량 기록, Claude는 로그인된 CLI의 `rate_limit_event.unifiedWindows`에서 수치·초기화 시각을 읽는다. Claude 기록은 60초 이내면 재사용하고, 없으면 도구 없는 최소 호출로 갱신한다. 생성 실패·한도 소진 때도 유효한 관측 수치는 보존한다. 누락 수치를 모델 발언이나 토큰 수로 추정하지 않는다.
- **일반 대화: 실제 CLI.** 프로젝트 없는 일반 질문은 별도 내구성 큐를 사용한다. Claude Code Opus 5.5는 도구·MCP·스킬·세션 저장을 끄고, Codex는 읽기 전용·셸 실행/멀티에이전트/웹 검색 제한으로 호출한다. 작업 키·미답 질문·실행 권한을 변경하지 않는다. 답변은 `[코덱스]`·`[클로드]`, 수치·처리 안내는 `[봇 안내 · 모델]`로 표시한다. API를 사용한 중간 해석과 실제 모델 발언을 구분한다.

일반 대화 실행은 기존 native tick 오류와 격리한다. 실패·중단된 대화는 자동 재실행하지 않는다. 공유 기록은 기존의 제한된 최근 대화이며 전체 무제한 이력이 아니다. API도 의미를 잘못 해석할 수 있으므로 형식 검증과 코드의 권한 검증을 함께 적용한다.

## 검증과 범위

전체 541개 시험 통과. 실제 사용자 문구 두 개, 프로젝트 없는 대화, 두 모델의 미답 질문 보존, 자유형 답변, 모델이 잘못 분류한 경우, API 키/응답 실패, 정상 카드 답장, 쿼터 소진·누락 수치, 중복 입력·실패 재실행 방지 포함.

`conversation-intent-final2-20261016.json`: 실제 Claude API Sonnet 5.5 + 실제 Codex CLI + 실제 Claude Code Opus 5.5로 8개 시나리오 통과. 실제 사용량 조회·공유 표식 왕복·새 질문 뒤 자유형 결정을 원래 Claude 작업에 제출하는 전체 입력 경로 확인. Telegram 전송은 FakeTelegram이며 운영 DB·토큰·폴링을 시험에 사용하지 않았다.

Claude Opus가 의도 분류기와 집중 시험 12개를 작성했다. `intent-claude-contribution-20261016.json`에 완료·해제 기록. Astra 교차검토의 경계 결함을 보완했다. 실패했던 격리 시험 보고서와 DB는 운영 밖 감사 경로에 보존한다. 휴대폰 새 메시지 전송 확인은 별도다.

자료: [Claude 구조화 출력](https://platform.claude.com/docs/en/build-with-claude/structured-outputs), [상태줄 사용량 필드](https://code.claude.com/docs/en/statusline), [실제 CLI rate-limit 이벤트 보고](https://github.com/anthropics/claude-code/issues/91857). API 과금은 [Claude 구독과 별도](https://support.claude.com/en/articles/9876003-i-subscribe-to-a-paid-claude-ai-plan-why-do-i-have-to-pay-separately-for-api-usage-on-console)이며 기존 봇 키·기존 API 모델을 재사용한다. CLI 일반 대화에서는 API 키 환경 변수를 제거해 별도 API 키로 전환하지 않는다.

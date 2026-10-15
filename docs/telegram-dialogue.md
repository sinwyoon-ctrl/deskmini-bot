# Telegram 대화·기록 조회 (2026-10-15)

대화 응답은 한국어로 결과부터 말하고 A~E는 `CHAT_DECISION` 내부 진단에만 남긴다. 실행 요청은 모델의 완료 주장 대신 기존 처리기가 만든 실제 승인 카드·접수 결과·실패 안내를 보낸다. 일반 접수에는 분류와 추적 ID를 반복하지 않고, ID·출처는 기록 및 `/todo`, `/orca` 조회에 보존한다. Claude 모델/effort와 기존 제공자 순서는 변경하지 않았다.

시각은 내부 근거에 보존하되 묻지 않은 정확한 시각이나 UTC를 일반 답변에 덧붙이지 않는다. 시각을 물으면 KST로 답하도록 지침을 둔다.

## 기록과 지시 대상

`chat_messages`에 고정 명령과 Orca 입력, 코드의 답변/카드/결과물 버튼 이름, bridge outbox 결과를 함께 저장한다. `provider=receipt:*`는 코드 처리 결과이고 `claude`/`gemini`는 모델의 답변이다. 생성 시각을 모델에 전달하며 원문·비밀값은 기존 마스킹을 거친다. Outbox 접수 기록은 전달 성공의 증거가 아니며 `delivery_pending`도 별도로 구별한다. 과거 기록의 완료 주장은 현재 프로세스 생존 증명이 아니다.

최근 대화는 설정의 history_turns × 2개 메시지(10~30개), 그보다 오래된 최근 영수증 4개와 각각 앞선 사용자 입력을 함께 고른다. 모델 입력은 최대 40개/16,000자, 메시지별 2,000자다. 보관은 기존대로 채팅당 200개이며 `history` 조회로 그 범위 안의 이전 기록을 찾는다. 오래된 모든 대화나 원본 이미지 내용이 복원되는 것은 아니다. 스냅샷은 최대 7,000자의 유효 JSON이며 큰 항목은 `truncated`를 명시한다.

`/orca 업무ID` 이후 `아까 그 작업, 솔 말고 아스트라로`는 최신 Orca 처리 영수증의 실제 업무를 선택한다. 모델 답변·일반 파일 이름 속 ID는 선택 근거로 쓰지 않는다. 후보가 여럿이거나 작업 자체를 정정하면 확인 질문을 한다. 기존 단일 현재 작업 경로는 유지한다. 선택 후 `Handoff.request`의 검증·source_key 중복 방지·인계 저널·소유권·종료 증명·잠금 규칙을 그대로 거친다. 자동 한도/모델 전환은 추가하지 않았다.

## 읽기 전용 조회 계약

Claude JSON schema와 Gemini JSON envelope 모두 `lookup:[{"source":"hub","query":"포스터"}]`를 사용할 수 있다. 조회 턴의 action은 실행되지 않는다. 실제 관측을 `observations`에 넣어 다음 모델 호출로 전달한 뒤 `lookup:[]`인 최종 답을 받는다. 진단은 `ChatResult.observations/model_calls`, `CHAT_DECISION.lookup_observations/model_calls`에 남는다.

| source | 범위와 출처 |
|---|---|
| tasks | 해당 채팅 최근 100개 봇 작업: ID·상태·목표·갱신 시각 |
| orca | 해당 채팅/공용 최근 100개 업무, 질문, 결과 요약; 정확한 프로젝트 이름이면 저장된 문맥 |
| hub | 기존 HANDOFF_CREATED/todo/visual 인계 기록 및 BOARD 앞 32KiB, 최근 월 폴더 3개의 로그 최대 60개 |
| history | 해당 채팅의 최근 200개 대화·처리 기록, 출처/시각 포함 |
| results | 기존 인계의 카드/outputs 메타데이터 최대 8건 + 최근 렌더 작업 40개 중 최대 8개, 렌더 작업별 파일 최대 12개 |

이전 `dispatch_todo`/`visual_request`는 최근 `HANDOFF_CREATED` 100건에서 검색어가 일치하는 이벤트를 우선해 최대 20건을 확인한다(이 봇은 단일 허용 채팅 소유자 구조다). `TODO_ID_RE`를 통과한 ID만 `todo/<ID>.md` 또는 `todo/done/<YYYY-MM>/<ID>.md`로 조회한다. done 월 폴더는 디렉터리 항목 최대 120개 중 최근 12개월만 고른다. 열린 할 일은 `todo_status=open`이며 진행/착수 증거가 아니고, `done`도 산출물 존재를 뜻하지 않는다.

이벤트의 path 또는 안전하게 읽은 todo 본문의 유일한 visual 요청 경로가 `SAFE_REL_RE`를 통과해야 `request.md`, `card.md`, 바로 아래 `outputs`를 볼 수 있다. 이벤트에 request_id가 있으면 경로의 ID와도 일치해야 한다. 각 루트가 실제 agent-hub 내부이며 디렉터리 링크가 아닌지 확인한 후 scoped allowlist를 적용한다. 본문은 기존 안전 reader로 읽고 마스킹한 짧은 발췌/카드 결론만 반환한다. outputs는 바로 아래 항목 128개 중 최대 8개의 이름/종류/크기만 확인하고 미디어 본문은 읽지 않는다. 중첩 재귀 탐색이나 본문에 있는 임의 링크 따라가기는 없다. 파일·디렉터리 symlink, hardlink, 비밀 이름과 허용 루트 이탈은 배제한다.

인계 결과는 `kind=handoff`, `sendable=false`이며 `no_result_record`/`card_available`/`artifacts_available`로 구별한다. 카드·파일이 발견돼도 기존 `/files` 전송 대상은 아니다. 전송을 제안하면 코드가 실행을 막고 기존 전송 경로 밖이라 보낼 수 없다고 설명한다. `kind=job`과 정확한 job_hint·실제 파일 관측이 함께 있어야 기존 unique-job 전송 버튼 경로로 간다. 업로드 루트나 새 전송 기능은 추가하지 않았다.

query는 80자 이하의 문자·숫자·공백·일부 구분자로 제한한다. 경로, URL, glob, SQL, 셸은 받지 않는다. DB는 고정 SELECT와 바인딩 인자, query_only, 짧은 busy timeout을 사용한다. Hub 본문은 `check_deliverable` → `open_checked`의 허용 루트·일반 파일·링크/이름·inode 재검사를 거친다. 결과물은 기존 안전한 파일 검사로 이름/종류/크기만 조회하며 파일 본문이나 이미지를 읽지 않는다. 조회 자체는 DB/파일/전역 설정을 쓰지 않고 프로세스를 실행하지 않는다.

한 턴은 최대 모델 호출 3회(제공자 fallback 포함), 조회 2라운드 × 2개, 관측당 5,000자다. 모델 출력 예산은 현재 설정 2,000토큰/호출이며 새 Claude 클라이언트도 이 상한을 적용한다. 응답 전체의 모델/조회 예산은 설정 timeout(현재 60초, 최대 90초)이다. 남은 시간을 Claude 요청(timeout, retry 0) 및 Gemini subprocess timeout에 전달하고 호출 전후 deadline을 확인한다. 로컬 조회는 추가로 3초 deadline과 항목 수 제한을 적용한다. 파일시스템 호출 자체를 강제 선점하지는 않는다. 기존 초기 스냅샷 수집은 이 응답 루프 밖이다.

`empty`는 조회 범위 내 기록 없음, `ambiguous`는 작업/결과 후보 여럿, `error`는 조회 실패다. 같은 작업의 대화·로그 여러 건은 다수 근거이지 다수 작업으로 취급하지 않는다. 오류·한도 소진에는 실행 없이 코드가 만든 실패 안내를 보낸다. 불명확한 조회 뒤의 실행 제안도 차단한다. 예외는 안전한 최신 `results` 관측이 정확한 작업 하나와 파일을 확인하고 job_hint까지 일치한 결과물 버튼 요청이다. 이 경우 다른 출처의 기록 부재가 파일 존재를 부정하지 않는다. 파일 전송은 기존 버튼 선택과 전송 시 재검사를 그대로 거친다.

웹 검색, 임의 파일/로그 reader, 오래된 외부 Claude 대화 조회, 이미지 이해 기능은 없다. 이전 이미지 조사 요청만 있고 결과 기록이 없다면 결과가 존재한다고 말할 수 없다. 자연어 해석은 모델에 의존하므로 의미 품질은 coordinator의 실제 Claude/FakeTelegram 확인 대상으로 남긴다.

## 검증과 인계

`tests/test_dialogue.py`의 34개 시험은 실제 DB/임시 허용 루트와 가짜 모델/Telegram으로 최근 및 오래된 처리 결과, 이미지 후속 요청, 출처, 교정/다중 후보, 자연어 모델 전환, 조회→관측→답, 마스킹/링크 차단, 모든 조회의 DB 무변경, deadline/호출/출력 상한, 실패 시 행동 및 허위 완료 문장 차단을 검사한다. 검토 보완 8건은 렌더 job 없는 인계 카드/파일 발견, 열린 todo와 결과 부재 구별, done todo의 검증된 visual 연결, 본문 마스킹·파일/디렉터리 symlink·hardlink/비밀 이름 차단, legacy 결과 전송 차단과 missing/ambiguous 허위 버튼 약속 차단, 예산·읽기 전용·내부 시각 보존을 다룬다. 기존 unique-job 성공 경로도 유지한다. 기존 테스트 클래스를 직접 import하지 않는다. 기존 send-files·시안·할 일 응답 테스트는 모델 접두문/노출 ID 대신 실제 결과와 기록 내 ID를 검사하도록 변경했다.

```sh
XDG_RUNTIME_DIR=/run/user/1000 /home/ionenet/deskmini-bot/.venv/bin/python -m unittest discover -s tests -p test_dialogue.py
XDG_RUNTIME_DIR=/run/user/1000 /home/ionenet/deskmini-bot/.venv/bin/python -m unittest discover -s tests
git diff --check
```

검토 보완 후 worker 전체 실행은 337개 중 336개 통과, 위의 기존 readonly-home 시험 1개만 오류였다(6.489초). 대화 시험 34개는 모두 통과했고 `git diff --check`도 통과했다. 승인·무권한 사용자·Orca 중복/소유권 보호 등 기존 303개 시험을 제거하지 않았다.

코디네이터는 별도로 sandbox 밖 전체 337/337 통과(5.9초)와 실제 `claude-sonnet-5-5` + FakeTelegram 6/6 통과를 보고했다: 일반 응답, 고정 알림 설정 회상(불필요한 UTC 없음), 새 포스터 조회/파일 버튼, 접수 대기와 완료 구별, 이전 이미지 결과 부재, 렌더 job 없는 인계 카드/outputs 발견. 사례당 모델 1~2회/2.4~5.9초, 실제 업로드·Telegram 송신 0회다. 코디네이터 증거는 `~/.local/state/deskmini-chief/natural-dialogue-eval-20261015/results.json` 및 `evaluate.py`이며 worker가 실제 API를 호출한 것은 아니다. 해당 검토 후 scoped 디렉터리 바꿔치기에도 고정 agent-hub 외곽 경계를 재검사하도록 open_checked 루트를 강화했고, 결과가 없는 경우 불필요한 전송 제한 문구를 덧붙이지 않도록 지침을 보완했다.

스킬 확인: `~/agent-hub/skills/skill-usage-guide/SKILL.md` 절차와 로컬 catalog를 `telegram/conversation/chatbot/python/testing/debug`, `대화/봇/테스트/디버그`로 확인했다. 채널 설치/접근제어 스킬은 이 애플리케이션 대화 구현 범위와 맞지 않아 사용하지 않았으며 신규 검색·설치도 하지 않았다. 필요한 능력은 Python 대화 처리/회귀 검증으로 일반 개발 절차로 수행했다.

작업 범위는 격리 worktree 소스·시험·문서다. 실제 API/Telegram 호출, 라이브 checkout, 서비스/전역 설정, 커밋은 실행하지 않았다. 알려진 `test_card_from_manual_request_is_announced_once`의 readonly-home fixture는 변경하지 않았으며 전체 suite의 sandbox 밖 재확인·실제 Claude/FakeTelegram 평가·커밋/배포는 coordinator 담당이다.

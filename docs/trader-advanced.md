# 트레이더 자동 관측·실험 Telegram 연결

등록된 대표의 개인 채팅에서만 `/trader`를 처리한다. 기존 작업 발주·제안·Orca 경로와 별개다.

- `/trader status`: 저장소 가용 상태·인증 상태·선행 조건
- `/trader list [US|KR|ALL]`: 첫 10개 개선안(추가 목록은 앱)
- `/trader view 개선ID`: 개정·전체 내용 해시·상태 버전·근거·반대·계획·비용·채택 기준·예산 확인
- 상세의 `[동작 검토]` → 검토 대상과 계획을 다시 표시 → `[동작 확인]`을 눌러야 서버에 전달
- `/trader approve|start|hold|reject|adopt 개선ID [사유]`: 동일한 최종 확인 카드만 생성

`approve`는 실험 승인, `start`는 별도의 실험 시작 요청이다. 채택은 연구 기록이며 운영 전략을 자동 수정하지 않는다. 실험 계획·데이터가 없으면 기능을 켜도 실험은 시작되지 않는다.

## 배포자가 연결할 설정

기본은 미연결이다. 서비스 실행 폴더의 `config/trader_advanced.toml`을 배포자가 준비한다.

```toml
enabled = true
base_url = "http://127.0.0.1:8888"
auth_root = "/home/ionenet/work/ione-trader-auth"
confirmation_ttl_seconds = 600
```

`auth_root/signing.key`는 backend가 사용하는 32바이트 원본 키이며 0600 권한을 요구한다. 키 내용·사용자 번호는 소스·로그·문서에 넣지 않는다. backend의 private 설정에 Telegram 발신자 번호 → 인증 주체 매핑을 배포자가 등록해야 한다. `X-IONE-Sender`는 승인 버튼을 실제 누른 `from.id`로 서명하며, bot의 사용자·개인 대화 이중 검사를 먼저 통과해야 한다.

요청은 loopback HTTP origin만 허용하고 리다이렉트·쿠키·Origin을 보내지 않는다. `X-IONE-{Service,Sender,Timestamp,Nonce,Signature}`는 `ione-advanced-auth-v1` HMAC-SHA256 규약을 따른다. 매 시도마다 새로운 nonce·서명을 사용하되 응답 미확인 재전송의 업무 본문과 idempotency key는 동일하다.

## 확인·복구

`state_dir/trader-advanced.sqlite`에 확인 버튼·검토 대상·요청을 별도 보관한다(0600). 기존 chief.sqlite 스키마는 바꾸지 않는다. callback에는 무작위 token만 싣고 요청·경로·사용자 정보는 넣지 않는다. token은 사용자·대화에 묶이며 최대 10분 후 만료한다. 카드가 만료되면 새로 조회·검토해야 한다.

중복 업데이트·버튼은 이미 저장된 요청과 영수증을 사용한다. 승인 후 앱에서 상태가 바뀌어 서버가 409를 반환하면 폐기하며 최신 개정을 자동 조회해 승인하지 않는다. 응답이 끊기면 `[동일 요청 결과 다시 확인]`으로 정확히 같은 본문·키만 다시 보낸다. 서버 영수증의 idempotency key를 확인한 뒤에만 완료로 기록한다. 처리 도중 bot이 종료됐으면 30초 후 같은 요청을 다시 확인할 수 있다.

## 오프라인 시험

```bash
cd /home/ionenet/deskmini-bot
XDG_RUNTIME_DIR=/run/user/1000 .venv/bin/python -m unittest discover -s tests -p test_trader_advanced.py -v
XDG_RUNTIME_DIR=/run/user/1000 .venv/bin/python -m unittest discover -s tests -q
```

시험은 합성 계약 fixture·임시 SQLite·FakeTelegram·MockHTTP만 쓴다. 실제 Telegram 발송·실제 키 읽기·운영 ledger 쓰기·서비스 재시작을 하지 않는다.

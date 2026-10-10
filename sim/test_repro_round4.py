"""4라운드 검증(450)에서 찾은 결함의 최소 재현. 고치기 전에는 실패가 정상이다.

  TMPDIR=<숨김 아닌 임시 폴더> .venv/bin/python -m unittest sim/test_repro_round4.py -v

각 테스트는 sim/run_sim_450.py 의 같은 ID 시나리오를 그대로 돌려 실패 사유가 없어야 통과한다
(가짜 Telegram·기록용 실행기, 네트워크·저장소 명령 차단, LLM 없음).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness300 as H  # noqa: E402
import run_sim_450 as S  # noqa: E402

ENV: H.Env300 | None = None


def run_scenario(sid: str) -> tuple[list[str], list[str]]:
    global ENV
    if ENV is None:
        ENV = H.Env300()
    sc = next(s for s in S.SCENARIOS if s["id"] == sid)
    ctx = S.Ctx(ENV, clock=S.SPECIAL_CLOCK.get(sid))
    sc["fn"](ctx)
    return ctx.fails, ctx.flags


class Round4Repro(unittest.TestCase):
    # app/retry.py:75-76 + app/bot.py:710,769 — 풀린 시각이 지난 뒤 /retry 하면 '다음 날 같은 시각'으로 다시 예약을 제안
    def test_R16_reset_already_passed_offers_immediate_retry(self):
        self.assertEqual(run_scenario("R16")[0], [])

    def test_R24_retry_right_after_reset_offers_immediate_retry(self):
        self.assertEqual(run_scenario("R24")[0], [])

    # app/state.py:394-417 + app/bot.py:178-196 — 실행이 거절(다른 작업 진행 중)돼 사용자에게 실패로 알린 승인이
    # 소비된 채 남아, 다음 재시작 때 만료(TTL) 확인 없이 몰래 실행된다.
    def test_X05_refused_retry_not_resurrected_on_restart(self):
        self.assertEqual(run_scenario("X05")[0], [])

    def test_C05_refused_start_not_resurrected_on_restart(self):
        self.assertEqual(run_scenario("C05")[0], [])

    # app/bot.py:526-541 → 218-223 — 만료·이미 사용·버전 변경 승인은 '요청 처리 실패: ConflictError' 로만 답한다
    def test_E01_expired_start_approval_clear_message(self):
        self.assertEqual(run_scenario("E01")[0], [])

    def test_E02_expired_merge_approval_clear_message(self):
        self.assertEqual(run_scenario("E02")[0], [])

    def test_R18_expired_retry_approval_clear_message(self):
        self.assertEqual(run_scenario("R18")[0], [])

    # app/bot.py:553 — 다른 작업 진행 중 착수 버튼: RunnerError 를 잡지 않아 '요청 처리 실패: RunnerError'
    def test_C04_busy_runner_clear_message(self):
        self.assertEqual(run_scenario("C04")[0], [])

    # app/bot.py:522-523 — answerCallbackQuery 실패(400 query too old: 봇이 꺼진 사이 누른 버튼)가 승인 처리 전체를 막는다
    def test_O06_answer_callback_failure_does_not_drop_press(self):
        self.assertEqual(run_scenario("O06")[0], [])

    # app/policy.py:212-221 — '/new unknownproj: …' 가 경고 없이 기본 프로젝트(deskmini)로 발주 카드가 된다
    def test_P03_unknown_project_prefix_not_silently_defaulted(self):
        self.assertEqual(run_scenario("P03")[0], [])

    # ~/agent-hub/bin/hub:169 — echo "$text" 라서 할 일 문구가 '-n'·'-e' 면 본문이 비거나 바뀐다
    def test_T13_hub_todo_dash_n_body_kept(self):
        self.assertEqual(run_scenario("T13")[1], [])

    # app/bot.py:302 — 첨부 처리 중 크래시 후 재생하면 같은 파일이 inbox 에 두 번 저장된다(update_id 중복 확인 없음)
    def test_X11_attachment_replay_not_duplicated(self):
        self.assertEqual(run_scenario("X11")[1], [])


if __name__ == "__main__":
    unittest.main()

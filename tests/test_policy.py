import unittest

from adapters.gemini_rest import Interpretation, InterpretationUnavailable
from app.policy import Policy


class UnavailableInterpreter:
    def interpret(self, user_text, known_projects):
        raise InterpretationUnavailable("offline")


class NullFieldInterpreter:
    def interpret(self, user_text, known_projects):
        return Interpretation.from_dict(
            {
                "intent": "create_task",
                "task_id": None,
                "project_id": "deskmini",
                "summary": "테스트",
                "objective": None,
                "output_format": None,
                "boundaries": [],
                "acceptance": [],
                "missing_fields": [],
            }
        )


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = Policy(UnavailableInterpreter(), ["deskmini"])

    def test_fixed_read_commands_work_without_llm(self) -> None:
        self.assertEqual(self.policy.decide("/status").intent, "status")
        self.assertEqual(self.policy.decide("지금 진행 상황 어때?").intent, "status")
        self.assertEqual(self.policy.decide("/tasks").intent, "list_tasks")

    def test_explicit_new_command_creates_confirmation(self) -> None:
        decision = self.policy.decide("/new 메모리 조회 API 구현")
        self.assertEqual(decision.intent, "create_task")
        self.assertTrue(decision.needs_confirmation)
        self.assertEqual(decision.project_id, "deskmini")

    def test_arbitrary_shell_is_not_a_command(self) -> None:
        decision = self.policy.decide("$ rm -rf /tmp/example")
        self.assertEqual(decision.intent, "unknown")

    def test_null_model_fields_receive_safe_defaults(self) -> None:
        policy = Policy(NullFieldInterpreter(), ["deskmini"])
        decision = policy.decide("테스트 기능 만들어줘")
        self.assertEqual(decision.specification["objective"], "테스트 기능 만들어줘")
        self.assertEqual(decision.specification["output_format"], "변경사항과 검증 결과")
        self.assertIn("병합·push·배포 제외", decision.specification["boundaries"])


if __name__ == "__main__":
    unittest.main()

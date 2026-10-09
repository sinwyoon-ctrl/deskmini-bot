import json
import unittest

from app.worker import classify_error, parse_claude_response


class WorkerTests(unittest.TestCase):
    def test_usage_limit_is_blocked_not_success(self) -> None:
        self.assertEqual(
            classify_error("You've hit your weekly limit · resets 3am"),
            "LIMIT_BLOCKED",
        )

    def test_normal_failure_is_execution_failed(self) -> None:
        self.assertEqual(classify_error("tool failed"), "EXECUTION_FAILED")

    def test_nested_secret_string_is_redacted_after_json_parsing(self) -> None:
        raw = json.dumps(
            {"is_error": True, "result": 'password="alpha beta gamma"'},
            ensure_ascii=False,
        )
        response, valid = parse_claude_response(raw)
        self.assertTrue(valid)
        self.assertNotIn("alpha", response["result"])
        self.assertNotIn("beta", response["result"])
        self.assertNotIn("gamma", response["result"])

    def test_invalid_json_is_not_returned_for_storage(self) -> None:
        response, valid = parse_claude_response('password="alpha beta gamma"')
        self.assertFalse(valid)
        self.assertEqual(response, {})


if __name__ == "__main__":
    unittest.main()

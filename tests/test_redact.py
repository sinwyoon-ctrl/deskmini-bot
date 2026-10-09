import unittest

from app.redact import redact, redact_text


class RedactTests(unittest.TestCase):
    def test_known_secret_shapes_are_removed(self) -> None:
        source = "token=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_123456 api=AIza" + "A" * 30
        redacted = redact_text(source)
        self.assertNotIn("123456789:", redacted)
        self.assertNotIn("AIza", redacted)

    def test_secret_keys_are_removed_recursively(self) -> None:
        value = {"token": "abc", "nested": {"password": "pw", "safe": "ok"}}
        self.assertEqual(
            redact(value),
            {"token": "<redacted>", "nested": {"password": "<redacted>", "safe": "ok"}},
        )

    def test_usage_counts_and_session_ids_are_not_mistaken_for_secrets(self) -> None:
        value = {"input_tokens": 123, "session_id": "session-reference", "api_key": "secret"}
        self.assertEqual(
            redact(value),
            {"input_tokens": 123, "session_id": "session-reference", "api_key": "<redacted>"},
        )

    def test_assignment_and_openai_style_secret_are_redacted(self) -> None:
        value = "password=hunter2 token: abcdefghijklmnop sk-abcdefghijklmnop"
        cleaned = redact_text(value)
        self.assertNotIn("hunter2", cleaned)
        self.assertNotIn("abcdefghijklmnop", cleaned)

    def test_quoted_secret_and_truncated_private_key_are_fully_redacted(self) -> None:
        quoted = redact_text('password="alpha beta" next=ok')
        private_key = "-----BEGIN PRIVATE KEY-----\n" + "A" * 500
        truncated = redact_text(private_key, 60)
        self.assertNotIn("alpha", quoted)
        self.assertNotIn("beta", quoted)
        self.assertNotIn("AAAA", truncated)

    def test_json_string_secret_is_redacted(self) -> None:
        cleaned = redact_text('{"password": "alpha beta", "safe": "ok"}')
        self.assertNotIn("alpha", cleaned)
        self.assertNotIn("beta", cleaned)
        self.assertIn('"safe": "ok"', cleaned)

    def test_json_escaped_quote_does_not_leak_secret_tail(self) -> None:
        cleaned = redact_text(r'{"password":"alpha\" beta gamma","safe":"ok"}')
        self.assertNotIn("alpha", cleaned)
        self.assertNotIn("beta", cleaned)
        self.assertNotIn("gamma", cleaned)
        self.assertIn('"safe":"ok"', cleaned)


if __name__ == "__main__":
    unittest.main()

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2

from adapters.claude_api import ClaudeChatClient, ClaudeResult, ClaudeUnavailable
from adapters.gemini_rest import InterpretationUnavailable
from app.bot import ChiefBot
from app.chat import (
    FALLBACK_TEXT,
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    ChatAction,
    ChatConfig,
    ChatResponder,
    ChatUnavailable,
    claude_messages,
)
from app.config import ProjectConfig, Settings
from app.context import WorkspacePaths, build_snapshot
from app.policy import Policy
from app.state import StateStore


ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "test-key-not-real"


def reply(text="안녕하세요", action=None):
    return {"reply": text, "action": action or {"type": "none"}}


class FakeGemini:
    model = "auto"

    def __init__(self, data=None, error=None):
        self.data = data or reply("gemini 답변")
        self.error = error
        self.calls = []

    def available(self):
        return True

    def chat(self, envelope):
        self.calls.append(envelope)
        if self.error:
            raise self.error
        return self.data, "gemini-test"

    def interpret(self, *_):
        raise InterpretationUnavailable("not used")


class FakeClaude:
    def __init__(self, data=None, error=None):
        self.data = data or reply("claude 답변")
        self.error = error
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return ClaudeResult(data=self.data, model="claude-sonnet-5-5", usage={"input_tokens": 10})


class FakeResponder:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def respond(self, text, history, snapshot):
        if self.error:
            raise self.error
        return self.result

    def active_providers(self):
        return [("gemini", "auto")]


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env_path = self.root / "notify.env"
        self.env_path.write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def responder(self, gemini=None, claude=None):
        return ChatResponder(
            ChatConfig(),
            env_path=self.env_path,
            gemini=gemini or FakeGemini(),
            claude=claude or FakeClaude(),
        )

    def add_key(self):
        with self.env_path.open("a", encoding="utf-8") as handle:
            handle.write(f"ANTHROPIC_API_KEY={FAKE_KEY}\n")


class ProviderSelectionTests(Base):
    def test_without_key_uses_gemini(self):
        claude = FakeClaude()
        result = self.responder(claude=claude).respond("안녕", [], "{}")
        self.assertEqual(result.provider, "gemini")
        self.assertEqual(claude.calls, [])
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)

    def test_key_added_later_switches_without_restart(self):
        claude = FakeClaude()
        responder = self.responder(claude=claude)
        self.assertEqual(responder.respond("안녕", [], "{}").provider, "gemini")
        self.add_key()
        result = responder.respond("안녕", [], "{}")
        self.assertEqual(result.provider, "claude")
        self.assertEqual(claude.calls[0]["api_key"], FAKE_KEY)
        self.assertEqual(claude.calls[0]["system_prompt"], SYSTEM_PROMPT)
        self.assertEqual(responder.active_providers()[0], ("claude", "claude-sonnet-5-5"))
        self.assertNotIn("ANTHROPIC_API_KEY", os.environ)

    def test_claude_error_falls_back_to_gemini(self):
        self.add_key()
        result = self.responder(claude=FakeClaude(error=ClaudeUnavailable("RateLimitError 429"))).respond(
            "안녕", [], "{}"
        )
        self.assertEqual(result.provider, "gemini")
        self.assertIn("RateLimitError", result.errors[0])

    def test_both_fail_raises_unavailable(self):
        self.add_key()
        responder = self.responder(
            gemini=FakeGemini(error=InterpretationUnavailable("429")),
            claude=FakeClaude(error=ClaudeUnavailable("APIConnectionError")),
        )
        with self.assertRaises(ChatUnavailable) as caught:
            responder.respond("안녕", [], "{}")
        self.assertEqual(len(caught.exception.errors), 2)

    def test_empty_reply_counts_as_failure(self):
        responder = self.responder(gemini=FakeGemini(data={"reply": "", "action": {}}))
        with self.assertRaises(ChatUnavailable):
            responder.respond("안녕", [], "{}")

    def test_invalid_action_is_ignored(self):
        action = ChatAction.from_dict({"type": "rm -rf", "task_id": "../etc"})
        self.assertEqual(action.type, "none")
        self.assertIsNone(action.task_id)

    def test_history_becomes_alternating_messages(self):
        history = [
            {"role": "assistant", "text": "고아 답변"},
            {"role": "user", "text": "질문1"},
            {"role": "assistant", "text": "답1"},
            {"role": "user", "text": "질문2"},
        ]
        messages = claude_messages(history, "질문3")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertTrue(messages[-1]["content"].endswith("질문3"))


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


class ClaudeClientTests(unittest.TestCase):
    def client(self, messages):
        self.keys = []

        def factory(api_key):
            self.keys.append(api_key)
            return SimpleNamespace(messages=messages)

        return ClaudeChatClient(model="claude-sonnet-5-5", client_factory=factory)

    def test_request_shape(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"reply": "네", "action": {"type": "none"}}')],
            stop_reason="end_turn",
            model="claude-sonnet-5-5",
            usage=SimpleNamespace(input_tokens=5, output_tokens=3, cache_read_input_tokens=0),
        )
        messages = FakeMessages(response)
        result = self.client(messages).complete(
            api_key=FAKE_KEY,
            system_prompt=SYSTEM_PROMPT,
            context_text="ctx",
            messages=[{"role": "user", "content": "hi"}],
            schema=RESPONSE_SCHEMA,
        )
        self.assertEqual(result.data["reply"], "네")
        self.assertEqual(self.keys, [FAKE_KEY])
        kwargs = messages.kwargs
        self.assertNotIn("thinking", kwargs)
        self.assertNotIn("tool_choice", kwargs)
        self.assertEqual(kwargs["output_config"]["effort"], "low")
        self.assertEqual(kwargs["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(kwargs["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", kwargs["system"][1])
        self.assertEqual(result.usage["input_tokens"], 5)

    def test_sdk_errors_become_unavailable(self):
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        messages = FakeMessages(error=anthropic.APIConnectionError(request=request))
        with self.assertRaises(ClaudeUnavailable):
            self.client(messages).complete(
                api_key=FAKE_KEY,
                system_prompt="s",
                context_text="c",
                messages=[],
                schema=RESPONSE_SCHEMA,
            )


class WorkspaceFixture(Base):
    def setUp(self):
        super().setUp()
        home = self.root / "home"
        hub = home / "agent-hub"
        (hub / "locks").mkdir(parents=True)
        (hub / "log" / "2026-10").mkdir(parents=True)
        (hub / "hosts").mkdir(parents=True)
        (hub / "BOARD.md").write_text("# BOARD\n- deskmini-bot: 진행 중 & <test>\n", encoding="utf-8")
        (hub / "locks" / "proj.lock").write_text("codex ionenet 작업 중", encoding="utf-8")
        for index in range(7):
            entry = hub / "log" / "2026-10" / f"2026100{index}-000000-x.md"
            entry.write_text(f"기록 {index}", encoding="utf-8")
            os.utime(entry, (1_000_000 + index, 1_000_000 + index))
        repo = home / "repo"
        repo.mkdir()
        git = ("git", "-C", str(repo))
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run((*git, "init", "-q"), check=True, env=env)
        (repo / "a.txt").write_text("a", encoding="utf-8")
        subprocess.run((*git, "add", "a.txt"), check=True, env=env)
        subprocess.run((*git, "commit", "-qm", "첫 커밋"), check=True, env=env)
        (repo / "b.txt").write_text("b", encoding="utf-8")
        (hub / "hosts" / "ionenet.conf").write_text(
            "# comment\nrepo $HOME/repo auto\nmissing $HOME/missing warn\n", encoding="utf-8"
        )
        (home / "work" / "old-project").mkdir(parents=True)
        (home / "work" / "hero-final").mkdir()
        os.utime(home / "work" / "old-project", (1_000_000, 1_000_000))
        self.paths = WorkspacePaths(home=home, agent_hub=hub, work=home / "work")

        state_dir = self.root / "state"
        project = ProjectConfig(
            project_id="deskmini",
            repo_path=repo,
            worktree_root=state_dir / "worktrees",
            default_branch="main",
            checks=(),
            deploy_enabled=False,
            deploy_environment=None,
            deploy_command=None,
            healthcheck_command=None,
            rollback_command=None,
            deployed_version_command=None,
        )
        self.settings = Settings(
            root=ROOT,
            state_dir=state_dir,
            db_path=state_dir / "chief.sqlite",
            telegram_token="test-token",
            allowed_chat_id=100,
            allowed_user_id=100,
            telegram_timeout_seconds=1,
            gemini_script=self.root / "missing-gemini.py",
            max_runtime_seconds=60,
            max_active_tasks=1,
            approval_ttl_seconds=600,
            projects={"deskmini": project},
            env_path=self.env_path,
        )
        self.bot = ChiefBot(self.settings)
        self.bot.paths = self.paths
        self.bot.telegram = FakeTelegram()
        self.bot.executor.shutdown(wait=False)

    def chat_result(self, text, action=None, provider="gemini"):
        from app.chat import ChatResult

        return ChatResult(
            reply=text,
            action=ChatAction.from_dict(action or {"type": "none"}),
            provider=provider,
            model="test-model",
            latency_ms=1,
        )

    def message(self, text):
        return {"chat": {"type": "private", "id": 100}, "from": {"id": 100}, "text": text}


class BotChatTests(WorkspaceFixture):
    def test_reply_is_escaped_and_redacted(self):
        secret = "sk-ant-api03-" + "A" * 30
        self.bot.chat = FakeResponder(self.chat_result(f"<b>굵게</b> 키는 {secret}"))
        self.bot._handle_message(self.message("키 알려줘"), 1)
        sent = self.bot.telegram.sent[-1]["text"]
        self.assertIn("&lt;b&gt;굵게&lt;/b&gt;", sent)
        self.assertNotIn(secret, sent)
        stored = self.bot.store.recent_chat_messages(100)
        self.assertEqual([item["role"] for item in stored], ["user", "assistant"])
        self.assertNotIn(secret, stored[-1]["text"])
        event = self.bot.store.recent_events_by_type("CHAT_DECISION", 1)[0]["payload"]
        self.assertEqual(event["provider"], "gemini")
        self.assertEqual(event["proposed_action"], "none")

    def test_create_task_action_only_yields_approval_card(self):
        action = {
            "type": "create_task",
            "project_id": None,
            "objective": "메모리 조회 API 추가",
            "output_format": "변경사항",
            "boundaries": ["배포 제외"],
            "acceptance": ["테스트 통과"],
        }
        self.bot.chat = FakeResponder(self.chat_result("발주 카드를 띄울게요.", action))
        started = []
        self.bot.runner.start = lambda task_id: started.append(task_id)
        self.bot._handle_message(self.message("메모리 API 만들어줘"), 2)
        self.assertEqual(started, [])
        card = self.bot.telegram.sent[-1]
        self.assertIn("작업 발주 확인", card["text"])
        buttons = card["reply_markup"]["inline_keyboard"]
        self.assertTrue(buttons[0][0]["callback_data"].startswith("approve:start:"))
        task = self.bot.store.list_tasks(1)[0]
        self.assertEqual(task["state"], "AWAITING_START")
        self.assertEqual(task["project_id"], "deskmini")

    def test_create_task_for_unregistered_project_is_dropped(self):
        action = {"type": "create_task", "project_id": "other-repo", "objective": "README 수정"}
        self.bot.chat = FakeResponder(self.chat_result("other-repo 는 등록되지 않았어요.", action))
        self.bot._handle_message(self.message("other-repo README 고쳐줘"), 3)
        self.assertEqual(len(self.bot.telegram.sent), 1)
        self.assertIsNone(self.bot.telegram.sent[-1]["reply_markup"])
        self.assertEqual(self.bot.store.list_tasks(5), [])

    def _task_in_state(self, state):
        task = self.bot.store.create_task(
            owner_user_id=100,
            chat_id=100,
            project_id="deskmini",
            request_text="x",
            specification={"objective": "x"},
            policy_hash="h",
            budget={"runtime_seconds": 60, "retries": 1},
            source_update_id=None,
        )
        with self.bot.store.connect() as connection:
            connection.execute("UPDATE tasks SET state=? WHERE id=?", (state, task["id"]))
        return task["id"]

    def test_stop_action_for_missing_or_finished_task_sends_note(self):
        stopped = []
        self.bot._stop_task = lambda task_id: stopped.append(task_id)
        finished = self._task_in_state("CANCELLED")
        for task_id in ("T-20991231-ffffff", finished):
            self.bot.chat = FakeResponder(
                self.chat_result("중단할까요?", {"type": "stop_task", "task_id": task_id})
            )
            self.bot._handle_message(self.message("그거 멈춰"), None)
            note = self.bot.telegram.sent[-1]
            self.assertIsNone(note["reply_markup"])
            self.assertIn("해당 작업이 없거나 이미 끝났습니다", note["text"])
            self.assertIn(task_id, note["text"])
        self.assertEqual(stopped, [])

    def test_deploy_disabled_refuses_without_card(self):
        task_id = self._task_in_state("READY")
        with self.bot.store.connect() as connection:
            connection.execute("UPDATE tasks SET head_sha=?, base_sha=? WHERE id=?", ("a" * 40, "b" * 40, task_id))
        self.bot._offer_release(task_id, "deploy")
        sent = self.bot.telegram.sent[-1]
        self.assertIsNone(sent["reply_markup"])
        self.assertIn("배포가 꺼져", sent["text"])
        with self.bot.store.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM approvals WHERE task_id=?", (task_id,)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_release_action_goes_through_offer_release(self):
        offered = []
        self.bot._offer_release = lambda task_id, action: offered.append((task_id, action))
        for kind in ("merge", "push", "deploy"):
            self.bot.chat = FakeResponder(
                self.chat_result("승인 카드를 띄웁니다.", {"type": kind, "task_id": "T-20261009-abcdef"})
            )
            self.bot._handle_message(self.message(f"{kind} 해줘"), None)
        self.assertEqual(
            offered,
            [("T-20261009-abcdef", "merge"), ("T-20261009-abcdef", "push"), ("T-20261009-abcdef", "deploy")],
        )

    def test_release_action_without_task_id_does_nothing(self):
        offered = []
        self.bot._offer_release = lambda task_id, action: offered.append((task_id, action))
        self.bot.chat = FakeResponder(self.chat_result("어떤 작업인가요?", {"type": "merge", "task_id": None}))
        self.bot._handle_message(self.message("병합해"), None)
        self.assertEqual(offered, [])

    def test_stop_action_requires_button(self):
        stopped = []
        self.bot._stop_task = lambda task_id: stopped.append(task_id)
        task_id = self._task_in_state("RUNNING")
        self.bot.chat = FakeResponder(
            self.chat_result("중단할까요?", {"type": "stop_task", "task_id": task_id})
        )
        self.bot._handle_message(self.message("그거 멈춰"), None)
        self.assertEqual(stopped, [])
        markup = self.bot.telegram.sent[-1]["reply_markup"]
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], f"stop:{task_id}")

    def test_both_providers_down_sends_command_help(self):
        self.bot.chat = FakeResponder(error=ChatUnavailable(("claude: x", "gemini: y")))
        self.bot._handle_message(self.message("작업 중인거 있을텐데"), None)
        self.assertIn("/status", self.bot.telegram.sent[-1]["text"])
        self.assertIn("대화형 응답을 만들 수 없습니다", self.bot.telegram.sent[-1]["text"])
        self.assertEqual(self.bot.store.recent_events_by_type("CHAT_UNAVAILABLE", 1)[0]["payload"]["errors"], ["claude: x", "gemini: y"])

    def test_fixed_commands_never_call_llm(self):
        self.bot.chat = FakeResponder(error=AssertionError("LLM must not be called"))
        for text in ("/new", "/task", "/stop", "/hub", "/git", "/work", "/model", "/help", "/unknown"):
            self.bot._handle_message(self.message(text), None)
        self.assertEqual(len(self.bot.telegram.sent), 9)


class CommandOutputTests(WorkspaceFixture):
    def test_hub(self):
        text = self.bot._hub_text()
        self.assertIn("proj: codex ionenet 작업 중", text)
        self.assertIn("기록 6", text)
        self.assertNotIn("기록 1", text)
        self.assertIn("&amp; &lt;test&gt;", text)
        self.assertLessEqual(len(text), 4_096)

    def test_git(self):
        text = self.bot._git_text()
        self.assertIn("첫 커밋", text)
        self.assertIn("미커밋 변경: 1개", text)
        self.assertIn("<b>missing</b>", text)
        self.assertIn("미확인", text)

    def test_work(self):
        text = self.bot._work_text()
        self.assertLess(text.index("hero-final"), text.index("old-project"))

    def test_model_hides_key(self):
        self.bot.chat = ChatResponder(ChatConfig(), env_path=self.env_path, gemini=FakeGemini(), claude=FakeClaude())
        self.assertIn("Gemini", self.bot._model_text())
        self.add_key()
        text = self.bot._model_text()
        self.assertIn("Claude API", text)
        self.assertIn("claude-sonnet-5-5", text)
        self.assertNotIn(FAKE_KEY, text)

    def test_snapshot_is_compact_and_contains_sources(self):
        snapshot = build_snapshot(self.bot.store, self.paths)
        for marker in ("bot_tasks", "agent_hub_board", "첫 커밋", "hero-final", "기록 6"):
            self.assertIn(marker, snapshot)
        self.assertLessEqual(len(snapshot), 7_000)

    def test_snapshot_lists_registered_projects(self):
        snapshot = build_snapshot(self.bot.store, self.paths, self.settings.projects)
        data = json.loads(snapshot)
        self.assertEqual(data["projects"], {"registered": {"deskmini": "~/repo"}, "only": "deskmini"})
        self.assertNotIn("projects", json.loads(build_snapshot(self.bot.store, self.paths)))

    def test_long_output_is_truncated_for_telegram(self):
        (self.paths.agent_hub / "BOARD.md").write_text("줄\n" * 5_000, encoding="utf-8")
        for index in range(30):
            (self.paths.agent_hub / "locks" / f"l{index}.lock").write_text("x" * 190, encoding="utf-8")
        text = self.bot._hub_text()
        self.assertLessEqual(len(text), 4_096)
        self.assertEqual(text.count("<pre>"), text.count("</pre>"))


class ChatMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "chief.sqlite", ROOT / "schema.sql")

    def tearDown(self):
        self.temp.cleanup()

    def test_recent_messages_in_order_and_pruned(self):
        for index in range(205):
            self.store.add_chat_message(1, "user" if index % 2 == 0 else "assistant", f"m{index}")
        self.store.add_chat_message(2, "user", "other chat")
        recent = self.store.recent_chat_messages(1, 10)
        self.assertEqual([item["text"] for item in recent], [f"m{i}" for i in range(195, 205)])
        with self.store.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM chat_messages WHERE chat_id=1").fetchone()[0]
        self.assertEqual(count, 200)

    def test_messages_are_redacted(self):
        self.store.add_chat_message(1, "user", "token=abcdefghijklmnop1234")
        self.assertNotIn("abcdefghijklmnop1234", self.store.recent_chat_messages(1)[0]["text"])

    def test_rejects_unknown_role(self):
        with self.assertRaises(ValueError):
            self.store.add_chat_message(1, "system", "x")


class PolicyChatTests(unittest.TestCase):
    def setUp(self):
        self.policy = Policy(FakeGemini(), ["deskmini"])

    def test_bare_commands_return_korean_usage(self):
        for command in ("/new", "/task", "/stop", "/task 아무거나"):
            decision = self.policy.decide_fixed(command)
            self.assertEqual(decision.intent, "usage")
            self.assertNotIn("objective", decision.message)

    def test_read_commands(self):
        for command, intent in (("/hub", "hub"), ("/git", "git"), ("/work", "work"), ("/model", "model")):
            self.assertEqual(self.policy.decide_fixed(command).intent, intent)
        self.assertEqual(self.policy.decide_fixed("/status@deskmini_bot").intent, "status")

    def test_natural_language_goes_to_chat(self):
        self.assertIsNone(self.policy.decide_fixed("홈페이지 모션 그래픽 작업 명령 대기중이야?"))

    def test_create_action_without_objective_is_dropped(self):
        self.assertIsNone(self.policy.from_chat_action(ChatAction(type="create_task"), "만들어줘"))

    def test_create_action_project_resolution(self):
        missing = self.policy.from_chat_action(ChatAction(type="create_task", objective="x"), "x")
        self.assertEqual(missing.project_id, "deskmini")
        unknown = ChatAction(type="create_task", objective="x", project_id="other")
        self.assertIsNone(self.policy.from_chat_action(unknown, "x"))
        multi = Policy(FakeGemini(), ["deskmini", "other"])
        self.assertIsNone(multi.from_chat_action(ChatAction(type="create_task", objective="x"), "x"))
        self.assertEqual(multi.from_chat_action(unknown, "x").project_id, "other")


class PromptRulesTests(unittest.TestCase):
    def test_server_rules_are_in_system_prompt(self):
        for phrase in (
            "운영/관리자 사이트는 읽기 전용",
            "스테이징만",
            "portainer·deskmini",
            "win11",
            "sudo",
            "force push",
            "브랜치 삭제",
            "폴더 통째 삭제",
            "비밀값",
            "직접 하시려면",
            "250자",
            "질문 하나",
            "등록 프로젝트가 하나뿐이면",
        ):
            self.assertIn(phrase, SYSTEM_PROMPT)

    def test_fallback_text_lists_commands(self):
        self.assertIn("/status", FALLBACK_TEXT)


if __name__ == "__main__":
    unittest.main()

"""처리 구분(A~E)·넘기는 일(Codex 시각 요청)·발주 프로젝트 확인·작업자 MCP/커넥터 차단."""
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from adapters.claude_cli import ClaudeCliReviewer
from app.chat import RESPONSE_SCHEMA, SYSTEM_PROMPT, ChatAction, ChatResult
from app.config import load_projects
from app.policy import Decision, Policy
from app.todo import HubResult
from app.visual import (
    card_conclusion,
    create_visual_request,
    handoff_status,
    is_visual_deliverable,
)
from app.worker import NO_MCP_ARGS, claude_sandbox, worker_claude_argv

from test_watch import BotFixture


ROOT = Path(__file__).resolve().parents[1]
REAL_REQUEST = "아이원네트웍스 홈페이지 온라인 교육 메뉴 부분에 사용할 모션 그래픽 제작을 하자"
DESCRIPTIONS = {"deskmini": "DeskMini 상태 API 서버(Fastify, /health 등) 코드. 웹사이트·브랜드 자산·영상 없음",
                "sandbox": "봇 처리 경로 시험용 연습 저장소(파이썬 예제·테스트)"}


class FakeHubTodo:
    """hub todo add 를 흉내 낸다: todo/<id>.md 를 만들고 '할 일: <id>' 를 돌려준다."""

    def __init__(self, hub_root: Path, ok: bool = True):
        self.hub_root = hub_root
        self.ok = ok
        self.calls = []

    def available(self):
        return True

    def add(self, assignee, text):
        self.calls.append((assignee, text))
        if not self.ok:
            return HubResult(False, "커밋 실패")
        todo_id = f"20261009-1200{len(self.calls):02d}-ionenet-{assignee}"
        (self.hub_root / "todo").mkdir(parents=True, exist_ok=True)
        (self.hub_root / "todo" / f"{todo_id}.md").write_text(f"assignee: {assignee}\n\n{text}\n")
        return HubResult(True, f"할 일: {todo_id}")

    def list(self):
        return HubResult(True, "1. [codex] x")


class Responder:
    def __init__(self, action, handling=None, reply="확인할게요"):
        self.action, self.handling, self.reply = action, handling, reply

    def respond(self, text, history, snapshot):
        return ChatResult(reply=self.reply, action=ChatAction.from_dict(self.action), provider="gemini", model="m",
                          latency_ms=1, handling=self.handling)

    def active_providers(self):
        return []


class NoMcpArgvTests(unittest.TestCase):
    def test_worker_argv_disables_mcp_and_claude_ai_connectors(self):
        argv = worker_claude_argv("/usr/bin/claude", "sonnet")
        for flag in NO_MCP_ARGS:
            self.assertIn(flag, argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn('--mcp-config={"mcpServers":{}}', argv)
        settings = next(item for item in argv if item.startswith("--settings="))
        self.assertEqual(json.loads(settings.split("=", 1)[1]), {"disableClaudeAiConnectors": True})
        self.assertIn("--restricted", argv)

    def test_sandbox_env_disables_claude_ai_mcp_servers(self):
        argv = claude_sandbox(workdir=Path("/home/u/w"), home=Path("/home/u"), credentials=Path("/home/u/.claude/.credentials.json"))
        index = argv.index("ENABLE_CLAUDEAI_MCP_SERVERS")
        self.assertEqual((argv[index - 1], argv[index + 1]), ("--setenv", "false"))

    def test_reviewer_argv_disables_mcp_and_connectors(self):
        with tempfile.TemporaryDirectory() as temp:
            argv = ClaudeCliReviewer(home=Path(temp)).argv(Path(temp) / "w", "deskmini-chief-r-x")
        for flag in NO_MCP_ARGS:
            self.assertIn(flag, argv)


class VisualHeuristicTests(unittest.TestCase):
    def test_visual_deliverables(self):
        for text in (REAL_REQUEST, "메인 배너 디자인 시안 4개", "로고 이미지 만들어줘", "hero 영상 렌더", "make a promo video",
                     "제품 소개 애니메이션"):
            self.assertTrue(is_visual_deliverable(text), text)

    def test_code_work_is_not_visual(self):
        for text in ("CSS 애니메이션 추가", "이미지 업로드 API 추가", "프로모션 문구 수정", "README 오타 고쳐",
                     "렌더 스크립트 버그 수정", "/health 응답에 버전 추가"):
            self.assertFalse(is_visual_deliverable(text), text)


class PolicyHandoffTests(unittest.TestCase):
    def setUp(self):
        self.policy = Policy(None, ["deskmini", "sandbox"], DESCRIPTIONS)

    def test_new_visual_request_is_not_a_task_card(self):
        decision = self.policy.decide_fixed("/new " + REAL_REQUEST)
        self.assertEqual(decision.intent, "visual_suggest")
        self.assertIn("/visual", decision.message)

    def test_chat_create_task_with_visual_objective_is_refused(self):
        action = ChatAction.from_dict({"type": "create_task", "objective": "온라인 교육 메뉴용 모션 그래픽 제작",
                                       "output_format": "mp4 영상"})
        self.assertEqual(self.policy.from_chat_action(action, REAL_REQUEST).intent, "visual_suggest")

    def test_chat_visual_request_and_command(self):
        action = ChatAction.from_dict({"type": "visual_request", "objective": "교육 메뉴 모션 그래픽", "project_id": "ionenet-hero"})
        decision = self.policy.from_chat_action(action, REAL_REQUEST)
        self.assertEqual((decision.intent, decision.project_id, decision.argument), ("visual_request", "ionenet-hero", "교육 메뉴 모션 그래픽"))
        self.assertEqual(self.policy.decide_fixed("/visual ionenet-hero: 2안 다듬기"),
                         Decision(intent="visual_request", project_id="ionenet-hero", argument="2안 다듬기"))
        self.assertEqual(self.policy.decide_fixed("/visual 배너 시안").intent, "visual_request")
        self.assertEqual(self.policy.decide_fixed("/visual").intent, "usage")

    def test_site_work_not_matching_any_project_asks_which_project(self):
        decision = self.policy.decide_fixed("/new 홈페이지 온라인 교육 메뉴 문구 수정")
        self.assertEqual(decision.intent, "ask_project")
        self.assertIn("deskmini — DeskMini 상태 API 서버", decision.message)
        self.assertIn("projects.toml", decision.message)
        chat = ChatAction.from_dict({"type": "create_task", "objective": "홈페이지 메뉴 문구 수정"})
        self.assertEqual(self.policy.from_chat_action(chat, "홈페이지 메뉴 문구 고쳐줘").intent, "ask_project")

    def test_explicit_project_or_matching_description_does_not_ask(self):
        self.assertEqual(self.policy.decide_fixed("/new deskmini: 홈페이지 링크 설명 추가").project_id, "deskmini")
        site = Policy(None, ["web", "deskmini"], {"web": "회사 홈페이지(정적 사이트) 코드", "deskmini": "API"})
        self.assertEqual(site.decide_fixed("/new 홈페이지 메뉴 문구 수정").intent, "create_task")
        self.assertEqual(self.policy.decide_fixed("/new 메모리 API 추가").project_id, "deskmini")

    def test_registry_descriptions_load(self):
        with tempfile.TemporaryDirectory() as temp:
            projects = load_projects(ROOT / "config" / "projects.toml", Path(temp))
        self.assertIn("Fastify", projects["deskmini"].description)
        self.assertTrue(projects["sandbox"].description)


class VisualRequestFileTests(unittest.TestCase):
    def test_creates_request_skeleton_and_codex_todo(self):
        with tempfile.TemporaryDirectory() as temp:
            hub = Path(temp)
            todo = FakeHubTodo(hub)
            now = datetime(2026, 10, 9, 21, 0, tzinfo=UTC)
            first = create_visual_request(hub, todo, REAL_REQUEST + " token=abcdefghijklmnop", None, now)
            second = create_visual_request(hub, todo, "배너 시안", "ionenet-hero", now)
            bad = create_visual_request(hub, todo, "x", "../etc", now)
            self.assertTrue(first.ok)
            self.assertEqual(first.request_id, "general-20261009-01")
            self.assertEqual(first.path, "visual/general/general-20261009-01/request.md")
            self.assertEqual(second.request_id, "ionenet-hero-20261009-01")
            self.assertEqual(bad.request_id, "general-20261009-02")  # 경로 같은 이름은 general 로
            text = (hub / first.path).read_text()
            self.assertIn("kind: concept", text)
            self.assertIn("from: telegram", text)
            self.assertIn("모션 그래픽", text)
            self.assertNotIn("abcdefghijklmnop", text)
            self.assertTrue((hub / "visual/general/general-20261009-01/inputs").is_dir())
            assignee, line = todo.calls[0]
            self.assertEqual(assignee, "codex")
            self.assertIn("general-20261009-01", line)
            self.assertEqual(first.todo_id, "20261009-120001-ionenet-codex")

    def test_status_and_card_conclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            hub = Path(temp)
            handoff = create_visual_request(hub, FakeHubTodo(hub), "배너 시안", None, datetime(2026, 10, 9, tzinfo=UTC))
            item = {"path": handoff.path, "todo_id": handoff.todo_id}
            self.assertEqual(handoff_status(hub, item), "open")
            (hub / "todo" / "done" / "2026-10").mkdir(parents=True)
            (hub / "todo" / f"{handoff.todo_id}.md").rename(hub / "todo" / "done" / "2026-10" / f"{handoff.todo_id}.md")
            self.assertEqual(handoff_status(hub, item), "done")
            card = (hub / handoff.path).with_name("card.md")
            card.write_text("---\nverdict: 수정 후 채택\n---\n\n# 결론 (두 줄 이내)\nB안이 메뉴 리듬에 가장 맞다.\n\n## 무엇을 봤나\n- x\n")
            self.assertEqual(handoff_status(hub, item), "card")
            self.assertEqual(card_conclusion(card), "B안이 메뉴 리듬에 가장 맞다. (수정 후 채택)")
            self.assertEqual(handoff_status(hub, {"path": "../../etc/request.md", "todo_id": "../x"}), "unknown")


class BotHandoffTests(BotFixture):
    def setUp(self):
        super().setUp()
        self.hub = self.bot.paths.agent_hub
        self.hub.mkdir(parents=True)
        self.bot._hub_todo = FakeHubTodo(self.hub)
        self.update = 300

    def say(self, text, action, handling=None):
        self.bot.chat = Responder(action, handling)
        self.update += 1
        self.bot._handle_update(self.message(text, update_id=self.update))

    def last_decision(self):
        return self.bot.store.recent_events_by_type("CHAT_DECISION", 1)[0]["payload"]

    def test_real_failure_sentence_does_not_create_deskmini_task(self):
        self.say(REAL_REQUEST, {"type": "create_task", "objective": "온라인 교육 메뉴 모션 그래픽 제작"})
        self.assertEqual(self.bot.store.list_tasks(5), [])
        self.assertIn("시각 결과물", self.tg.sent[-1]["text"])
        self.assertEqual(self.last_decision()["handling_class"], "C")

    def test_class_c_visual_request_reports_who_id_and_return_path_then_notifies(self):
        self.say(REAL_REQUEST, {"type": "visual_request", "objective": "온라인 교육 메뉴 모션 그래픽"}, "C")
        text = self.tg.sent[-1]["text"]
        self.assertIn("Codex 에게 시안 요청을 넘겼습니다", text)
        self.assertIn("general-", text)
        self.assertIn("20261009-120001-ionenet-codex", text)
        self.assertIn("판단 카드", text)
        self.assertEqual(self.last_decision()["handling_class"], "C")
        self.assertEqual(self.bot.store.list_tasks(5), [])
        # /todo · /hub 에 상태가 보인다
        self.update += 1
        self.bot._handle_update(self.message("/todo", update_id=self.update))
        self.assertIn("봇이 넘긴 일", self.tg.sent[-1]["text"])
        self.assertIn("진행 전/중", self.tg.sent[-1]["text"])
        self.assertIn("봇이 넘긴 일", self.bot._hub_text())
        # 카드가 생기면 결론 한 줄을 한 번만 알린다
        request_dir = next((self.hub / "visual" / "general").iterdir())
        (request_dir / "card.md").write_text("---\nverdict: 채택 권장\n---\n# 결론\nA안 채택.\n")
        before = len(self.tg.sent)
        self.bot._check_handoffs()
        self.bot._check_handoffs()
        notes = [m["text"] for m in self.tg.sent[before:]]
        self.assertEqual(len(notes), 1)
        self.assertIn("A안 채택.", notes[0])

    def test_closed_todo_is_reported_once(self):
        self.update += 1
        self.bot._handle_update(self.message("/todo codex 로그인 문구 정리", update_id=self.update))
        todo_id = "20261009-120001-ionenet-codex"
        self.assertIn(todo_id, self.tg.sent[-1]["text"])
        (self.hub / "todo" / "done" / "2026-10").mkdir(parents=True)
        (self.hub / "todo" / f"{todo_id}.md").rename(self.hub / "todo" / "done" / "2026-10" / f"{todo_id}.md")
        before = len(self.tg.sent)
        self.bot._check_handoffs()
        self.bot._check_handoffs()
        self.assertEqual(len(self.tg.sent) - before, 1)
        self.assertIn("닫았습니다", self.tg.sent[-1]["text"])

    def test_handling_class_examples(self):
        self.say("서버 상태 어때?", {"type": "none"}, "A")
        self.assertEqual(self.last_decision()["handling_class"], "A")
        self.say("영상 보내줘", {"type": "send_files", "job_hint": "full-v1"}, "A")
        self.assertEqual(self.last_decision()["handling_class"], "A")
        self.say("메모리 API 추가해줘", {"type": "create_task", "objective": "메모리 조회 API 추가"}, "B")
        self.assertEqual(self.last_decision()["handling_class"], "B")
        self.say("codex 한테 문구 정리 남겨", {"type": "dispatch_todo", "assignee": "codex", "todo_text": "문구 정리"}, "C")
        self.assertEqual(self.last_decision()["handling_class"], "C")
        self.say("깃허브 저장소 새로 만들어줘", {"type": "none"}, "D")
        self.assertEqual(self.last_decision()["handling_class"], "D")
        self.say("운영 사이트 글 지워줘", {"type": "none"}, "E")
        self.assertEqual(self.last_decision()["handling_class"], "E")
        # 모델이 B 라고 해도 받아들인 동작이 없으면 B 로 기록하지 않는다
        self.say("뭔가 해줘", {"type": "none"}, "B")
        self.assertEqual(self.last_decision()["handling_class"], "A")


class PromptTaxonomyTests(unittest.TestCase):
    def test_prompt_and_schema_carry_taxonomy_and_capability_map(self):
        for phrase in ("처리 구분", "A 직접 답함", "B 직접 실행", "C 넘기고 결과를 받아 옴", "D 대표가 직접", "E 거절",
                       "visual_request", "능력 지도", "projects.descriptions"):
            self.assertIn(phrase, SYSTEM_PROMPT)
        self.assertIn("handling", RESPONSE_SCHEMA["required"])
        self.assertIn("visual_request", RESPONSE_SCHEMA["properties"]["action"]["properties"]["type"]["enum"])

    def test_spec_documents_taxonomy(self):
        spec = (ROOT / "SYSTEM_SPEC.md").read_text(encoding="utf-8")
        for phrase in ("처리 구분", "handling_class", "visual_request"):
            self.assertIn(phrase, spec)


if __name__ == "__main__":
    unittest.main()

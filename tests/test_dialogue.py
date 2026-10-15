"""Observable dialogue/lookup boundaries; fixtures are imported as modules (no duplicate discovery)."""
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_chat as chat_fixture
import test_watch as watch_fixture
from adapters.claude_api import ClaudeResult, ClaudeUnavailable
from app.chat import ChatConfig, ChatResponder, SYSTEM_PROMPT
from app.context import build_snapshot
from app.lookup import ReadOnlyLookup
from app.orca_bridge import OrcaQueue
from app.orca_handoff import Handoff


def query(source, text=""):
    return {"source": source, "query": text}


def lookup_turn(*requests):
    return {"reply": "", "handling": "A", "action": {"type": "none"}, "lookup": list(requests)}


def answer(text="확인했어요", action=None):
    return {"reply": text, "handling": "A", "action": action or {"type": "none"}, "lookup": []}


class SequenceClaude(chat_fixture.FakeClaude):
    def __init__(self, turns):
        super().__init__()
        self.turns = iter(turns)

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        turn = next(self.turns)
        if isinstance(turn, Exception):
            raise turn
        if callable(turn):
            turn = turn(kwargs)
        return ClaudeResult(turn, "claude-sonnet-5-5", {"input_tokens": 10, "output_tokens": 5})


class SequenceGemini(chat_fixture.FakeGemini):
    def __init__(self, turns):
        super().__init__()
        self.turns = iter(turns)

    def chat(self, envelope):
        self.calls.append(envelope)
        turn = next(self.turns)
        return turn(envelope) if callable(turn) else turn, "gemini-test"


class LookupLoopTests(chat_fixture.WorkspaceFixture):
    def service(self):
        return ReadOnlyLookup(self.bot.store, self.paths, 100, self.bot.file_roots, self.bot.jobs_config)

    def responder(self, turns):
        self.add_key()
        claude = SequenceClaude(turns)
        responder = ChatResponder(ChatConfig(providers=("claude",)), env_path=self.env_path,
                                  gemini=chat_fixture.FakeGemini(), claude=claude)
        responder.lookup = self.service()
        return responder, claude

    def test_fresh_query_observation_answer_and_usage(self):
        def final(call):
            self.assertIn("기록 6", call["context_text"])
            self.assertIn('"source": "hub"', call["context_text"])
            self.assertIn("observed_at", call["context_text"])
            return answer("공용 기록에 기록 6이 있어요.")
        responder, claude = self.responder([lookup_turn(query("hub", "기록 6")), final])
        result = responder.respond("아까 한 일은?", [], "{}")
        self.assertEqual(result.model_calls, 2)
        self.assertEqual(result.usage["input_tokens"], 20)
        self.assertEqual(result.observations[0]["status"], "ok")
        self.assertEqual(result.action.type, "none")
        self.assertLessEqual(claude.calls[-1]["timeout_seconds"], 60)

    def test_gemini_observations_and_provenance_use_same_protocol(self):
        gemini = SequenceGemini([lookup_turn(query("history", "포스터")), answer("기록에 포스터가 있어요.")])
        self.bot.store.add_chat_message(100, "assistant", "포스터 준비 대기", "receipt:bot")
        responder = ChatResponder(ChatConfig(providers=("gemini",)), env_path=None, gemini=gemini)
        responder.lookup = self.service()
        result = responder.respond("그 포스터는?", [], "{}")
        self.assertEqual(result.model_calls, 2)
        self.assertIn("receipt:bot", gemini.calls[1]["state_snapshot"])
        self.assertIn("포스터 준비 대기", gemini.calls[1]["state_snapshot"])

    def test_missing_then_ambiguous_result_cannot_trigger_action(self):
        responder, _ = self.responder([lookup_turn(query("results", "없음")),
                                      answer("해당 결과물 기록이 없어요.", {"type": "send_files"})])
        result = responder.respond("이미지 보여줘", [], "{}")
        self.assertEqual(result.observations[0]["status"], "empty")
        self.assertEqual(result.action.type, "none")
        for text in ("포스터 A", "포스터 B"):
            self.bot.store.create_task(owner_user_id=100, chat_id=100, project_id="deskmini",
                request_text=text, specification={"objective": text}, policy_hash="h", budget={})
        responder, _ = self.responder([lookup_turn(query("tasks", "포스터")),
                                      answer("A와 B 중 어느 포스터인가요?", {"type": "create_task", "objective": "x"})])
        result = responder.respond("그거 해줘", [], "{}")
        self.assertEqual(result.observations[0]["status"], "ambiguous")
        self.assertEqual(result.action.type, "none")

    def test_round_and_query_bounds_ignore_action_on_lookup_turn(self):
        turn = lookup_turn(query("hub"), query("history"))
        turn["action"] = {"type": "create_task", "objective": "must not run"}
        responder, claude = self.responder([turn, turn, turn])
        result = responder.respond("확인", [], "{}")
        self.assertEqual((len(claude.calls), len(result.observations)), (3, 4))
        self.assertEqual(result.action.type, "none")
        self.assertIn("한도", result.reply)
        responder, claude = self.responder([lookup_turn(*[query("hub")] * 3)])
        result = responder.respond("확인", [], "{}")
        self.assertEqual(len(claude.calls), 1)
        self.assertFalse(result.observations)
        self.assertEqual(result.action.type, "none")

    def test_failure_observation_reaches_model_without_exception_secrets(self):
        responder, claude = self.responder([lookup_turn(query("hub")), answer("조회가 실패했어요.")])
        def broken(*args, **kwargs):
            raise OSError("password=very-secret-value")
        responder.lookup = broken
        result = responder.respond("확인", [], "{}")
        self.assertIn('"status": "error"', claude.calls[1]["context_text"])
        self.assertNotIn("very-secret-value", str(result))
        self.assertEqual(result.action.type, "none")

    def test_provider_failure_after_lookup_is_honest_and_nonacting(self):
        responder, _ = self.responder([lookup_turn(query("hub")), ClaudeUnavailable("offline")])
        result = responder.respond("확인", [], "{}")
        self.assertEqual(result.action.type, "none")
        self.assertIn("실행하지 않았", result.reply)
        self.assertEqual(len(result.observations), 1)

    def test_deadline_discards_late_action_and_stops_calls(self):
        responder, claude = self.responder([answer("완료", {"type": "create_task", "objective": "x"})])
        with patch("app.chat.time.monotonic", side_effect=[0, 0, 61, 61]):
            result = responder.respond("해줘", [], "{}")
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(result.action.type, "none")
        self.assertIn("응답 시간", result.reply)

    def test_lookup_inputs_never_become_shell_paths_or_sql(self):
        service = self.service()
        for request in (query("shell", "ls"), query("hub", "../notify.env"), query("hub", "$(id)"),
                        query("history", "' OR 1=1 --"), query("results", "/etc/passwd"),
                        query("tasks", "x" * 81), {"source": "hub", "query": "", "path": "/tmp/x"}):
            with self.subTest(request=request), patch("subprocess.run", side_effect=AssertionError("no shell")):
                self.assertEqual(service(request)["status"], "error")
        self.assertEqual(service(query("hub"), deadline=time.monotonic() - 1)["status"], "error")

    def test_hub_safe_reads_block_symlink_hardlink_and_redact_output(self):
        secret = "sk-ant-api03-" + "A" * 40
        board = self.paths.agent_hub / "BOARD.md"
        board.write_text("포스터 " + secret)
        observation = self.service()(query("hub", "포스터"))
        self.assertNotIn(secret, str(observation))
        self.assertIn("redacted", str(observation))
        board.unlink()
        outside = self.root / "outside.md"
        outside.write_text("MUST_NOT_READ")
        board.symlink_to(outside)
        self.assertEqual(self.service()(query("hub", "MUST_NOT_READ"))["status"], "empty")
        board.unlink()
        os.link(outside, board)
        self.assertEqual(self.service()(query("hub", "MUST_NOT_READ"))["status"], "empty")

    def test_observation_redaction_and_output_size_defense(self):
        secret = "ghp_" + "X" * 40
        responder, claude = self.responder([lookup_turn(query("hub")), answer("조회 실패")])
        responder.lookup = lambda *a, **kw: {"status": "ok", "items": ["x" * 6_000, secret]}
        result = responder.respond("확인", [], "{}")
        self.assertEqual(result.observations[0]["status"], "error")
        self.assertNotIn(secret, claude.calls[-1]["context_text"])
        for index in range(15):
            self.bot.store.add_chat_message(100, "assistant", "내용" * 2_000, "receipt:bot")
        observation = self.service()(query("history"))
        self.assertLessEqual(len(json.dumps(observation, ensure_ascii=False)), 5_000)
        self.assertTrue(observation["truncated"])

    def test_tasks_are_scoped_and_missing_has_explicit_status(self):
        task = self.bot.store.create_task(owner_user_id=100, chat_id=100, project_id="deskmini",
            request_text="포스터 코드", specification={"objective": "포스터 코드"}, policy_hash="h", budget={})
        obs = self.service()(query("tasks", task["id"]))
        self.assertEqual(obs["items"][0]["state"], "AWAITING_START")
        with self.bot.store.connect() as db:
            db.execute("UPDATE tasks SET chat_id=999 WHERE id=?", (task["id"],))
        self.assertEqual(self.service()(query("tasks", task["id"]))["status"], "empty")

    def test_large_snapshot_remains_valid_and_exposes_omissions(self):
        with patch("app.context.repo_summary", return_value={"commits": ["commit " * 2_000]}):
            snapshot = build_snapshot(self.bot.store, self.paths, self.settings.projects)
        self.assertLessEqual(len(snapshot), 7_000)
        self.assertIn("jobs_recent", json.loads(snapshot))
        self.assertIn("truncated", snapshot)

    def test_all_lookup_sources_leave_database_unchanged(self):
        with self.bot.store.connect() as db:
            before = list(db.iterdump())
        with patch("subprocess.run", side_effect=AssertionError("lookups never spawn a process")):
            for source in ("tasks", "orca", "hub", "history", "results"):
                self.assertNotEqual(self.service()(query(source))["status"], "error")
        with self.bot.store.connect() as db:
            self.assertEqual(list(db.iterdump()), before)

    def test_failed_lookup_blocks_hostile_action_even_when_reply_requests_it(self):
        responder, _ = self.responder([lookup_turn(query("hub", "../private.env")),
            answer("운영 변경을 시작합니다", {"type": "dispatch_todo", "assignee": "codex", "todo_text": "change production"})])
        self.bot.chat = responder
        with patch("app.bot.build_snapshot", return_value="{}"), patch.object(self.bot, "_execute_decision") as execute:
            self.bot._handle_message(self.message("기록만 확인해줘"), 151)
        execute.assert_not_called()
        self.assertNotIn("운영 변경을 시작", str(self.bot.telegram.sent))
        self.assertIn("조회에 실패", self.bot.telegram.sent[-1]["text"])
        event = self.bot.store.recent_events_by_type("CHAT_DECISION", 1)[0]["payload"]
        self.assertEqual(event["lookup_observations"][0]["status"], "error")


class ContinuityTests(chat_fixture.WorkspaceFixture):
    def capture(self):
        mock = chat_fixture.FakeResponder(self.chat_result("알겠어요"))
        mock.respond = unittest.mock.Mock(return_value=self.chat_result("알겠어요"))
        self.bot.chat = mock
        return mock

    def test_fixed_command_and_actual_card_survive_small_talk(self):
        self.bot._handle_message(self.message("/new API 코드 문구 수정"), 101)
        task = self.bot.store.list_tasks(1)[0]
        for index in range(24):
            self.bot.store.add_chat_message(100, "user", f"잡담 {index}")
            self.bot.store.add_chat_message(100, "assistant", "네", "claude")
        captured = self.capture()
        self.bot._handle_message(self.message("아까 그 작업은?"), 102)
        history = captured.respond.call_args.args[1]
        self.assertTrue(any("/new API" in r["text"] for r in history))
        receipt = next(r for r in history if task["id"] in r["text"])
        self.assertTrue(receipt["provider"].startswith("receipt:"))
        self.assertIn("created_at", receipt)
        self.assertIn("착수", receipt["text"])

    def test_action_hallucination_is_neither_sent_nor_remembered(self):
        self.bot.chat = chat_fixture.FakeResponder(self.chat_result("이미 수정하고 배포까지 완료했어요.", {
            "type": "create_task", "objective": "API 수정", "output_format": "코드", "acceptance": ["테스트"]}))
        started = []
        self.bot.runner.start = started.append
        self.bot._handle_message(self.message("API 수정해줘"), 111)
        self.assertFalse(started)
        self.assertEqual(self.bot.store.list_tasks(1)[0]["state"], "AWAITING_START")
        self.assertNotIn("배포까지 완료", str(self.bot.telegram.sent))
        self.assertNotIn("배포까지 완료", str(self.bot.store.recent_chat_messages(100)))

    def test_failed_action_records_actual_failure(self):
        self.bot.chat = chat_fixture.FakeResponder(self.chat_result("완료했습니다", {
            "type": "send_files", "job_hint": "없는-작업"}))
        self.bot._handle_message(self.message("이미지 보여줘"), 112)
        history = self.bot.store.recent_chat_messages(100)
        self.assertIn("찾지 못", history[-1]["text"])
        self.assertNotIn("완료했습니다", str(history))

    def test_unauthorized_messages_do_not_enter_memory(self):
        message = self.message("/new never")
        message["from"]["id"] = 999
        self.bot._handle_message(message, 113)
        self.assertEqual(self.bot.store.recent_chat_messages(100), [])

    def test_internal_handling_does_not_require_classification_prose(self):
        self.assertIn("handling A~E는 내부 진단 전용", SYSTEM_PROMPT)
        self.assertNotIn("reply 첫 문장에서 어떻게 처리", SYSTEM_PROMPT)
        self.assertIn("정정", SYSTEM_PROMPT)


class LegacyLookupTests(chat_fixture.WorkspaceFixture):
    def service(self):
        return ReadOnlyLookup(self.bot.store, self.paths, 100, self.bot.file_roots, self.bot.jobs_config)

    def seed(self, *, visual=True, done=False):
        todo_id = "20261009-120001-ionenet-codex"
        relative = "visual/poster/poster-20261009-01/request.md"
        folder = self.paths.agent_hub / relative
        folder.parent.mkdir(parents=True)
        folder.write_text("# 요청\n이전 포스터 이미지 조사", encoding="utf-8")
        (folder.parent / "outputs").mkdir()
        todo = self.paths.agent_hub / "todo"
        if done:
            todo /= "done/2026-10"
        todo.mkdir(parents=True)
        (todo / (todo_id + ".md")).write_text("이전 포스터 이미지 조사\n" + relative, encoding="utf-8")
        self.bot.store.add_event(None, None, "HANDOFF_CREATED", "handoff", {
            "to": "codex", "kind": "visual" if visual else "todo", "text": "이전 포스터 이미지 조사",
            "todo_id": todo_id, "request_id": folder.parent.name if visual else None,
            "path": relative if visual else None})
        return folder.parent, todo_id

    def test_legacy_card_and_outputs_are_found_without_render_job(self):
        folder, todo_id = self.seed()
        (folder / "card.md").write_text("verdict: 채택\n\n# 결론\nA안 채택. 대비가 선명함.\n")
        (folder / "outputs" / "A.png").write_bytes(b"image-metadata-only")
        for source in ("hub", "results"):
            obs = self.service()(query(source, "포스터"))
            item = obs["items"][0]
            self.assertEqual(item["kind"], "handoff")
            self.assertEqual(item["todo_id"], todo_id)
            self.assertEqual(item["todo_status"], "open")
            self.assertEqual(item["result_status"], "artifacts_available")
            self.assertIn("A안 채택", item["conclusion"])
            self.assertEqual(item["files"][0]["name"], "A.png")
            self.assertFalse(item["sendable"])
            self.assertNotIn("job_hint", item)
        with self.bot.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_open_todo_is_found_but_does_not_prove_results_or_running(self):
        _, todo_id = self.seed(visual=False)
        obs = self.service()(query("results", todo_id))
        self.assertEqual(obs["status"], "ok")
        self.assertEqual(obs["items"][0]["todo_status"], "open")
        self.assertEqual(obs["items"][0]["result_status"], "no_result_record")
        self.assertEqual(obs["items"][0]["files"], [])
        self.assertEqual(self.service()(query("results", "不存在"))["status"], "empty")

    def test_done_todo_resolves_validated_visual_link_and_card(self):
        folder, todo_id = self.seed(visual=False, done=True)
        (folder / "card.md").write_text("verdict: 재작업\n# 결론\n배경 개선 필요\n")
        item = self.service()(query("hub", todo_id))["items"][0]
        self.assertEqual(item["todo_status"], "done")
        self.assertEqual(item["result_status"], "card_available")
        self.assertIn("배경 개선", item["conclusion"])
        self.assertEqual(item["files"], [])

    def test_legacy_secret_names_symlinks_hardlinks_and_text_redaction(self):
        folder, _ = self.seed()
        secret = "sk-ant-api03-" + "Q" * 40
        (folder / "card.md").write_text("# 결론\nA안 " + secret + "\n")
        outside = self.root / "outside.png"
        outside.write_bytes(b"not-for-lookup")
        (folder / "outputs" / "linked.png").symlink_to(outside)
        os.link(outside, folder / "outputs" / "hard.png")
        (folder / "outputs" / "api-key.txt").write_text("private")
        (folder / "outputs" / "ok.png").write_bytes(b"ok")
        obs = self.service()(query("results", "포스터"))
        self.assertNotIn(secret, str(obs))
        self.assertIn("redacted", str(obs))
        self.assertEqual([f["name"] for f in obs["items"][0]["files"]], ["ok.png"])
        (folder / "card.md").unlink()
        outside_card = self.root / "outside-card.md"
        outside_card.write_text("MUST_NOT_READ")
        (folder / "card.md").symlink_to(outside_card)
        self.assertNotIn("MUST_NOT_READ", str(self.service()(query("results", "포스터"))))
        (folder / "card.md").unlink()
        os.link(outside_card, folder / "card.md")
        self.assertNotIn("MUST_NOT_READ", str(self.service()(query("results", "포스터"))))

    def test_event_path_and_directory_link_cannot_expand_lookup_root(self):
        folder, _ = self.seed()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.png").write_bytes(b"private")
        (folder / "outputs").rmdir()
        (folder / "outputs").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.service()(query("results", "포스터"))["items"][0]["files"], [])
        self.bot.store.add_event(None, None, "HANDOFF_CREATED", "handoff", {
            "text": "malicious-path", "todo_id": "../outside", "path": "../outside/request.md"})
        item = self.service()(query("results", "malicious-path"))["items"][0]
        self.assertNotIn("request_file", item)
        self.assertNotIn("todo_file", item)
        self.assertEqual(item["files"], [])

    def test_legacy_result_never_validates_send_files_or_promises_buttons(self):
        folder, _ = self.seed()
        (folder / "outputs" / "A.png").write_bytes(b"image")
        gemini = SequenceGemini([lookup_turn(query("results", "포스터")),
            answer("버튼을 띄웠고 이미지를 보냈어요", {"type": "send_files", "job_hint": None})])
        self.bot.chat = ChatResponder(ChatConfig(providers=("gemini",)), env_path=None, gemini=gemini)
        with patch("app.bot.build_snapshot", return_value="{}"), patch.object(self.bot, "_files_command") as send:
            self.bot._handle_message(self.message("아까 포스터 이미지 보여줘"), 991)
        send.assert_not_called()
        self.assertIn("전송 경로 밖", self.bot.telegram.sent[-1]["text"])
        self.assertNotIn("보냈어요", str(self.bot.telegram.sent))
        self.assertIn("A.png", gemini.calls[1]["state_snapshot"])

    def test_missing_and_ambiguous_lookup_suppresses_false_button_promise(self):
        self.seed()
        second = {"to": "codex", "kind": "todo", "text": "포스터 두 번째", "todo_id": None, "path": None}
        self.bot.store.add_event(None, None, "HANDOFF_CREATED", "handoff", second)
        for hint in ("missing", "포스터"):
            gemini = SequenceGemini([lookup_turn(query("results", hint)),
                answer("이미지 버튼을 띄웠어요. 완료했습니다", {"type": "send_files", "job_hint": hint})])
            responder = ChatResponder(ChatConfig(providers=("gemini",)), env_path=None, gemini=gemini)
            responder.lookup = self.service()
            result = responder.respond("이미지 보여줘", [], "{}")
            self.assertEqual(result.action.type, "none")
            self.assertNotIn("띄웠어요", result.reply)
            self.assertNotIn("완료했습니다", result.reply)

    def test_legacy_scan_is_readonly_bounded_and_retains_internal_time(self):
        folder, _ = self.seed()
        for index in range(20):
            (folder / "outputs" / f"image-{index}.png").write_bytes(b"image")
        with self.bot.store.connect() as db:
            before = list(db.iterdump())
        with patch("subprocess.run", side_effect=AssertionError("no shell")):
            obs = self.service()(query("results", "포스터"))
        with self.bot.store.connect() as db:
            self.assertEqual(before, list(db.iterdump()))
        self.assertEqual(len(obs["items"][0]["files"]), 8)
        self.assertLessEqual(len(json.dumps(obs, ensure_ascii=False)), 5_000)
        self.assertIn("created_at", obs["items"][0])
        self.assertIn("묻지 않은 정확한 시각이나 UTC", SYSTEM_PROMPT)
        self.assertIn("한국 시간(KST)", SYSTEM_PROMPT)


class OrcaContinuityTests(watch_fixture.BotFixture):
    def queue(self):
        return OrcaQueue.load(self.bot.store, self.settings.root)

    def test_fixed_receipt_anchors_elliptical_model_correction_and_dedup(self):
        queue = self.queue()
        one = queue.submit("a", "trial", "sol", "이미지 조사", self.settings.allowed_chat_id)
        two = queue.submit("b", "trial", "sol", "다른 업무", self.settings.allowed_chat_id)
        self.bot._handle_update(self.message("/orca " + one["id"], 9101))
        with patch.object(self.bot.chat, "respond", side_effect=AssertionError("no LLM")):
            self.bot._handle_update(self.message("아까 그 작업, 솔 말고 아스트라로", 9102))
        changes = Handoff(queue).changes(one["id"])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["model"], "gpt-6-astra")
        self.assertEqual(Handoff(queue).changes(two["id"]), [])
        self.bot._handle_update(self.message("아까 그 작업, 솔 말고 아스트라로", 9102))
        self.assertEqual(len(Handoff(queue).changes(one["id"])), 1)
        self.assertNotIn(one["id"], self.tg.sent[-1]["text"])
        self.assertIn("종료", self.tg.sent[-1]["text"])
        self.assertIn(one["id"], str(self.bot.store.recent_chat_messages(self.settings.allowed_chat_id)))

    def test_multiple_receipt_candidates_and_task_correction_clarify(self):
        queue = self.queue()
        for key in ("a", "b"):
            queue.submit(key, "trial", "sol", key, self.settings.allowed_chat_id)
        self.bot._handle_update(self.message("/orca status", 9201))
        self.bot._handle_update(self.message("아까 그 작업 아스트라로 이어줘", 9202))
        self.assertIn("하나로 정할 수 없", self.tg.sent[-1]["text"])
        self.bot._handle_update(self.message("그 작업 말고 아스트라로 이어줘", 9203))
        self.assertIn("작업을 바꾸려는", self.tg.sent[-1]["text"])
        self.assertTrue(all(not Handoff(queue).changes(row["id"]) for row in queue.rows()))

    def test_model_text_cannot_select_orca_reference(self):
        row = self.queue().submit("a", "trial", "sol", "x", self.settings.allowed_chat_id)
        self.bot.store.add_chat_message(self.settings.allowed_chat_id, "assistant", row["id"], "claude")
        self.bot._handle_update(self.message("아까 그 작업 아스트라로 이어줘", 9301))
        self.assertEqual(Handoff(self.queue()).changes(row["id"]), [])

    def test_bridge_result_outbox_enters_history_once(self):
        row = self.queue().submit("a", "trial", "sol", "이미지 조사", self.settings.allowed_chat_id)
        self.bot.store.queue_outbox(dedupe_key="bridge-result", chat_id=self.settings.allowed_chat_id,
            method="sendMessage", payload={"chat_id": self.settings.allowed_chat_id,
                "text": row["id"] + " 조사 완료: 포스터 후보 세 개", "parse_mode": "HTML"})
        self.bot._flush_outbox()
        self.bot._flush_outbox()
        rows = self.bot.store.recent_chat_messages(self.settings.allowed_chat_id)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["provider"].startswith("receipt:"))
        self.assertIn("포스터 후보", rows[0]["text"])

    def test_results_lookup_lists_safe_metadata_without_reading_media(self):
        # BotFixture already has a render job with a deliverable.
        self.bot._current_jobs(self.now)
        service = ReadOnlyLookup(self.bot.store, self.bot.paths, self.settings.allowed_chat_id,
                                 self.bot.file_roots, self.bot.jobs_config)
        with patch("app.lookup.open_checked", side_effect=AssertionError("metadata only")):
            obs = service(query("results", "full-v1"))
        self.assertEqual(obs["status"], "ok")
        self.assertTrue(obs["items"][0]["files"])
        self.assertEqual(obs["items"][0]["job_hint"], "full-v1")

    def test_image_request_followup_queries_real_receipts_and_results(self):
        self.bot._handle_update(self.message("/files full-v1", 9401))
        gemini = SequenceGemini([lookup_turn(query("history", "full-v1"), query("results", "full-v1")),
                                 answer("full-v1 결과물 목록을 확인했어요.")])
        self.bot.chat = ChatResponder(ChatConfig(providers=("gemini",)), env_path=None, gemini=gemini)
        with patch("app.bot.build_snapshot", return_value="{}"):
            self.bot._handle_update(self.message("아까 이미지 보여줘", 9402))
        context = gemini.calls[-1]["state_snapshot"]
        self.assertIn("hero.mp4", context)
        self.assertIn("receipt:", context)
        self.assertIn("선택지", context)
        event = self.bot.store.recent_events_by_type("CHAT_DECISION", 1)[0]["payload"]
        self.assertEqual(event["model_calls"], 2)
        self.assertEqual(len(event["lookup_observations"]), 2)
        self.assertEqual(self.tg.files, [])

    def test_found_artifact_after_missing_hub_record_can_offer_correct_file_buttons(self):
        self.bot._handle_update(self.message("/files full-v1", 9501))
        gemini = SequenceGemini([lookup_turn(query("hub", "never-existed"), query("results", "full-v1")),
            answer("파일을 보냈어요", {"type": "send_files", "job_hint": "full-v1"})])
        self.bot.chat = ChatResponder(ChatConfig(providers=("gemini",)), env_path=None, gemini=gemini)
        with patch("app.bot.build_snapshot", return_value="{}"):
            self.bot._handle_update(self.message("이미지 보여줘", 9502))
        self.assertIn("받을 파일을 누르세요", self.tg.sent[-1]["text"])
        self.assertNotIn("파일을 보냈어요", str(self.tg.sent))
        self.assertEqual(self.tg.files, [])

from __future__ import annotations

import html
import os
import socket
import json
import logging
import signal
import stat
import sys
import re
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from adapters.gemini_rest import GeminiInterpreter
from adapters.telegram import TelegramAPI, TelegramError, inline_keyboard
from app.alerts import (
    SEVERITY_ICONS,
    AlertConfig,
    AlertEvent,
    AlertPrefs,
    AlertStore,
    alert_text,
    brief_due,
    decide_delivery,
    format_duration,
    in_quiet_hours,
    keyboard_rows,
    parse_duration,
    MAX_QUIET_SECONDS,
)
from app.chat import FALLBACK_TEXT, ChatConfig, ChatResponder, ChatResult, ChatUnavailable, gemini_model
from app.collector import bytes_human, collect_host_status
from app.config import Settings
from app.files import (
    TEXT_INLINE_CHARS,
    check_deliverable,
    list_deliverables,
    open_checked,
    read_redacted_text,
    resolve_allowed,
)
from app.jobs import (
    STATUS_ICONS,
    STATUS_LABELS,
    JobObservation,
    JobsConfig,
    clean_log_lines,
    discover_jobs,
    match_job,
    tail_text,
)
from app.codex_usage import latest_codex_limits, window_label
from app.context import (
    WorkspacePaths,
    board_text,
    build_snapshot,
    home_relative,
    hub_locks,
    recent_hub_logs,
    recent_work_dirs,
    registered_repos,
    repo_summary,
)
from app.inbox import (
    MAX_ATTACHMENT_BYTES,
    AttachmentRejected,
    check_content,
    describe,
    incoming_from_message,
    photo_name,
    sanitize_name,
    store_file,
    text_preview,
)
from app.policy import Decision, Policy
from app.release import ReleaseError, ReleaseManager, ReleaseRefused
from app.retry import RETRY_MARGIN, parse_reset_time, reset_pending, scheduled_time
from app.runner import RunnerError, TaskRunner
from app.redact import redact_html, redact_text
from app.state import ConflictError, StateStore
from app.todo import HubTodo
from app.verify import VerificationError, Verifier, build_reviewer
from app.visual import STATUS_LABELS as HANDOFF_STATUS_LABELS
from app.visual import SAFE_REL_RE, card_conclusion, create_visual_request, handoff_status, todo_id_from_output
from app.watch import Observation, detect


LOG = logging.getLogger("deskmini-chief")
RELEASE_ACTIONS = frozenset({"merge", "push", "deploy"})
ACTIVE_STATES = (
    "QUEUED",
    "STARTING",
    "RUNNING",
    "VERIFYING",
    "STOP_REQUESTED",
    "STOPPING",
)


def esc(value: Any) -> str:
    """평문을 먼저 마스킹한 뒤 HTML 이스케이프한다(이스케이프된 글자를 다시 마스킹하면 태그가 깨진다)."""
    text = str(value)
    return html.escape(redact_text(text, len(text) + 1), quote=True)



def sd_notify(state: str) -> None:
    """systemd 에 상태를 알린다(NOTIFY_SOCKET 이 없으면 아무것도 하지 않음)."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode())
    except OSError:
        pass


class ChiefBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = StateStore(settings.db_path, settings.root / "schema.sql")
        self.telegram = TelegramAPI(
            settings.telegram_token,
            request_timeout=settings.telegram_timeout_seconds + 15,
        )
        models_path = settings.root / "config" / "models.toml"
        self.gemini = GeminiInterpreter(settings.gemini_script, model=gemini_model(models_path))
        self.policy = Policy(
            self.gemini,
            list(settings.projects),
            {pid: getattr(project, "description", "") for pid, project in settings.projects.items()},
        )
        self.chat_config = ChatConfig.load(models_path)
        self.chat = ChatResponder(self.chat_config, env_path=settings.env_path, gemini=self.gemini)
        self.paths = WorkspacePaths.default()
        self.runner = TaskRunner(settings, self.store)
        self.verifier = Verifier(settings, self.store, build_reviewer(models_path, self.gemini, settings.state_dir))
        self.release = ReleaseManager(settings, self.store)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chief")
        self.verification_futures: dict[str, Future[dict[str, Any]]] = {}
        self.release_futures: dict[str, Future[dict[str, Any]]] = {}
        self.last_card_update: dict[str, float] = {}
        self.stopping = False
        # 감시기·알림. 시뮬레이터는 clock·job_scanner·observer 를 바꾸거나 watch_tick(observation=...) 을 쓴다.
        self.alert_config = AlertConfig.load(settings.root / "config" / "alerts.toml")
        self.jobs_config = JobsConfig.load(settings.root / "config" / "jobs.toml")
        self.alerts = AlertStore(self.store)
        self.clock: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.job_scanner: Callable[[datetime], list[JobObservation]] = self._scan_jobs
        self.observer: Callable[[datetime], Observation] = self.observe
        self.last_watch_monotonic: float | None = None

    def run(self) -> None:
        me = self.telegram.get_me()
        LOG.info("Telegram connection verified for bot id=%s", me.get("id"))
        self._recover_state()
        self._notify(
            "chief-started",
            "🏛️ <b>DeskMini 비서실장 시작</b>\n\n"
            "상태 조회와 작업 접수를 받을 준비가 됐습니다.\n"
            "명령: /status · /tasks · /jobs · /files · /hub · /git · /work · /new · /help",
        )
        while not self.stopping:
            sd_notify("WATCHDOG=1")  # 루프가 멈추면 systemd(WatchdogSec) 가 재시작한다(10/10 멈춤 사고)
            self._process_futures()
            self._reconcile_tasks()
            self._run_scheduled_retries()
            self._flush_outbox()
            self._maybe_watch()
            offset = self.store.next_update_offset()
            try:
                updates = self.telegram.get_updates(offset, self.settings.telegram_timeout_seconds)
            except TelegramError as exc:
                LOG.warning("Telegram polling failed: %s", redact_text(str(exc), 500))
                time.sleep(min(exc.retry_after or 3, 30))
                continue
            for update in updates:
                self._handle_update(update)

    def _recover_state(self) -> None:
        self.store.requeue_interrupted_updates()
        for operation in self.store.list_operations_in_state("EXECUTING"):
            try:
                self.release.reconcile_interrupted(operation)
            except Exception as exc:
                LOG.error("Operation recovery failed: %s", type(exc).__name__)
                recovered = self.store.finish_operation(
                    operation["id"],
                    state="UNKNOWN",
                    before_evidence=operation.get("before_evidence") or {},
                    after_evidence={"reason": "bot restarted before the outcome was proven"},
                )
                self.store.add_event(
                    operation["task_id"], None, "OPERATION_OUTCOME_UNKNOWN", "recovery", recovered
                )
        self._recover_consumed_approvals()
        for update in self.store.pending_updates():
            self._handle_update(update, replay=True)

    def _recover_consumed_approvals(self) -> None:
        """소비만 되고 실행 시도 기록(outcome)이 없는 승인 = 소비 직후 크래시. 승인 유효시간 안이고 작업 버전이 그대로면
        한 번 이어서 실행한다. 유효시간이 지났거나 예약 승인(예약 확인을 못 받은 채 몇 시간 뒤 실행되면 놀랍다)이면
        ABANDONED 로 남기고 사용자에게 한 번 알린다. 실행이 거절·실패로 끝난 승인(outcome 기록됨)은 다시 실행하지 않는다."""
        now = datetime.now(UTC)  # 승인 유효시간은 소비(consume_approval)와 같은 실제 시계로 잰다
        abandoned: list[dict[str, Any]] = []
        for approval in self.store.recoverable_consumed_approvals():
            if approval["action"] == "schedule_retry" or datetime.fromisoformat(approval["expires_at"]) <= now:
                self.store.set_approval_outcome(approval["id"], "ABANDONED", "not executed before restart")
                abandoned.append(approval)
                continue
            try:
                self._execute_approval(approval)
            except Exception as exc:
                LOG.error("Could not recover consumed approval %s: %s", approval["id"], type(exc).__name__)
                refused = isinstance(exc, (RunnerError, ConflictError))
                self.store.set_approval_outcome(
                    approval["id"], "ABANDONED" if refused else "FAILED", f"recovery: {type(exc).__name__}: {exc}"
                )
                abandoned.append(approval)
                continue
            if approval["action"] not in RELEASE_ACTIONS:
                self.store.set_approval_outcome(approval["id"], "EXECUTED", "recovered after restart")
        if abandoned:
            ids = ", ".join(f"<code>{esc(item['task_id'])}</code>" for item in abandoned[:5])
            self._notify(
                f"approvals-abandoned-{abandoned[0]['id']}",
                f"⚠️ 재시작 전에 처리되지 못한 승인은 폐기했습니다 — 다시 요청해 주세요: {ids}\n"
                "착수는 /new, 재시도는 <code>/retry 작업ID</code> 로 버튼을 다시 띄웁니다.",
            )

    def stop(self, *_: Any) -> None:
        self.stopping = True

    def _handle_update(self, update: dict[str, Any], replay: bool = False) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return
        authorized = self._authorized_update(update)
        stored_payload = update if authorized else {"update_id": update_id, "unauthorized": True}
        if not self.store.claim_update(update_id, stored_payload):
            return
        if not authorized:
            self.store.finish_update(update_id, "IGNORED")
            return
        try:
            if "message" in update:
                self._handle_message(update["message"], update_id)
            elif "callback_query" in update:
                self._handle_callback(update["callback_query"], replay=replay)
            self.store.finish_update(update_id)
        except (ConflictError, RunnerError) as exc:
            # 만료·이미 사용한 승인, 진행 중인 다른 작업 같은 예상된 거절: 알기 쉬운 안내 한 번 + 처리 완료로 남긴다
            # (FAILED 로 두면 Telegram 재배달·재시작 재생 때 같은 안내가 반복된다).
            LOG.info("Update refused: %s", type(exc).__name__)
            self.store.finish_update(update_id, "PROCESSED", f"{type(exc).__name__}: {exc}")
            self._send(self._refusal_text(exc))
        except Exception as exc:
            LOG.error("Update processing failed: %s", type(exc).__name__)
            self.store.finish_update(update_id, "FAILED", type(exc).__name__)
            if self._authorized_update(update):
                self._send(f"⚠️ 요청 처리 실패: <code>{esc(type(exc).__name__)}</code>")

    def _authorized_update(self, update: dict[str, Any]) -> bool:
        message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
        sender = update.get("message", {}).get("from") or (update.get("callback_query") or {}).get("from") or {}
        chat = message.get("chat") or {}
        return (
            chat.get("type") == "private"
            and chat.get("id") == self.settings.allowed_chat_id
            and sender.get("id") == self.settings.allowed_user_id
        )

    def _authorized_message(self, message: dict[str, Any]) -> bool:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        return (
            chat.get("type") == "private"
            and chat.get("id") == self.settings.allowed_chat_id
            and sender.get("id") == self.settings.allowed_user_id
        )

    def _handle_message(self, message: dict[str, Any], update_id: int) -> None:
        if not self._authorized_message(message):
            return
        incoming = incoming_from_message(message)
        if incoming is not None:
            self._handle_attachment(message, incoming, update_id)
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send("텍스트, 사진, 문서(pdf·txt·md·csv·json)만 처리할 수 있습니다.")
            return
        command = text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command == "/orca":
            self._orca_command(text, update_id)
            return
        decision = self.policy.decide_fixed(text)
        if decision is not None:
            self._execute_decision(decision, text, update_id)
            return
        pending = self._pending_attachments()
        if pending:
            # 캡션 없이 보낸 첨부가 있으면 이번 지시와 함께 모델에 메타데이터를 보여 준다.
            text = text + "\n" + "\n".join(self._attachment_context(item, waiting=True) for item in pending)
        self._chat_reply(text, update_id)

    def _orca_command(self, text: str, update_id: int) -> None:
        from app.orca_bridge import OrcaQueue
        queue = OrcaQueue.load(self.store, self.settings.root)
        argument = text.strip().split(maxsplit=1)
        argument = argument[1] if len(argument) > 1 else ""
        if not argument or argument == "status":
            self._send(esc(queue.describe()))
        elif argument.startswith("recover "):
            try:
                queue.recover(argument.split(maxsplit=1)[1].strip())
            except ValueError as exc:
                self._send(esc(exc))
                return
            self._send("저장된 동일 요청의 복구를 접수했습니다. 새 작업으로 재실행하지 않습니다.")
        elif argument.startswith("reply "):
            parts = argument.split(maxsplit=2)
            if len(parts) != 3:
                self._send("사용법: <code>/orca reply 요청ID 답변 내용</code>")
                return
            try:
                queue.answer(parts[1], f"telegram:{update_id}", parts[2])
            except ValueError as exc:
                self._send(esc(exc))
                return
            self._send("답변을 접수했습니다. 실행기가 Orca 작업자에게 전달합니다.")
        elif argument.startswith("new "):
            parts = argument.split(maxsplit=3)
            if len(parts) != 4:
                self._send("사용법: <code>/orca new trial codex 작업 내용</code>")
                return
            try:
                row = queue.submit(f"telegram:{update_id}", parts[1], parts[2], parts[3], self.settings.allowed_chat_id)
            except ValueError as exc:
                self._send(esc(exc))
                return
            self._notify(f"orca:{row['id']}:accepted", esc(
                f"Orca 접수: {row['id']} · {row['state']}\n{row['agent']} / {row['project']}\n"
                "실행기가 상태를 확인한 후 시작합니다. 조회: /orca " + row['id']))
        else:
            from app.orca_bridge import ID_RE
            self._send(esc(queue.describe(argument)) if ID_RE.fullmatch(argument) else
                "사용법: <code>/orca status</code> · <code>/orca 요청ID</code> · <code>/orca new trial codex 작업 내용</code>")

    # ------------------------------------------------------------------ 첨부(사진·문서)

    def _pending_attachments(self) -> list[dict[str, Any]]:
        since = (self.clock() - PENDING_ATTACHMENT_WINDOW).isoformat(timespec="seconds")
        return self.store.pending_attachments(self.settings.allowed_chat_id, since)

    def _attachment_context(self, item: dict[str, Any], *, waiting: bool = False) -> str:
        line = describe(item)
        if waiting:
            line = line.replace("[첨부:", "[대기 중 첨부:", 1)
        preview = text_preview(item)
        if preview:
            line += "\n[첨부 내용(마스킹)]\n" + preview
        return line

    def _handle_attachment(self, message: dict[str, Any], incoming: Any, update_id: int) -> None:
        caption = message.get("caption") if isinstance(message.get("caption"), str) else ""
        caption = caption.strip()
        # 크래시 뒤 같은 update 를 재생하면 이미 저장한 파일을 다시 받지 않는다((update_id, file_unique_id) 기준).
        existing = self.store.attachment_for_file(update_id, incoming.file_key)
        if existing is not None:
            attachment_id = int(existing["id"])
            item = existing
        else:
            try:
                if incoming.size is not None and incoming.size > MAX_ATTACHMENT_BYTES:
                    raise AttachmentRejected("20MB 를 넘는 파일은 받지 않습니다.")
                name = sanitize_name(incoming.name, incoming.mime)
                info = self.telegram.get_file(incoming.file_id)
                if int(info.get("file_size") or 0) > MAX_ATTACHMENT_BYTES:
                    raise AttachmentRejected("20MB 를 넘는 파일은 받지 않습니다.")
                content = self.telegram.download_file(str(info.get("file_path") or ""), MAX_ATTACHMENT_BYTES)
                if incoming.kind == "photo":
                    name = photo_name(name, content)
                check_content(name, content)
            except AttachmentRejected as exc:
                self.store.add_event(None, None, "ATTACHMENT_REJECTED", "inbox", {"reason": str(exc), "kind": incoming.kind})
                self._send(f"📎 받지 않았습니다: {esc(exc)}")
                return
            except TelegramError as exc:
                self._send(f"📎 파일을 내려받지 못했습니다: {esc(redact_text(str(exc), 200))}")
                return
            now = self.clock()
            path, digest = store_file(self.settings.state_dir / "inbox", name, content, now)
            attachment_id = self.store.add_attachment(
                update_id=update_id,
                chat_id=self.settings.allowed_chat_id,
                kind=incoming.kind,
                original_name=incoming.name,
                stored_name=name,
                stored_path=str(path),
                mime=incoming.mime,
                size=len(content),
                sha256=digest,
                caption=caption or None,
                file_unique_id=incoming.file_key,
            )
            item = self.store.get_attachment(attachment_id) or {}
            self.store.add_event(None, None, "ATTACHMENT_STORED", "inbox", {"id": attachment_id, "name": name, "size": len(content)})
        if not caption:
            if message.get("media_group_id"):
                self._send(f"📎 받았습니다: {esc(describe(item))}")
                return
            self._send(
                f"📎 받았습니다: {esc(describe(item))}\n\n"
                "이 파일로 무엇을 할까요? 30분 안에 보내는 다음 지시에 붙입니다.\n"
                f"예: <code>/new sandbox: 첨부한 표를 README 에 정리</code> 또는 그냥 설명을 보내 주세요."
            )
            return
        decision = self.policy.decide_fixed(caption)
        if decision is not None:
            self._execute_decision(decision, caption, update_id)
            return
        others = [entry for entry in self._pending_attachments() if entry.get("id") != attachment_id]
        context = [self._attachment_context(item)] + [self._attachment_context(entry, waiting=True) for entry in others]
        self._chat_reply(caption + "\n" + "\n".join(context), update_id)

    def _link_attachments(self, task_id: str, source_update_id: int | None) -> list[dict[str, Any]]:
        ids = [item["id"] for item in self.store.attachments_for_update(source_update_id)] if source_update_id else []
        ids += [item["id"] for item in self._pending_attachments() if item["id"] not in ids]
        self.store.link_attachments(task_id, ids)
        return self.store.attachments_for_task(task_id)

    def _chat_reply(self, text: str, update_id: int | None) -> None:
        chat_id = self.settings.allowed_chat_id
        history = self.store.recent_chat_messages(chat_id, self.chat_config.history_turns)
        snapshot = build_snapshot(self.store, self.paths, self.settings.projects)
        try:
            result = self.chat.respond(text, history, snapshot)
        except ChatUnavailable as exc:
            self.store.add_chat_message(chat_id, "user", text)
            self.store.add_event(
                None, None, "CHAT_UNAVAILABLE", "chat", {"errors": list(exc.errors)}
            )
            self._send(esc(FALLBACK_TEXT))
            return
        self.store.add_chat_message(chat_id, "user", text)
        self.store.add_chat_message(chat_id, "assistant", result.reply, result.provider)
        decision = self.policy.from_chat_action(result.action, text)
        self._record_chat_decision(result, decision)
        reply = result.reply
        if decision is not None and decision.intent in IMMEDIATE_INTENTS and APPROVAL_CLAIM_RE.search(reply):
            # 알림 설정·결과물 목록은 승인 없이 바로 처리되고 실제 결과는 아래 명령 응답이 알린다.
            # 모델이 "승인 카드를 띄웠다"처럼 사실과 다른 말을 하면 그 문장 대신 코드가 만든 한 줄로 바꾼다.
            reply = IMMEDIATE_REPLIES[decision.intent]
        self._send(esc(redact_text(reply, 3_500)))
        if decision is not None:
            self._execute_decision(decision, text, update_id)

    def _record_chat_decision(self, result: ChatResult, decision: Decision | None) -> None:
        self.store.add_event(
            None,
            None,
            "CHAT_DECISION",
            "chat",
            {
                "provider": result.provider,
                "model": result.model,
                "proposed_action": result.action.type,
                "proposed_task_id": result.action.task_id,
                "accepted_intent": decision.intent if decision else None,
                "handling_class": handling_class(decision, result.handling),
                "model_handling": result.handling,
                "latency_ms": result.latency_ms,
                "usage": result.usage,
                "fallback_errors": list(result.errors),
            },
        )

    def _execute_decision(
        self, decision: Decision, original_text: str, source_update_id: int | None = None
    ) -> None:
        if decision.intent == "help":
            self._send(self._help_text())
            return
        if decision.intent == "usage":
            self._send(decision.message or self._help_text())
            return
        if decision.intent == "hub":
            self._send(self._hub_text())
            return
        if decision.intent == "git":
            self._send(self._git_text())
            return
        if decision.intent == "work":
            self._send(self._work_text())
            return
        if decision.intent == "model":
            self._send(self._model_text())
            return
        if decision.intent == "codex":
            self._send(self._codex_text())
            return
        if decision.intent == "status":
            self._send(self._status_text())
            return
        if decision.intent == "jobs":
            self._send(self._jobs_text())
            return
        if decision.intent == "files":
            self._files_command(decision.argument)
            return
        if decision.intent == "logs":
            self._logs_command(decision.argument)
            return
        if decision.intent == "quiet":
            self._quiet_command(decision.argument)
            return
        if decision.intent == "alerts":
            self._alerts_command(decision.argument)
            return
        if decision.intent == "brief":
            self._send(self.brief_text(self.clock()))
            return
        if decision.intent == "retry" and decision.task_id:
            self._offer_retry(decision.task_id)
            return
        if decision.intent == "todo_list":
            result = self.hub_todo.list()
            title = "<b>열린 할 일</b> (agent-hub/todo)" if result.ok else "⚠️ 할 일 목록을 읽지 못했습니다"
            self._send(_fit(
                f"{title}\n<pre>{esc(result.output or '- 없음')}</pre>\n" + self._handoff_lines()
                + "추가: <code>/todo codex 할 일</code> · 닫기: <code>/todo done 번호</code>"
            ))
            return
        if decision.intent == "visual_request":
            self._visual_request(decision.argument, decision.project_id)
            return
        if decision.intent in {"visual_suggest", "ask_project"}:
            self._send(decision.message)
            return
        if decision.intent == "todo_add" and decision.project_id:
            result = self.hub_todo.add(decision.project_id, decision.argument)
            if result.ok:
                todo_id = todo_id_from_output(result.output)
                self._record_handoff(decision.project_id, "todo", decision.argument, todo_id=todo_id)
                self._send(
                    f"📝 <b>{esc(decision.project_id)}</b> 할 일로 남겼습니다(agent-hub 커밋 · 처리 구분 C: 넘기고 결과를 받아 옴).\n"
                    f"• {esc(decision.argument[:300])}\n추적 ID: <code>{esc(todo_id or result.output[-120:])}</code>\n"
                    "다음 세션 시작(hub read) 때 맨 위에 보이고, 닫히면 텔레그램으로 알려 드립니다. 목록: /todo"
                )
            else:
                self._send(f"⚠️ 할 일을 남기지 못했습니다: {esc(result.output[-300:])}")
            return
        if decision.intent == "todo_done" and decision.argument:
            result = self.hub_todo.done(decision.argument)
            prefix = "✅ 닫았습니다" if result.ok else "⚠️ 닫지 못했습니다"
            self._send(f"{prefix}: <code>{esc(result.output[-200:])}</code>")
            return
        if decision.intent == "list_tasks":
            self._send(self._tasks_text())
            return
        if decision.intent == "task_detail" and decision.task_id:
            self._send(self._task_text(decision.task_id))
            return
        if decision.intent == "create_task" and decision.specification and decision.project_id:
            task = self.store.create_task(
                owner_user_id=self.settings.allowed_user_id,
                chat_id=self.settings.allowed_chat_id,
                project_id=decision.project_id,
                request_text=original_text,
                specification=decision.specification,
                policy_hash=self.policy.policy_hash,
                budget={"runtime_seconds": self.settings.max_runtime_seconds, "retries": 1},
                source_update_id=source_update_id,
            )
            self._link_attachments(task["id"], source_update_id)
            approval_id = self.store.create_approval(
                task_id=task["id"],
                action="start",
                payload=self._start_payload(task),
                owner_user_id=self.settings.allowed_user_id,
                chat_id=self.settings.allowed_chat_id,
                task_version=task["version"],
                ttl_seconds=self.settings.approval_ttl_seconds,
            )
            response = self._send(
                self._task_confirmation_text(task),
                reply_markup=inline_keyboard(
                    [
                        [("🚀 착수", f"approve:start:{approval_id}")],
                        [("취소", f"cancel:{task['id']}")],
                    ]
                ),
            )
            if response and response.get("message_id"):
                self.store.set_task_fields(task["id"], telegram_message_id=response["message_id"])
            return
        if decision.intent == "stop_task" and decision.task_id:
            if decision.needs_confirmation:
                # 모델이 제안한 중단: 실제로 있고 진행 중인 작업에만 버튼을 띄운다.
                try:
                    current = self.store.get_task(decision.task_id)
                except KeyError:
                    current = None
                if current is None or current["state"] not in ACTIVE_STATES:
                    self._send(f"해당 작업이 없거나 이미 끝났습니다: <code>{esc(decision.task_id)}</code>")
                    return
                self._send(
                    f"중단하려면 버튼을 눌러 주세요: <code>{esc(decision.task_id)}</code>",
                    reply_markup=self._running_keyboard(decision.task_id),
                )
                return
            self._stop_task(decision.task_id)
            return
        if decision.intent in {"merge", "push", "deploy"}:
            if not decision.task_id:
                self._send("대상 작업 ID를 함께 입력해 주세요. 예: <code>merge T-...</code>")
                return
            self._offer_release(decision.task_id, decision.intent)
            return
        self._send(decision.message or "요청을 안전하게 분류하지 못했습니다. /help를 확인해 주세요.")

    def _handle_callback(self, query: dict[str, Any], *, replay: bool = False) -> None:
        message = query.get("message") or {}
        synthetic = {"callback_query": query}
        if not self._authorized_update(synthetic):
            return
        callback_id = query.get("id")
        data = str(query.get("data") or "")
        if callback_id and not replay:
            try:
                self.telegram.answer_callback(callback_id)
            except Exception as exc:  # noqa: BLE001 — 버튼 로딩 표시만 지우는 호출. 실패해도 누른 것은 처리한다.
                # 봇이 꺼진 사이 누른 버튼은 'query is too old' 로 거절된다.
                LOG.warning("answerCallbackQuery failed: %s", redact_text(str(exc), 200))
        if data.startswith("approve:"):
            _, action, approval_id = data.split(":", 2)
            try:
                approval = self.store.consume_approval(
                    approval_id=approval_id,
                    action=action,
                    owner_user_id=self.settings.allowed_user_id,
                    chat_id=self.settings.allowed_chat_id,
                )
            except ConflictError as exc:
                if not replay or "already used" not in str(exc):
                    raise
                previous = self.store.get_approval(approval_id)
                if (
                    previous["action"] != action
                    or previous["owner_user_id"] != self.settings.allowed_user_id
                    or previous["chat_id"] != self.settings.allowed_chat_id
                    or previous["consumed_at"] is None
                ):
                    raise
                self._send(
                    f"ℹ️ 이미 접수된 승인입니다. 현재 상태를 복구했습니다: "
                    f"<code>{esc(previous['task_id'])}</code>"
                )
                return
            try:
                self._execute_approval(approval)
            except Exception as exc:
                refused = isinstance(exc, (RunnerError, ConflictError))
                self.store.set_approval_outcome(
                    approval["id"], "REFUSED" if refused else "FAILED", f"{type(exc).__name__}: {exc}"
                )
                raise
            if action not in RELEASE_ACTIONS:
                # 반영(merge·push·deploy)은 operations 기록이 실행 여부의 근거라 여기서 남기지 않는다.
                self.store.set_approval_outcome(approval["id"], "EXECUTED")
            return
        if data.startswith("stop:"):
            self._stop_task(data.split(":", 1)[1])
            return
        if data.startswith("cancel:"):
            task_id = data.split(":", 1)[1]
            self.store.transition(task_id, expected_state="AWAITING_START", new_state="CANCELLED")
            self._send(f"취소했습니다: <code>{esc(task_id)}</code>")
            return
        self._watch_callback(data)

    # ------------------------------------------------------------------ 넘기는 일(C)

    def _record_handoff(
        self, to: str, kind: str, text: str, *, todo_id: str | None = None,
        request_id: str | None = None, path: str | None = None,
    ) -> None:
        self.store.add_event(None, None, "HANDOFF_CREATED", "handoff", {
            "to": to, "kind": kind, "text": redact_text(text, 200), "todo_id": todo_id,
            "request_id": request_id, "path": path,
        })

    def _handoffs(self, limit: int = 10) -> list[dict[str, Any]]:
        items = []
        for event in self.store.recent_events_by_type("HANDOFF_CREATED", limit):
            item = dict(event["payload"])
            item["created_at"] = event["created_at"]
            item["status"] = handoff_status(self.paths.agent_hub, item)
            items.append(item)
        return items

    def _handoff_lines(self) -> str:
        items = self._handoffs(6)
        if not items:
            return ""
        lines = [
            f"• [{esc(HANDOFF_STATUS_LABELS[item['status']])}] {esc(item.get('to'))} · "
            f"<code>{esc(item.get('request_id') or item.get('todo_id') or '-')}</code> {esc(str(item.get('text') or '')[:60])}"
            for item in items
        ]
        return "<b>봇이 넘긴 일</b>\n" + "\n".join(lines) + "\n"

    def _visual_request(self, text: str, project: str | None) -> None:
        handoff = create_visual_request(self.paths.agent_hub, self.hub_todo, text, project, self.clock().astimezone(self.alert_config.tz))
        if not handoff.ok:
            where = f" (요청서: <code>{esc(handoff.path)}</code>)" if handoff.path else ""
            self._send(f"⚠️ Codex 에게 넘기지 못했습니다{where}: {esc(str(handoff.output)[-300:])}")
            return
        self._record_handoff("codex", "visual", text, todo_id=handoff.todo_id, request_id=handoff.request_id, path=handoff.path)
        self._send(
            "🎨 <b>Codex 에게 시안 요청을 넘겼습니다</b> (처리 구분 C · 승인 없음)\n"
            f"요청 ID: <code>{esc(handoff.request_id)}</code> · 할 일: <code>{esc(handoff.todo_id or '확인 불가')}</code>\n"
            f"요청서: <code>agent-hub/{esc(handoff.path)}</code>\n"
            "Codex 가 판단 카드(card.md)를 올리면 결론을 텔레그램으로 알려 드립니다. "
            "고른 안은 DeskMini 에서 Blender 로 렌더합니다. 진행 상황: /todo"
        )

    def _check_handoffs(self) -> None:
        """넘긴 일의 결과 받아 오기: 판단 카드가 생기거나 할 일이 닫히면 한 번 알린다(outbox dedupe)."""
        for item in self._handoffs(20):
            key = item.get("request_id") or item.get("todo_id")
            if not key:
                continue
            if item["status"] == "card":
                card = (self.paths.agent_hub / str(item["path"])).with_name("card.md")
                conclusion = card_conclusion(card) or "결론 줄 없음"
                self._notify(
                    f"handoff-card-{key}",
                    f"📬 <b>{esc(item.get('to'))} 판단 카드 도착</b>: <code>{esc(key)}</code>\n"
                    f"결론: {esc(conclusion)}\n<code>agent-hub/{esc(str(item['path']).rsplit('/', 1)[0])}/card.md</code>",
                )
            elif item["status"] == "done":
                self._notify(
                    f"handoff-done-{key}",
                    f"✅ <b>{esc(item.get('to'))}</b> 가 넘긴 일을 닫았습니다: <code>{esc(key)}</code>\n"
                    f"• {esc(str(item.get('text') or '')[:200])}",
                )
        # 봇이 만들지 않은 요청(Claude·대표가 직접 쓴 request.md)의 판단 카드도 알린다. dedupe 키가 같아 중복되지 않는다.
        visual_root = self.paths.agent_hub / "visual"
        for card in sorted(visual_root.glob("*/*/card.md"))[-50:]:
            key = card.parent.name
            if card.is_symlink() or not SAFE_REL_RE.fullmatch(f"visual/{card.parent.parent.name}/{key}/request.md"):
                continue
            conclusion = card_conclusion(card) or "결론 줄 없음"
            images = len([p for p in (card.parent / "outputs").glob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}])
            self._notify(
                f"handoff-card-{key}",
                f"📬 <b>판단 카드 도착</b>: <code>{esc(key)}</code>\n"
                f"결론: {esc(conclusion)}\n이미지 {images}장 · <code>agent-hub/visual/{esc(card.parent.parent.name)}/{esc(key)}/card.md</code>",
            )

    def _execute_approval(self, approval: dict[str, Any]) -> None:
        """소비된 승인을 실행한다. 거절(RunnerError·ConflictError)은 예외로 올리고, 재시도 거절은 직접 안내한다."""
        action = approval["action"]
        if action == "start":
            task = self.store.get_task(approval["task_id"])
            if approval["payload"] != self._start_payload(task):
                raise ConflictError("Approved start payload no longer matches task")
            try:
                started = self.runner.start(task["id"])
            except RunnerError as exc:
                if str(exc) != "Another task is active":
                    raise
                # 다른 작업이 진행 중: 이 승인은 거절로 끝내고(재시작 때 몰래 착수하지 않는다) 새 착수 버튼을 준다.
                self.store.set_approval_outcome(approval["id"], "REFUSED", str(exc))
                self._offer_start_again(task, self._busy_text("착수하지"))
                return
            self._update_task_card(
                started["id"],
                self._task_text(started["id"]),
                reply_markup=self._running_keyboard(started["id"]),
            )
        elif action == "retry":
            self._execute_retry(approval)
        elif action == "schedule_retry":
            self._accept_scheduled_retry(approval)
        elif action in RELEASE_ACTIONS:
            future = self.executor.submit(self.release.execute, approval)
            self.release_futures[approval["task_id"]] = future
            self._send(f"⏳ <b>{esc(action)}</b> 실행을 시작했습니다: <code>{esc(approval['task_id'])}</code>")
        else:
            raise ConflictError("Approval action mismatch")

    def _offer_start_again(self, task: dict[str, Any], reason: str) -> None:
        approval_id = self.store.create_approval(
            task_id=task["id"],
            action="start",
            payload=self._start_payload(task),
            owner_user_id=self.settings.allowed_user_id,
            chat_id=self.settings.allowed_chat_id,
            task_version=task["version"],
            ttl_seconds=self.settings.approval_ttl_seconds,
        )
        self._send(
            f"⏸️ <code>{esc(task['id'])}</code> · {esc(reason)}\n"
            f"버튼 유효시간: {self.settings.approval_ttl_seconds // 60}분",
            reply_markup=inline_keyboard(
                [[("🚀 착수", f"approve:start:{approval_id}")], [("취소", f"cancel:{task['id']}")]]
            ),
        )

    def _busy_text(self, verb: str) -> str:
        active = [task["id"] for task in self.store.list_tasks_in_states(ACTIVE_STATES)]
        who = ", ".join(active[:2]) or "…"
        return f"다른 작업({who})이 진행 중이라 {verb} 못했습니다. 끝나면 다시 시도해 주세요."

    def _refusal_text(self, exc: Exception) -> str:
        """예상된 거절(ConflictError·RunnerError)을 사용자 말로 바꾼다. 모르는 문구는 원인을 짧게 붙인다."""
        message = str(exc)
        if message == "Approval expired":
            return "⌛ 승인 유효시간이 지났습니다. 상태 카드에서 다시 요청해 주세요."
        if message == "Approval already used":
            return "ℹ️ 이미 처리된 버튼입니다(승인은 한 번만 쓸 수 있습니다). 현재 상태는 /tasks 에서 확인해 주세요."
        if message in {"Approval not found", "Approval action mismatch", "Approval owner mismatch"}:
            return "⚠️ 확인할 수 없는 승인 버튼입니다. 상태 카드에서 다시 요청해 주세요."
        if message in {"Task changed after approval", "Task version changed", "Task changed after the retry was approved"} or (
            "no longer matches task" in message
        ):
            return "⚠️ 승인 뒤 작업 상태가 바뀌어 이 버튼은 더 이상 쓸 수 없습니다. 상태 카드에서 다시 요청해 주세요."
        if message == "Another task is active":
            return "⏸️ " + self._busy_text("처리하지")
        expected = re.fullmatch(r"Expected (\w+), got (\w+)", message)
        if expected:
            return f"⚠️ 작업이 지금 <code>{esc(expected.group(2))}</code> 상태라 이 요청을 처리할 수 없습니다."
        return f"⚠️ 처리하지 못했습니다: {esc(redact_text(message, 300))}"

    def _reconcile_tasks(self) -> None:
        for task in self.store.list_tasks_in_states(ACTIVE_STATES):
            task_id = task["id"]
            if task["state"] in {"STARTING", "RUNNING", "STOP_REQUESTED", "STOPPING"}:
                previous = task["state"]
                current = self.runner.reconcile(task_id)
                if current != previous:
                    self._update_task_card(task_id, self._task_text(task_id))
                if current in {"FAILED", "BLOCKED"}:
                    # 작업자 단계 실패·한도 차단: 이유와 [다시 시도]/예약 버튼을 바로 보낸다.
                    self._offer_retry(task_id)
                    self.alerts.record_bot_notice(f"task:{task_id}:{current}", self.clock())
            refreshed_for_card = self.store.get_task(task_id)
            if refreshed_for_card["state"] == "RUNNING":
                last = self.last_card_update.get(task_id, 0.0)
                if time.monotonic() - last >= 30:
                    self._update_task_card(
                        task_id,
                        self._task_text(task_id),
                        reply_markup=self._running_keyboard(task_id),
                    )
            refreshed = self.store.get_task(task_id)
            if refreshed["state"] == "VERIFYING" and task_id not in self.verification_futures:
                self.verification_futures[task_id] = self.executor.submit(self.verifier.verify, task_id)

    def _process_futures(self) -> None:
        for task_id, future in list(self.verification_futures.items()):
            if not future.done():
                continue
            del self.verification_futures[task_id]
            try:
                task = future.result()
            except Exception as exc:
                LOG.error("Verification failed: %s", type(exc).__name__)
                self._send(f"⚠️ 검증 처리 오류: <code>{esc(task_id)}</code> · {esc(type(exc).__name__)}")
                continue
            if task["state"] == "READY":
                self._offer_release(task_id, "merge")
            elif task["state"] in {"FAILED", "BLOCKED"}:
                self._offer_retry(task_id)
            else:
                self._send(self._task_text(task_id))
            # 이미 알렸으니 감시기가 같은 전이를 다시 알리지 않게 남긴다.
            self.alerts.record_bot_notice(f"task:{task_id}:{task['state']}", self.clock())

        for task_id, future in list(self.release_futures.items()):
            if not future.done():
                continue
            del self.release_futures[task_id]
            try:
                operation = future.result()
            except Exception as exc:
                LOG.error("Release operation failed: %s", type(exc).__name__)
                self._send(f"⚠️ 반영 처리 오류: <code>{esc(task_id)}</code> · {esc(type(exc).__name__)}")
                continue
            self._send(self._operation_text(operation))
            if operation["state"] == "SUCCEEDED" and operation["kind"] == "merge":
                self._offer_release(task_id, "push")
            elif operation["state"] == "SUCCEEDED" and operation["kind"] == "push":
                project = self.settings.projects[self.store.get_task(task_id)["project_id"]]
                if project.deploy_enabled:
                    self._offer_release(task_id, "deploy")

    def _offer_release(self, task_id: str, action: str) -> None:
        try:
            payload = self.release.approval_payload(task_id, action)
            task = self.store.get_task(task_id)
            approval_id = self.store.create_approval(
                task_id=task_id,
                action=action,
                payload=payload,
                owner_user_id=self.settings.allowed_user_id,
                chat_id=self.settings.allowed_chat_id,
                task_version=task["version"],
                ttl_seconds=self.settings.approval_ttl_seconds,
            )
        except ReleaseRefused as exc:
            self._send(esc(exc))
            return
        except (ReleaseError, KeyError) as exc:
            self._send(f"{esc(action)} 승인 카드를 만들 수 없습니다: {esc(exc)}")
            return
        labels = {"merge": "main 병합", "push": "원격 Push", "deploy": "스테이징 배포"}
        self._send(
            f"<b>{esc(labels[action])} 승인</b>\n\n"
            f"작업: <code>{esc(task_id)}</code>\n"
            f"SHA: <code>{esc(payload['head_sha'][:12])}</code>\n"
            f"승인 유효시간: {self.settings.approval_ttl_seconds // 60}분",
            reply_markup=inline_keyboard(
                [[(f"승인: {labels[action]}", f"approve:{action}:{approval_id}")]]
            ),
        )

    def _stop_task(self, task_id: str) -> None:
        try:
            task = self.store.get_task(task_id)
            if task["state"] == "VERIFYING":
                self.verifier.cancel(task_id)
                future = self.verification_futures.get(task_id)
                if future:
                    future.cancel()
            self.runner.stop(task_id)
        except (RunnerError, KeyError, ConflictError) as exc:
            self._send(f"중단하지 못했습니다: <code>{esc(task_id)}</code> · {esc(exc)}")
            return
        self._update_task_card(
            task_id,
            f"🛑 <b>종료 확인</b>\n\n산출물은 보존했습니다: <code>{esc(task_id)}</code>",
        )

    # ------------------------------------------------------------------ 재시도

    def _retry_payload(self, task: dict[str, Any], action: str, run_at: str | None = None) -> dict[str, Any]:
        payload = {
            "task_id": task["id"],
            "task_version": task["version"],
            "action": action,
            "mode": self.runner.retry_mode(task),
        }
        if run_at is not None:
            payload["run_at"] = run_at
        return payload

    def _failure_summary(self, task: dict[str, Any]) -> tuple[str, datetime | None]:
        """(사용자에게 보일 실패 이유 HTML, 한도가 풀리는 시각). 원문은 마스킹 후 짧게 자른다."""
        run = self.store.latest_run(task["id"]) or {}
        result = run.get("result") or {}
        classification = str(result.get("classification") or "")
        lines: list[str] = []
        reset_at: datetime | None = None
        if task["state"] == "BLOCKED" and classification == "LIMIT_BLOCKED":
            error = str(result.get("error") or "")
            reset_at = self._limit_reset_at(task, run, error)
            lines.append("Claude 사용량 한도에 걸려 작업자가 일을 시작하지 못했습니다.")
            if reset_at is not None:
                local = reset_at.astimezone(self.alert_config.tz)
                passed = " — 이미 지났습니다" if not reset_pending(reset_at, self.clock()) else ""
                lines.append(f"풀리는 시각: <b>{local:%m/%d %H:%M}</b> (한국 시각){passed}")
            else:
                lines.append("풀리는 시각은 메시지에서 확인하지 못했습니다.")
            lines.append(f"원문: <code>{esc(error[:200])}</code>")
            return "\n".join(lines), reset_at
        evidence_path = self.settings.state_dir / "evidence" / f"{task['id']}-verification.json"
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            evidence = {}
        worker_failed = classification and classification != "SUCCESS"
        if worker_failed:
            lines.append(f"작업자 실패: <code>{esc(classification)}</code>")
            error = str(result.get("error") or "")
            if error:
                lines.append(f"<code>{esc(error[:300])}</code>")
        elif evidence.get("verdict") in {"FAIL", "UNVERIFIED"}:
            reason = str(evidence.get("reason") or "")
            label = {
                "One or more registered checks failed": "등록된 검증 명령 실패",
                "Independent review did not pass": "독립 검토 불통과",
                "Worker produced no file changes": "작업자가 파일을 바꾸지 않음",
            }.get(reason, reason)
            if evidence.get("verdict") == "UNVERIFIED":
                label = "독립 검토를 하지 못함(검토 제공자 응답 없음) — 검증만 다시 할 수 있습니다"
            lines.append(f"검증 결과: {esc(label)}")
            for check in [item for item in evidence.get("checks", []) if not item.get("passed")][:2]:
                command = " ".join(str(part) for part in check.get("argv") or [])
                tail = "\n".join(str(check.get("output") or "").strip().splitlines()[-6:])
                lines.append(f"• <code>{esc(command)}</code>\n<pre>{esc(tail[-500:])}</pre>")
            if evidence.get("review_summary"):
                lines.append(f"검토 요약({esc(evidence.get('review_verdict') or '')}): {esc(str(evidence['review_summary'])[:300])}")
            for finding in (evidence.get("review_findings") or [])[:3]:
                lines.append(f"• {esc(str(finding)[:200])}")
        else:
            lines.append(f"상태: {esc(task['state'])} (자세한 이유 기록 없음)")
        return "\n".join(lines), None

    def _limit_reset_at(self, task: dict[str, Any], run: dict[str, Any], error: str) -> datetime | None:
        """한도가 풀리는 시각. "resets 9am" 같은 시각은 한도 메시지를 받은 때(실행 종료 시각) 기준으로 가장 가까운 시각이다.
        지금 기준으로 읽으면 이미 지난 시각이 '내일 같은 시각'이 된다. 같은 작업 버전에 이미 예약을 제안했고
        그 시각이 지났으면 그것을 우선한다(같은 문구를 다시 읽어 하루 뒤로 미루지 않는다)."""
        now = self.clock()
        reference = now
        try:
            ended = datetime.fromisoformat(str(run.get("ended_at") or ""))
            if ended.tzinfo is not None and ended <= now:
                reference = ended
        except ValueError:
            pass
        reset_at = parse_reset_time(error, reference)
        for run_at in self.store.proposed_retry_times(task["id"], int(task["version"])):
            try:
                proposed = datetime.fromisoformat(run_at) - RETRY_MARGIN
            except ValueError:
                continue
            if proposed <= now and (reset_at is None or reset_at > now):
                reset_at = proposed
        return reset_at

    def _offer_retry(self, task_id: str) -> None:
        """실패·차단 카드: 이유 + [다시 시도] 또는 한도 해제 시각 예약 버튼(일회용 승인, 작업 버전에 묶임)."""
        try:
            task = self.store.get_task(task_id)
        except KeyError:
            self._send(f"작업을 찾지 못했습니다: <code>{esc(task_id)}</code>")
            return
        icon = "⛔" if task["state"] == "BLOCKED" else "❌"
        reason, reset_at = self._failure_summary(task)
        head = f"{icon} <b>{esc(task['state'])}</b> · <code>{esc(task_id)}</code> ({esc(task['project_id'])})\n\n{reason}"
        allowed, why = self.runner.can_retry(task)
        if not allowed:
            self._send(_fit(head + f"\n\n{esc(why)}."))
            return
        used, budget = self.runner.retry_budget(task)
        mode = self.runner.retry_mode(task)
        now = self.clock()
        if reset_at is not None and reset_pending(reset_at, now):
            run_at = scheduled_time(reset_at)
            payload = self._retry_payload(task, "schedule_retry", run_at.isoformat(timespec="seconds"))
            label = f"⏰ {run_at.astimezone(self.alert_config.tz):%m/%d %H:%M} 에 다시 시도 예약"
            action = "schedule_retry"
            note = "한도가 풀리기 전이라 바로 다시 시도하지 않고 예약을 제안합니다. 누르면 그 시각에 봇이 한 번 다시 실행합니다."
        else:
            payload = self._retry_payload(task, "retry")
            label = "🔁 검증 다시 시도" if mode == "verify" else "🔁 다시 시도"
            action = "retry"
            if mode == "verify":
                verify_used, verify_cap = self.runner.verify_retry_budget(task)
                note = f"작업자는 끝났고 검토만 못 했으니 검증만 다시 합니다. 검증 재시도 {verify_used}/{verify_cap}회 사용."
            else:
                note = f"이전 변경을 이어서 실패 이유를 고치게 합니다. 재시도 {used}/{budget}회 사용."
            if reset_at is not None:
                note = "한도가 풀리는 시각이 지났으니 바로 다시 시도할 수 있습니다. " + note
        approval_id = self.store.create_approval(
            task_id=task_id,
            action=action,
            payload=payload,
            owner_user_id=self.settings.allowed_user_id,
            chat_id=self.settings.allowed_chat_id,
            task_version=task["version"],
            ttl_seconds=self.settings.approval_ttl_seconds,
        )
        self._send(
            _fit(head + f"\n\n{esc(note)}\n버튼 유효시간: {self.settings.approval_ttl_seconds // 60}분 · 다시 띄우기 <code>/retry {esc(task_id)}</code>"),
            reply_markup=inline_keyboard([[(label, f"approve:{action}:{approval_id}")]]),
        )

    def _execute_retry(self, approval: dict[str, Any]) -> None:
        task = self.store.get_task(approval["task_id"])
        if approval["payload"] != self._retry_payload(task, "retry"):
            raise ConflictError("Approved retry payload no longer matches task")
        try:
            started = self.runner.retry(task["id"], expected_version=approval["payload"]["task_version"])
        except (RunnerError, ConflictError) as exc:
            # 거절로 끝난 승인은 재시작 복구가 몰래 다시 실행하지 않는다. 다시 하려면 /retry 로 새 버튼을 띄운다.
            self.store.set_approval_outcome(approval["id"], "REFUSED", f"{type(exc).__name__}: {exc}")
            if str(exc) == "Another task is active":
                why = esc(self._busy_text("시작하지"))
            elif isinstance(exc, RunnerError) and not str(exc).isascii():
                why = esc(exc)  # can_retry 의 한국어 사유(재시도 한도 등)
            else:
                why = self._refusal_text(exc)
            self._send(
                f"🔁 다시 시도하지 못했습니다: <code>{esc(task['id'])}</code>\n{why}\n"
                f"다시 하려면 <code>/retry {esc(task['id'])}</code>"
            )
            return
        if started["state"] == "VERIFYING":
            self._send(f"🔁 검증을 다시 시작합니다: <code>{esc(task['id'])}</code>")
            return
        self._update_task_card(
            started["id"], self._task_text(started["id"]), reply_markup=self._running_keyboard(started["id"])
        )

    def _accept_scheduled_retry(self, approval: dict[str, Any]) -> None:
        task = self.store.get_task(approval["task_id"])
        run_at = str(approval["payload"].get("run_at") or "")
        if approval["payload"] != self._retry_payload(task, "schedule_retry", run_at):
            raise ConflictError("Approved retry payload no longer matches task")
        self.store.schedule_retry(
            task_id=task["id"],
            approval_id=approval["id"],
            task_version=int(task["version"]),
            run_at=run_at,
            note="limit reset",
        )
        local = datetime.fromisoformat(run_at).astimezone(self.alert_config.tz)
        self._send(
            f"⏰ 예약했습니다: <code>{esc(task['id'])}</code> 를 <b>{local:%m/%d %H:%M}</b> 에 한 번 다시 시도합니다.\n"
            "그 사이 작업이 바뀌면(중단·다른 재시도) 예약은 실행하지 않습니다."
        )

    def _run_scheduled_retries(self) -> None:
        now = self.clock()
        for item in self.store.due_retries(now.isoformat(timespec="seconds")):
            try:
                task = self.store.get_task(item["task_id"])
            except KeyError:
                self.store.finish_scheduled_retry(item["id"], "SKIPPED", "task missing")
                continue
            if int(task["version"]) != int(item["task_version"]) or task["state"] not in {"FAILED", "BLOCKED"}:
                self.store.finish_scheduled_retry(item["id"], "SKIPPED", f"task is {task['state']}")
                continue
            if self.store.active_task_count() >= self.settings.max_active_tasks:
                continue  # 다른 작업이 끝나면 다음 루프에서 실행한다
            if not self.store.finish_scheduled_retry(item["id"], "STARTED"):
                continue
            try:
                started = self.runner.retry(task["id"], expected_version=item["task_version"])
            except (RunnerError, ConflictError) as exc:
                self.store.finish_scheduled_retry(item["id"], "FAILED", str(exc))
                self._notify(
                    f"scheduled-retry-{item['id']}",
                    f"⚠️ 예약한 재시도를 실행하지 못했습니다: <code>{esc(task['id'])}</code> · {esc(exc)}",
                )
                continue
            self._notify(
                f"scheduled-retry-{item['id']}",
                f"⏰ 예약한 재시도를 시작했습니다: <code>{esc(task['id'])}</code> · {esc(started['state'])}",
            )

    @property
    def hub_todo(self) -> HubTodo:
        """agent-hub 할 일. 테스트는 self._hub_todo 를 바꿔 끼운다."""
        override = getattr(self, "_hub_todo", None)
        return override if override is not None else HubTodo(self.paths.agent_hub)

    def _start_payload(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": task["id"],
            "task_version": task["version"],
            "project_id": task["project_id"],
            "policy_hash": task["policy_hash"],
            "action": "start",
        }

    def _status_text(self) -> str:
        status = collect_host_status()
        memory = status["memory"]
        docker = status["docker"]
        containers = docker.get("containers") or []
        container_lines = [
            f"• {esc(item.get('name'))}: {esc(item.get('status') or item.get('state'))}"
            for item in containers[:8]
        ] or ["• 확인된 컨테이너 없음"]
        if not docker.get("ok"):
            container_lines = [f"• 미확인: {esc(docker.get('error') or 'Docker 조회 실패')}"]
        active = self.store.list_tasks_in_states(ACTIVE_STATES)
        task_lines = [f"• <code>{esc(task['id'])}</code>: {esc(task['state'])}" for task in active] or ["• 진행 중 작업 없음"]
        conclusion = "관리 작업 실행 중" if active else "관리 작업 대기 중"
        unconfirmed = []
        if not docker.get("ok"):
            unconfirmed.append("Docker 컨테이너 상태")
        if status["remote_control"]["tmux_session"] == "unknown":
            unconfirmed.append("Remote Control 세션")
        return (
            "<b>결론</b>\n"
            f"{esc(conclusion)}\n\n"
            "<b>관측 증거</b>\n"
            f"관측 시각: <code>{esc(status['observed_at'])}</code>\n"
            f"호스트: <code>{esc(status['hostname'])}</code>\n"
            f"부하: <code>{esc(status['load_average'])}</code>\n"
            f"메모리 여유: <code>{esc(bytes_human(memory.get('available')))}</code> / {esc(bytes_human(memory.get('total')))}\n"
            f"디스크 여유: <code>{esc(bytes_human(status['disk']['free']))}</code>\n"
            f"Remote Control: <code>{esc(status['remote_control']['tmux_session'])}</code>\n\n"
            "<b>컨테이너</b>\n" + "\n".join(container_lines) + "\n\n"
            "<b>관리 작업</b>\n" + "\n".join(task_lines) + "\n\n"
            "<b>미확인</b>\n"
            + ("• " + "\n• ".join(unconfirmed) if unconfirmed else "• 없음")
            + "\n\n<b>다음 행동</b>\n• 작업 상세는 <code>/task T-...</code>"
        )

    def _tasks_text(self) -> str:
        tasks = self.store.list_tasks(10)
        if not tasks:
            return "등록된 작업이 없습니다."
        lines = ["<b>최근 작업</b>"]
        lines.extend(
            f"• <code>{esc(task['id'])}</code> · {esc(task['project_id'])} · <b>{esc(task['state'])}</b>"
            for task in tasks
        )
        return "\n".join(lines)

    def _task_text(self, task_id: str) -> str:
        try:
            task = self.store.get_task(task_id)
        except KeyError:
            return f"작업을 찾지 못했습니다: <code>{esc(task_id)}</code>"
        run = self.store.latest_run(task_id)
        events = self.store.recent_events(task_id, 3)
        event_lines = [
            f"• {esc(event['created_at'])} · {esc(event['event_type'])}"
            for event in events
        ] or ["• 이벤트 없음"]
        conclusion = {
            "READY": "검증 통과 — 병합 승인 대기",
            "BLOCKED": "외부 제약으로 중단",
            "FAILED": "검증 또는 실행 실패",
            "CANCELLED": "사용자 요청으로 중단",
        }.get(task["state"], "작업 진행 상태 확인")
        text = (
            f"<b>결론</b>\n{esc(conclusion)}\n\n"
            f"<b>관측 증거 · {esc(task_id)}</b>\n"
            f"프로젝트: <code>{esc(task['project_id'])}</code>\n"
            f"상태: <b>{esc(task['state'])}</b>\n"
            f"버전: {esc(task['version'])}\n"
            f"기준 SHA: <code>{esc((task.get('base_sha') or '미확인')[:12])}</code>\n"
            f"결과 SHA: <code>{esc((task.get('head_sha') or '미확인')[:12])}</code>\n"
        )
        if run:
            text += (
                f"실행: <code>{esc(run['id'])}</code>\n"
                f"실행 상태: {esc(run['state'])}\n"
                f"마지막 생존 신호: {esc(run.get('heartbeat_at') or '미확인')}\n"
            )
        text += "\n<b>최근 증거</b>\n" + "\n".join(event_lines)
        text += "\n\n<b>미확인</b>\n"
        text += "• 없음" if task["state"] in {"READY", "CANCELLED"} else "• 최종 반영 전"
        text += "\n\n<b>다음 행동</b>\n"
        if task["state"] == "READY":
            text += "• 병합 승인"
        elif task["state"] in {"FAILED", "BLOCKED"}:
            text += f"• 이유 확인·다시 시도: <code>/retry {esc(task_id)}</code>"
        else:
            text += "• 상태 카드의 가능한 동작 확인"
        return text

    def _task_confirmation_text(self, task: dict[str, Any]) -> str:
        spec = task["specification"]
        boundaries = "\n".join(f"• {esc(item)}" for item in spec.get("boundaries", [])) or "• 없음"
        acceptance = "\n".join(f"• {esc(item)}" for item in spec.get("acceptance", [])) or "• 등록된 검증 통과"
        attachments = self.store.attachments_for_task(task["id"])
        attached = (
            "\n\n<b>첨부</b> (작업 폴더 .chief-inbox/, 커밋 안 됨)\n"
            + "\n".join(f"• {esc(describe(item))}" for item in attachments)
            if attachments
            else ""
        )
        return (
            "<b>작업 발주 확인</b>\n\n"
            f"ID: <code>{esc(task['id'])}</code>\n"
            f"프로젝트: <code>{esc(task['project_id'])}</code>\n"
            f"목표: {esc(spec.get('objective', ''))}\n"
            f"산출물: {esc(spec.get('output_format', ''))}\n\n"
            f"<b>범위 밖</b>\n{boundaries}\n\n"
            f"<b>완료 기준</b>\n{acceptance}"
            + attached
        )

    def _operation_text(self, operation: dict[str, Any]) -> str:
        label = {"merge": "로컬 병합", "push": "원격 Push", "deploy": "배포"}.get(
            operation["kind"], operation["kind"]
        )
        return (
            f"<b>결론 · {esc(label)}</b>\n"
            f"판정: <b>{esc(operation['state'])}</b>\n\n"
            "<b>관측 증거</b>\n"
            f"작업: <code>{esc(operation['task_id'])}</code>\n"
            f"이전: <code>{esc(redact_text(json.dumps(operation.get('before_evidence'), ensure_ascii=False))[:700])}</code>\n"
            f"이후: <code>{esc(redact_text(json.dumps(operation.get('after_evidence'), ensure_ascii=False))[:900])}</code>\n\n"
            "<b>미확인</b>\n"
            + ("• 없음" if operation["state"] in {"SUCCEEDED", "FAILED"} else "• 작업 중단 시점의 외부 상태")
            + "\n\n<b>다음 행동</b>\n"
            + ("• 다음 승인 단계를 확인" if operation["state"] == "SUCCEEDED" else "• 증거 확인 후 새 승인")
        )

    def _running_keyboard(self, task_id: str) -> dict[str, Any]:
        return inline_keyboard([[("⛔ 중단 요청", f"stop:{task_id}")]])

    def _help_text(self) -> str:
        return (
            "<b>DeskMini 비서실장</b>\n\n"
            "/status — 서버 및 작업 상태\n"
            "/tasks — 최근 작업\n"
            "/task T-... — 작업 상세\n"
            "/new 작업내용 — 새 작업 발주\n"
            "/stop T-... — 작업 중단\n"
            "/hub — Codex·Claude 공용 작업 기록과 잠금\n"
            "/git — 저장소별 최근 커밋\n"
            "/work — ~/work 최근 작업 폴더\n"
            "/model — 현재 대화 모델\n"
            "/codex — DeskMini Codex 사용 한도\n"
            "/jobs — 장시간 작업(렌더) 상태\n"
            "/files 작업 — 결과물 받기(버튼)\n"
            "/logs 작업 — 작업 로그 끝 30줄\n"
            "/quiet 2h — 잠시 조용히(최대 12시간, /quiet off 해제)\n"
            "/alerts on|off — 먼저 알리기 켜기(조용히도 해제)·끄기, /alerts 현재 설정\n"
            "   off 여도 치명적인 알림(컨테이너 멈춤·디스크·메모리 부족)은 즉시, 아침 요약은 계속 옵니다\n"
            "/brief — 아침 요약 미리 보기\n"
            "/retry T-... — 실패·차단 작업 다시 시도(버튼 승인)\n"
            "/orca — Orca 접수·상태 조회(/orca new trial codex 작업 내용)\n"
            "/todo codex 할 일 — Codex·Claude 에게 할 일 남기기(/todo 목록, /todo done 번호)\n"
            "사진·문서 + 설명 — 첨부로 작업 발주(캡션 없으면 무엇을 할지 묻습니다)\n"
            f"/new 프로젝트: 내용 — 프로젝트 지정 발주({esc(', '.join(self.settings.projects))}, 기본 {esc(self.policy.default_project)})\n\n"
            "그 밖의 말은 대화로 답합니다. 작업 착수·중단·병합·Push·배포는 항상 버튼 승인을 거칩니다.\n"
            "작업·서버 상태가 바뀌면 먼저 알립니다(23:00–07:00 은 치명적인 것만, 나머지는 08:30 요약)."
        )

    def _hub_text(self) -> str:
        board = board_text(self.paths, 1_800) or "BOARD.md 를 읽지 못했습니다."
        locks = hub_locks(self.paths)
        logs = recent_hub_logs(self.paths, 5, 300)
        lock_lines = [f"• {esc(item)}" for item in locks] or ["• 없음"]
        log_lines = [f"• <code>{esc(item['file'][:15])}</code> {esc(item['text'])}" for item in logs] or ["• 기록 없음"]
        handoffs = self._handoff_lines()
        return _fit(
            (handoffs + "\n" if handoffs else "")
            + "<b>공용 작업 잠금</b>\n" + "\n".join(lock_lines)
            + "\n\n<b>최근 작업 기록</b>\n" + "\n".join(log_lines)
            + "\n\n<b>BOARD 요약</b>\n<pre>" + esc(board) + "</pre>"
        )

    def _git_text(self) -> str:
        repos = registered_repos(self.paths)
        if not repos:
            return "등록된 저장소 목록(agent-hub/hosts/ionenet.conf)을 읽지 못했습니다."
        blocks = []
        for name, path in repos:
            summary = repo_summary(path)
            header = f"<b>{esc(name)}</b> <code>{esc(home_relative(path, self.paths.home))}</code>"
            if not summary["ok"]:
                blocks.append(f"{header}\n• 미확인: {esc(summary.get('error') or '')}")
                continue
            dirty = summary["uncommitted"]
            commits = "\n".join(f"• <code>{esc(line)}</code>" for line in summary["commits"]) or "• 커밋 없음"
            blocks.append(
                f"{header}\n{commits}\n미커밋 변경: {esc('미확인' if dirty is None else f'{dirty}개')}"
            )
        return _fit("\n\n".join(blocks))

    def _work_text(self) -> str:
        dirs = recent_work_dirs(self.paths, 8)
        if not dirs:
            return f"<code>{esc(home_relative(self.paths.work, self.paths.home))}</code> 아래에 작업 폴더가 없거나 읽지 못했습니다."
        lines = [f"• {esc(item['modified'])} · <code>{esc(item['name'])}</code>" for item in dirs]
        return _fit("<b>최근 수정된 작업 폴더</b> (~/work)\n" + "\n".join(lines))

    def _codex_text(self) -> str:
        limits = latest_codex_limits(self.paths.home)
        if not limits:
            return "DeskMini Codex 사용 기록이 아직 없어 한도를 확인할 수 없습니다. Codex 가 한 번 실행되면 표시됩니다."
        tz = self.alert_config.tz
        lines = ["<b>DeskMini Codex 사용 한도</b>"]
        for key in ("primary", "secondary"):
            window = limits.get(key)
            if not window:
                continue
            left = max(0.0, 100.0 - window["used_percent"])
            reset = window["resets_at"].astimezone(tz).strftime("%m-%d %H:%M") if window["resets_at"] else "미확인"
            lines.append(
                f"• {esc(window_label(window['window_minutes']))}: {window['used_percent']:.0f}% 사용 · "
                f"<b>{left:.0f}% 남음</b> · 초기화 {esc(reset)}"
            )
        if limits.get("limit_reached"):
            lines.append(f"⚠️ 한도 도달: {esc(limits['limit_reached'])}")
        observed = limits["observed_at"].astimezone(tz).strftime("%m-%d %H:%M")
        lines.append(f"\n관측: 마지막 Codex 실행 {esc(observed)} 기준 (요금제 {esc(limits.get('plan_type') or '미확인')})")
        lines.append("메인 PC Codex 도 같은 계정이면 한도를 함께 씁니다. 그 사용분은 다음 DeskMini 실행 때 반영됩니다.")
        return "\n".join(lines)

    def _model_text(self) -> str:
        order = self.chat.active_providers()
        if order:
            provider, model = order[0]
            label = "Claude API" if provider == "claude" else "Gemini 무료 REST"
            active = f"{label} · <code>{esc(model)}</code>"
        else:
            active = "사용 가능한 대화 모델 없음 (고정 명령만 동작)"
        fallback = " → ".join(f"{name}({model})" for name, model in order[1:]) or "없음"
        key_state = "있음" if any(name == "claude" for name, _ in order) else "없음"
        last = self.store.recent_events_by_type("CHAT_DECISION", 1)
        last_line = "없음"
        if last:
            payload = last[0]["payload"]
            last_line = f"{payload.get('provider')} · {payload.get('model')} · {last[0]['created_at']}"
        return (
            f"<b>현재 대화 모델</b>\n{active}\n\n"
            f"예비: {esc(fallback)}\n"
            f"Claude API 키: {key_state} (값은 표시하지 않음)\n"
            f"마지막 응답: {esc(last_line)}"
        )

    # ------------------------------------------------------------------ 감시기·알림

    @property
    def file_roots(self) -> tuple[Path, ...]:
        """결과물·로그를 읽을 수 있는 허용 루트."""
        return (self.paths.work, self.settings.state_dir / "evidence")

    def _scan_jobs(self, now: datetime) -> list[JobObservation]:
        return discover_jobs(self.jobs_config, self.paths.home, now.timestamp())

    def observe(self, now: datetime) -> Observation:
        """실제 서버 관측(읽기 전용, 고정 argv·짧은 타임아웃)."""
        status = collect_host_status(self.alert_config.tmux_session)
        docker = status["docker"]
        containers = (
            {str(item.get("name")): str(item.get("state") or "") for item in docker.get("containers") or []}
            if docker.get("ok")
            else None
        )
        tasks: dict[str, dict[str, Any]] = {}
        for task in self.store.list_tasks(20):
            info: dict[str, Any] = {"state": task["state"]}
            if task["state"] == "BLOCKED":
                run = self.store.latest_run(task["id"])
                info["classification"] = ((run or {}).get("result") or {}).get("classification")
            tasks[task["id"]] = info
        return Observation(
            at=now,
            containers=containers,
            disk_free=status["disk"]["free"],
            disk_total=status["disk"]["total"],
            mem_available=status["memory"].get("available"),
            mem_total=status["memory"].get("total"),
            tmux=status["remote_control"]["tmux_session"],
            tasks=tasks,
            jobs={job.key: job for job in self.job_scanner(now)},
        )

    def _maybe_watch(self) -> None:
        current = time.monotonic()
        if (
            self.last_watch_monotonic is not None
            and current - self.last_watch_monotonic < self.alert_config.interval_seconds
        ):
            return
        self.last_watch_monotonic = current
        try:
            self.watch_tick()
        except Exception as exc:  # 감시 실패가 폴링을 막지 않게 한다.
            LOG.error("Watch tick failed: %s", type(exc).__name__)
        try:
            self.brief_tick()
        except Exception as exc:
            LOG.error("Morning brief failed: %s", type(exc).__name__)
        try:
            self._check_handoffs()
        except Exception as exc:
            LOG.error("Hand-off check failed: %s", type(exc).__name__)

    def watch_tick(
        self, now: datetime | None = None, observation: Observation | None = None
    ) -> list[dict[str, Any]]:
        """관측 1회 → 변화 감지 → 알림 규칙 → outbox. 처리한 알림 목록을 돌려준다(시뮬레이터용)."""
        now = now or self.clock()
        obs = observation if observation is not None else self.observer(now)
        previous = self.alerts.get_state("observed")
        busy = set(self.verification_futures) | set(self.release_futures)
        events, state = detect(obs, previous, self.alert_config, busy_tasks=busy)
        for job in obs.jobs.values():
            self.alerts.upsert_job(job.key, job.path, job.label, job.status, now)
        self.alerts.set_state("observed", state, now)
        results = [self._dispatch_alert(event, now, obs) for event in events]
        digest = self._flush_held_alerts(now)
        if digest:
            results.append(digest)
        return results

    def _dispatch_alert(self, event: AlertEvent, now: datetime, obs: Observation) -> dict[str, Any]:
        prefs = self.alerts.prefs(self.alert_config.enabled)
        status, reason = decide_delivery(
            event,
            now=now,
            config=self.alert_config,
            prefs=prefs,
            last_same_key_at=self.alerts.last_key_time(event.key),
            sent_last_hour=self.alerts.sent_since(now - timedelta(hours=1)),
        )
        alert_id = self.alerts.record(event, status=status, reason=reason, now=now)
        result: dict[str, Any] = {
            "id": alert_id,
            "key": event.key,
            "severity": event.severity,
            "kind": event.kind,
            "status": status,
            "reason": reason,
        }
        if status != "SENT":
            return result
        job_id = None
        has_deliverables = False
        job_key = event.ref.get("job_key")
        if job_key:
            row = self.alerts.job_by_key(job_key)
            job_id = row["id"] if row else None
            job = obs.jobs.get(job_key)
            has_deliverables = bool(job and job.has_deliverables)
        rows = keyboard_rows(alert_id, job_id=job_id, has_deliverables=has_deliverables)
        text = alert_text(event, now, self.alert_config)
        self._notify(f"alert-{alert_id}", text, reply_markup=inline_keyboard(rows))
        result["text"] = text
        result["buttons"] = [data for row in rows for _, data in row]
        return result

    def _flush_held_alerts(self, now: datetime) -> dict[str, Any] | None:
        """조용히(/quiet)·시간당 상한으로 보류된 알림을, 다시 보낼 수 있게 되면 한 건으로 묶어 보낸다.
        조용한 시간·알림 끔으로 보류된 것은 아침 요약으로 간다."""
        prefs = self.alerts.prefs(self.alert_config.enabled)
        if not prefs.enabled or prefs.muted(now) or in_quiet_hours(now, self.alert_config):
            return None
        if self.alerts.sent_since(now - timedelta(hours=1)) >= self.alert_config.hourly_cap:
            return None
        reasons = ("muted", "cap") if self.alert_config.brief_enabled else ("muted", "cap", "quiet", "off")
        held = self.alerts.list_alerts(statuses=("QUEUED",), reasons=reasons, limit=100)
        if not held:
            return None
        held.reverse()
        lines = [f"🔔 <b>보류됐던 알림 {len(held)}건</b>"]
        lines += [self._alert_line(item) for item in held[:8]]
        if len(held) > 8:
            lines.append(f"… 외 {len(held) - 8}건")
        event = AlertEvent(key=f"digest:{now.isoformat()}", severity="info", kind="digest", title="보류 알림 묶음")
        digest_id = self.alerts.record(event, status="SENT", reason="digest", now=now)
        self.alerts.mark([item["id"] for item in held], "DIGESTED")
        self._notify(f"alert-{digest_id}", "\n".join(lines))
        return {"id": digest_id, "key": event.key, "severity": "info", "kind": "digest", "status": "SENT",
                "reason": "digest", "count": len(held)}

    def _alert_line(self, item: dict[str, Any]) -> str:
        stamp = self._local(item["created_at"]).strftime("%m-%d %H:%M")
        body = f" · {esc(item['body'][:80])}" if item.get("body") else ""
        return f"• {SEVERITY_ICONS.get(item['severity'], 'ℹ️')} {esc(stamp)} {esc(item['title'])}{body}"

    def _local(self, value: str | datetime) -> datetime:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(self.alert_config.tz)

    def brief_tick(self, now: datetime | None = None) -> bool:
        """아침 요약을 날짜당 한 번 보낸다. 보냈으면 True."""
        now = now or self.clock()
        last = (self.alerts.get_state("brief") or {}).get("last_date")
        date = brief_due(now, self.alert_config, last)
        if date is None:
            return False
        text = self.brief_text(now)
        self.alerts.set_state("brief", {"last_date": date}, now)
        queued = self.alerts.list_alerts(statuses=("QUEUED",), limit=500)
        self.alerts.mark([item["id"] for item in queued], "BRIEFED")
        self._notify(f"brief-{date}", text)
        return True

    def brief_text(self, now: datetime) -> str:
        """템플릿 요약(LLM 없음): 지난 24시간 알림, 장시간 작업, 봇 작업, 서버."""
        local = now.astimezone(self.alert_config.tz)
        since = now - timedelta(hours=24)
        weekday = "월화수목금토일"[local.weekday()]
        lines = [f"🌅 <b>아침 요약</b> · {local:%m/%d}({weekday}) {local:%H:%M}"]

        recent = [
            item
            for item in self.alerts.list_alerts(statuses=("SENT", "QUEUED", "DIGESTED"), since=since, limit=60)
            if item["kind"] != "digest"
        ]
        held = [item for item in recent if item["status"] == "QUEUED"]
        lines.append(f"\n<b>지난 24시간 알림</b> {len(recent)}건 (보류됐던 것 {len(held)}건)")
        shown = sorted(recent, key=lambda item: ({"critical": 0, "warn": 1}.get(item["severity"], 2), -item["id"]))
        lines += [self._alert_line(item) + (" ⏸" if item["status"] == "QUEUED" else "") for item in shown[:8]]
        if not recent:
            lines.append("• 없음")
        elif len(recent) > 8:
            lines.append(f"… 외 {len(recent) - 8}건")

        observed = self.alerts.get_state("observed") or {}
        job_states = observed.get("jobs") or {}
        counts: dict[str, int] = {}
        for value in job_states.values():
            counts[value.get("status")] = counts.get(value.get("status"), 0) + 1
        lines.append("\n<b>장시간 작업</b>")
        if counts:
            lines.append(
                "• "
                + " · ".join(
                    f"{STATUS_LABELS.get(name, name)} {count}"
                    for name, count in sorted(counts.items(), key=lambda pair: str(pair[0]))
                )
            )
            missing = sum(1 for value in job_states.values() if value.get("postprocess_missing"))
            if missing:
                lines.append(f"• ⚠️ 후처리 미완 {missing}건")
        else:
            lines.append("• 관측된 작업 없음")

        lines.append("\n<b>봇 작업</b>")
        active = self.store.list_tasks_in_states(ACTIVE_STATES)
        changed = [task for task in self.store.list_tasks(10) if (task.get("updated_at") or "") >= since.isoformat(timespec="seconds")]
        if active:
            lines += [f"• 진행 중 <code>{esc(task['id'])}</code> {esc(task['state'])}" for task in active[:3]]
        lines += [
            f"• <code>{esc(task['id'])}</code> → {esc(task['state'])}"
            for task in changed[:5]
            if task["id"] not in {item["id"] for item in active}
        ]
        if not active and not changed:
            lines.append("• 변화 없음")

        host = observed.get("host") or {}
        lines.append("\n<b>서버</b> (마지막 감시 기준)")
        if host:
            containers = host.get("containers")
            container_text = (
                ", ".join(f"{name} {'실행' if state == 'running' else state}" for name, state in containers.items())
                if isinstance(containers, dict)
                else "미확인"
            )
            tmux = {"active": "있음", "inactive": "없음"}.get(str(host.get("tmux")), "미확인")
            lines.append(
                f"• 디스크 여유 {esc(bytes_human(host.get('disk_free')))} · 메모리 여유 {esc(bytes_human(host.get('mem_available')))}"
            )
            lines.append(f"• 컨테이너: {esc(container_text)}")
            lines.append(f"• Remote Control 세션: {tmux}")
        else:
            lines.append("• 아직 관측 없음")
        prefs = self.alerts.prefs(self.alert_config.enabled)
        if not prefs.enabled:
            lines.append("\n🔕 알림이 꺼져 있습니다(/alerts on).")
        return _fit("\n".join(lines))

    # ------------------------------------------------------------------ 명령: /jobs /files /logs /quiet /alerts

    def _current_jobs(self, now: datetime) -> list[JobObservation]:
        jobs = self.job_scanner(now)
        for job in jobs:
            self.alerts.upsert_job(job.key, job.path, job.label, job.status, now)
        return jobs

    def _job_status_label(self, job: JobObservation) -> str:
        label = STATUS_LABELS.get(job.status, job.status)
        if job.postprocess_missing:
            label += " · ⚠️ 후처리 미완"
        return label

    def _jobs_text(self) -> str:
        now = self.clock()
        jobs = self._current_jobs(now)[:10]
        if not jobs:
            return "감시 중인 장시간 작업이 없습니다(config/jobs.toml 패턴 + STARTED 마커)."
        lines = ["<b>장시간 작업</b> (마커 기준, 최근 활동 순)"]
        for job in jobs:
            stamp = datetime.fromtimestamp(job.last_activity, UTC).astimezone(self.alert_config.tz)
            extra = " · 결과물 있음" if job.has_deliverables else ""
            lines.append(
                f"{STATUS_ICONS.get(job.status, '•')} {esc(job.label)}\n"
                f"   {esc(self._job_status_label(job))} · {stamp:%m-%d %H:%M}{extra}"
            )
        lines.append("\n결과물: <code>/files 이름</code> · 로그: <code>/logs 이름</code>")
        return _fit("\n".join(lines))

    def _resolve_job(self, hint: str, *, for_files: bool = False) -> tuple[JobObservation | None, int | None]:
        now = self.clock()
        jobs = self._current_jobs(now)
        job = match_job(jobs, hint, prefer_deliverables=for_files)
        if job is None:
            return None, None
        row = self.alerts.job_by_key(job.key)
        return job, (row["id"] if row else None)

    def _files_command(self, hint: str) -> None:
        if not hint:
            jobs = [job for job in self._current_jobs(self.clock()) if job.has_deliverables][:6]
            if not jobs:
                self._send("보낼 결과물이 있는 작업이 없습니다. /jobs 로 작업 상태를 볼 수 있습니다.")
                return
            if len(jobs) == 1:
                row = self.alerts.job_by_key(jobs[0].key)
                if row:
                    self._send_deliverable_list(row["id"])
                return
            buttons = []
            for job in jobs:
                row = self.alerts.job_by_key(job.key)
                if row:
                    buttons.append([(_button_label(job.label), f"jf:{row['id']}")])
            self._send("결과물을 받을 작업을 고르세요.", reply_markup=inline_keyboard(buttons))
            return
        job, job_id = self._resolve_job(hint, for_files=True)
        if job is None or job_id is None:
            self._send(f"작업을 찾지 못했습니다: <code>{esc(hint)}</code>\n/jobs 에서 이름을 확인해 주세요.")
            return
        self._send_deliverable_list(job_id)

    def _send_deliverable_list(self, job_id: int) -> None:
        row = self.alerts.get_job(job_id)
        if row is None:
            self._send("작업 기록을 찾지 못했습니다. /jobs 로 다시 확인해 주세요.")
            return
        job_dir, reason = resolve_allowed(row["path"], self.file_roots)
        if job_dir is None or not job_dir.is_dir():
            self._send(f"이 작업 폴더는 보낼 수 없습니다: {esc(reason or '폴더 아님')}")
            return
        checks = list_deliverables(
            job_dir, self.jobs_config.deliverable_dirs, self.file_roots, self.jobs_config.max_deliverables
        )
        if not checks:
            self._send(f"<b>{esc(row['label'])}</b>\n보낼 수 있는 결과물이 없습니다.")
            return
        now = self.clock()
        buttons = []
        oversize = []
        icons = {"video": "🎬", "photo": "🖼", "document": "📄", "text": "📝"}
        for check in checks:
            assert check.path is not None
            if check.too_large:
                oversize.append(
                    f"• {esc(check.path.name)} ({esc(bytes_human(check.size))}) — 50MB 초과, 경로만: "
                    f"<code>{esc(home_relative(check.path, self.paths.home))}</code>"
                )
                continue
            deliverable_id = self.alerts.upsert_deliverable(job_id, str(check.path), check.size, now)
            label = f"{icons.get(check.kind, '📄')} {redact_text(check.path.name, 300)} · {bytes_human(check.size)}"
            buttons.append([(_button_label(label), f"f:{deliverable_id}")])
        text = f"<b>{esc(row['label'])}</b> 결과물 — 받을 파일을 누르세요."
        if oversize:
            text += "\n\n" + "\n".join(oversize)
        self._send(_fit(text), reply_markup=inline_keyboard(buttons) if buttons else None)

    def _deliver_file(self, deliverable_id: int) -> None:
        row = self.alerts.get_deliverable(deliverable_id)
        if row is None:
            self._send("파일 기록을 찾지 못했습니다. /files 로 목록을 다시 띄워 주세요.")
            return
        check = check_deliverable(row["path"], self.file_roots)
        shown = home_relative(Path(row["path"]), self.paths.home)
        if check.too_large and check.path is not None:
            self._send(
                f"파일이 커서 보낼 수 없습니다({esc(bytes_human(check.size))}, 한도 50MB).\n"
                f"경로: <code>{esc(home_relative(check.path, self.paths.home))}</code>"
            )
            return
        if not check.ok or check.path is None:
            self._send(f"보내지 않았습니다: {esc(check.reason)}")
            self.store.add_event(None, None, "FILE_REFUSED", "files", {"path": shown, "reason": check.reason})
            return
        path = check.path
        name = redact_text(path.name, 300)  # 이름 규칙이 놓친 키 모양 글자도 라벨·캡션·파일명에 내보내지 않는다
        caption = redact_html(f"{esc(name)} · {esc(bytes_human(check.size))}", 1_024)
        try:
            # 검사한 바로 그 파일(st_dev/st_ino)만 O_NOFOLLOW 로 열어 그 fd 에서만 읽는다(검사~읽기 사이 바꿔치기 방지).
            with open_checked(check, self.file_roots) as handle:
                if check.kind == "text":
                    text, truncated = read_redacted_text(path, handle=handle)
                    content = b""
                else:
                    text, truncated = "", False
                    content = handle.read(check.size + 1)
                    if len(content) != check.size:
                        raise OSError("file changed while reading")
            if check.kind == "text":
                if len(text) <= TEXT_INLINE_CHARS and not truncated:
                    self._send(f"📝 <b>{esc(name)}</b> (마스킹됨)\n<pre>{esc(text) or '(빈 파일)'}</pre>")
                else:
                    self.telegram.send_file(
                        self.settings.allowed_chat_id,
                        "document",
                        name,
                        text.encode("utf-8"),
                        caption=caption + " · 마스킹됨" + (" · 앞부분만" if truncated else ""),
                    )
            else:
                try:
                    self.telegram.send_file(
                        self.settings.allowed_chat_id, check.kind, name, content, caption=caption
                    )
                except TelegramError:
                    if check.kind != "photo":
                        raise
                    # 사진 규격 제한에 걸리면 원본 문서로 한 번 더 보낸다.
                    self.telegram.send_file(
                        self.settings.allowed_chat_id, "document", name, content, caption=caption
                    )
        except (TelegramError, OSError) as exc:
            LOG.warning("File delivery failed: %s", redact_text(str(exc), 300))
            self._send(f"⚠️ 파일 전송 실패: {esc(name)} · {esc(type(exc).__name__)}")
            return
        self.store.add_event(
            None, None, "FILE_SENT", "files", {"path": shown, "size": check.size, "kind": check.kind}
        )

    def _logs_command(self, hint: str) -> None:
        job, job_id = self._resolve_job(hint)
        if job is None or job_id is None:
            target = f": <code>{esc(hint)}</code>" if hint else ""
            self._send(f"로그를 볼 작업을 찾지 못했습니다{target}\n/jobs 에서 이름을 확인해 주세요.")
            return
        self._send_logs(job_id)

    def _send_logs(self, job_id: int) -> None:
        row = self.alerts.get_job(job_id)
        if row is None:
            self._send("작업 기록을 찾지 못했습니다. /jobs 로 다시 확인해 주세요.")
            return
        job_dir, reason = resolve_allowed(row["path"], self.file_roots)
        if job_dir is None or not job_dir.is_dir():
            self._send(f"이 작업 로그는 읽을 수 없습니다: {esc(reason or '폴더 아님')}")
            return
        logs, refused = self._job_logs(job_dir)
        for name, why in refused:
            self._send(f"{esc(name)}: 읽지 않았습니다({esc(why)})")
        if not logs:
            if not refused:
                self._send(f"<b>{esc(row['label'])}</b>\nrun.log·postprocess.log 가 없습니다.")
            return
        for log in logs:
            safe = log
            lines = clean_log_lines(tail_text(safe), 30)
            body = "\n".join(lines).replace(str(self.paths.home), "~") or "(내용 없음)"
            self._send(
                _fit(f"📜 <b>{esc(log.name)}</b> · {esc(row['label'])} (끝 30줄, 마스킹됨)\n<pre>{esc(body)}</pre>")
            )

    def _job_logs(self, job_dir: Path) -> tuple[list[Path], list[tuple[str, str]]]:
        """run.log 와 가장 최근 postprocess*.log. 심볼릭 링크·일반 파일 아님·허용 루트 밖은 건너뛴다.

        (보여 줄 로그, [(이름, 거부 사유)]) — 끊어진 링크 하나 때문에 /logs 전체가 실패하지 않게 한다.
        """
        run_log: Path | None = None
        post_logs: list[tuple[float, Path]] = []
        refused: list[tuple[str, str]] = []
        try:
            candidates = sorted(job_dir.glob("*.log"))
        except OSError:
            candidates = []
        for item in candidates:
            if item.name != "run.log" and not item.name.startswith("postprocess"):
                continue
            try:
                info = item.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                refused.append((item.name, "심볼릭 링크·일반 파일이 아닌 로그는 읽지 않습니다"))
                continue
            safe, why = resolve_allowed(item, self.file_roots)
            if safe is None:
                refused.append((item.name, why))
                continue
            if item.name == "run.log":
                run_log = safe
            else:
                post_logs.append((info.st_mtime, safe))
        post_logs.sort(key=lambda pair: pair[0], reverse=True)
        logs = ([run_log] if run_log else []) + [path for _, path in post_logs[:1]]
        return logs, refused

    def _quiet_command(self, argument: str) -> None:
        now = self.clock()
        prefs = self.alerts.prefs(self.alert_config.enabled)
        arg = (argument or "").strip().lower()
        if arg in {"off", "해제", "끝", "0"}:
            self.alerts.set_prefs(AlertPrefs(enabled=prefs.enabled, muted_until=None), now)
            self._send(self._prefs_result_text(now))
            return
        if not arg:
            self._send(
                "얼마나 조용히 할지 적어 주세요.\n예: <code>/quiet 2h</code>, <code>/quiet 30m</code> (최대 12시간), "
                "해제는 <code>/quiet off</code>"
            )
            return
        seconds = parse_duration(arg)
        if seconds is None:
            self._send("기간을 이해하지 못했습니다. 예: <code>/quiet 2h</code>, <code>/quiet 30m</code>")
            return
        if seconds > MAX_QUIET_SECONDS:
            self._send("조용히는 최대 12시간까지입니다. 예: <code>/quiet 12h</code>")
            return
        self._mute(seconds, now)

    def _mute(self, seconds: int, now: datetime) -> None:
        prefs = self.alerts.prefs(self.alert_config.enabled)
        until = now + timedelta(seconds=seconds)
        self.alerts.set_prefs(AlertPrefs(enabled=prefs.enabled, muted_until=until), now)
        saved = self.alerts.prefs(self.alert_config.enabled)  # 저장된 값을 다시 읽어 그대로 알린다
        if not saved.muted(now):
            self._send(self._prefs_result_text(now))
            return
        self._send(
            f"🔕 {format_duration(seconds)} 조용히 합니다({saved.muted_until.astimezone(self.alert_config.tz):%H:%M} 까지).\n"
            "치명적인 알림(컨테이너 멈춤·디스크·메모리 부족)은 그대로 보냅니다."
        )

    def _prefs_result_text(self, now: datetime) -> str:
        """알림 설정을 바꾼 뒤 DB 에서 다시 읽은 실제 상태로 답한다(모델·명령 문구가 아니라 저장된 값 기준)."""
        prefs = self.alerts.prefs(self.alert_config.enabled)
        head = "🔔 먼저 알리기: 켜짐" if prefs.enabled else "🔕 먼저 알리기: 꺼짐(치명적인 알림만 즉시, 나머지는 아침 요약)"
        if prefs.muted(now):
            quiet = f"조용히: {prefs.muted_until.astimezone(self.alert_config.tz):%m-%d %H:%M} 까지(치명적인 알림은 그대로)"
        else:
            quiet = "조용히: 꺼짐"
        return f"{head}\n{quiet}"

    def _alerts_command(self, argument: str) -> None:
        now = self.clock()
        arg = (argument or "").strip().lower()
        prefs = self.alerts.prefs(self.alert_config.enabled)
        if arg in {"on", "켜", "켜기"}:
            # 켜기는 '다시 알림을 받겠다'는 뜻이라 조용히 모드도 함께 푼다.
            self.alerts.set_prefs(AlertPrefs(enabled=True, muted_until=None), now)
            self._send(self._prefs_result_text(now))
            return
        if arg in {"off", "꺼", "끄기"}:
            self.alerts.set_prefs(AlertPrefs(enabled=False, muted_until=prefs.muted_until), now)
            self._send(self._prefs_result_text(now))
            return
        if arg and arg not in {"status", "상태"}:
            self._send(
                "사용법: <code>/alerts</code> 설정 보기 · <code>/alerts on</code>(켜기 + 조용히 해제) · "
                "<code>/alerts off</code>(치명적인 것만)"
            )
            return
        cfg = self.alert_config
        muted = (
            f"{prefs.muted_until.astimezone(cfg.tz):%m-%d %H:%M} 까지 조용히" if prefs.muted(now) else "아님"
        )
        recent = self.alerts.list_alerts(statuses=("SENT", "QUEUED"), limit=5)
        held = len(self.alerts.list_alerts(statuses=("QUEUED",), limit=500))
        lines = [
            "<b>알림 설정</b>",
            f"먼저 알리기: {'켜짐' if prefs.enabled else '꺼짐(치명적인 것만)'}",
            f"조용히: {esc(muted)}",
            f"조용한 시간: {esc(cfg.quiet_start)}–{esc(cfg.quiet_end)} ({esc(cfg.timezone)}, 치명적인 것만 즉시)",
            f"아침 요약: {esc(cfg.brief_time) if cfg.brief_enabled else '꺼짐'} · 시간당 상한 {cfg.hourly_cap}건",
            f"보류 중: {held}건",
            "\n<b>최근 알림</b>",
        ]
        lines += [self._alert_line(item) for item in recent] or ["• 없음"]
        self._send(_fit("\n".join(lines)))

    # ------------------------------------------------------------------ 알림 버튼

    def _watch_callback(self, data: str) -> None:
        """al:<알림> jf:<작업> jl:<작업> f:<결과물> q:<분>. 행 번호만 받고 경로는 받지 않는다."""
        match = CALLBACK_RE.fullmatch(data)
        if not match:
            self._send("알 수 없거나 만료된 버튼입니다.")
            return
        kind, number = match.group(1), int(match.group(2))
        if kind == "al":
            self._send(self._alert_detail_text(number))
        elif kind == "jf":
            self._send_deliverable_list(number)
        elif kind == "jl":
            self._send_logs(number)
        elif kind == "f":
            self._deliver_file(number)
        elif kind == "q":
            if not 1 <= number <= MAX_QUIET_SECONDS // 60:
                self._send("조용히는 최대 12시간까지입니다.")
                return
            self._mute(number * 60, self.clock())

    def _alert_detail_text(self, alert_id: int) -> str:
        alert = self.alerts.get_alert(alert_id)
        if alert is None:
            return "알림 기록을 찾지 못했습니다."
        ref = alert.get("ref") or {}
        if ref.get("task_id"):
            return self._task_text(str(ref["task_id"]))
        stamp = self._local(alert["created_at"]).strftime("%m-%d %H:%M")
        lines = [
            f"{SEVERITY_ICONS.get(alert['severity'], 'ℹ️')} <b>{esc(alert['title'])}</b>",
            f"관측: {esc(stamp)} · 등급 {esc(alert['severity'])}",
        ]
        if alert.get("body"):
            lines.append(esc(alert["body"]))
        job_key = ref.get("job_key")
        if job_key:
            row = self.alerts.job_by_key(str(job_key))
            current = next((job for job in self.job_scanner(self.clock()) if job.key == job_key), None)
            if current is not None:
                last = datetime.fromtimestamp(current.last_activity, UTC).astimezone(self.alert_config.tz)
                lines += [
                    f"\n현재 상태: {esc(self._job_status_label(current))}",
                    f"마커: {esc(', '.join(current.markers))}",
                    f"마지막 활동: {last:%m-%d %H:%M}",
                    f"결과물: {'있음' if current.has_deliverables else '없음'}",
                ]
            elif row:
                lines.append(f"\n마지막 기록 상태: {esc(STATUS_LABELS.get(row['status'], row['status']))}")
            if row:
                lines.append(f"경로: <code>{esc(home_relative(Path(row['path']), self.paths.home))}</code>")
            lines.append("\n<b>미확인</b>\n• 마커·로그 시각만 본 추정입니다(프로세스 생존은 확인하지 않음)")
        return _fit("\n".join(lines))

    def _send(self, text: str, *, reply_markup: dict[str, Any] | None = None) -> dict[str, Any] | None:
        text = redact_html(text, 4_096)
        try:
            return self.telegram.send_message(
                self.settings.allowed_chat_id,
                text,
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            LOG.warning("Telegram send failed: %s", redact_text(str(exc), 500))
            self.store.queue_outbox(
                dedupe_key=f"send-{time.time_ns()}",
                chat_id=self.settings.allowed_chat_id,
                method="sendMessage",
                payload={
                    "chat_id": self.settings.allowed_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": reply_markup,
                    "disable_web_page_preview": True,
                },
            )
            return None

    def _update_task_card(
        self,
        task_id: str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        text = redact_html(text, 4_096)
        task = self.store.get_task(task_id)
        message_id = task.get("telegram_message_id")
        response: dict[str, Any] | None = None
        if message_id:
            try:
                response = self.telegram.edit_message_text(
                    self.settings.allowed_chat_id,
                    int(message_id),
                    text,
                    reply_markup=reply_markup,
                )
            except TelegramError as exc:
                if "message is not modified" in str(exc).lower():
                    response = {"message_id": message_id}
                else:
                    LOG.warning("Task card edit failed: %s", redact_text(str(exc), 500))
        if not message_id or (message_id and response is None):
            response = self._send(text, reply_markup=reply_markup)
            if response and response.get("message_id"):
                self.store.set_task_fields(task_id, telegram_message_id=response["message_id"])
        self.last_card_update[task_id] = time.monotonic()

    def _notify(self, dedupe_key: str, text: str, *, reply_markup: dict[str, Any] | None = None) -> None:
        text = redact_html(text, 4_096)
        payload: dict[str, Any] = {"chat_id": self.settings.allowed_chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
            payload["disable_web_page_preview"] = True
        self.store.queue_outbox(
            dedupe_key=dedupe_key,
            chat_id=self.settings.allowed_chat_id,
            method="sendMessage",
            payload=payload,
        )
        self._flush_outbox()

    def _flush_outbox(self) -> None:
        for item in self.store.pending_outbox():
            try:
                self.telegram.call_with_retry(item["method"], item["payload"], attempts=1)
            except TelegramError as exc:
                state = self.store.update_outbox(
                    item["id"],
                    success=False,
                    retry_after_seconds=min(exc.retry_after or 10, 300),
                    permanent=exc.permanent,
                )
                if state == "FAILED":
                    LOG.error(
                        "Outbox item %s dropped after %s attempts: %s",
                        item["id"],
                        int(item.get("attempts") or 0) + 1,
                        redact_text(str(exc), 300),
                    )
            else:
                self.store.update_outbox(item["id"], success=True)


# 승인 없이 바로 처리되는 대화 동작과, 모델이 승인이 필요한 것처럼 말했을 때 대신 쓸 문장
IMMEDIATE_INTENTS = {"alerts", "quiet", "files", "todo_add", "visual_request"}
IMMEDIATE_REPLIES = {
    "todo_add": "할 일 목록에 바로 남깁니다. 결과는 아래에 알려 드릴게요.",
    "visual_request": "Codex 에게 시안 요청을 바로 넘깁니다. 추적 ID 는 아래에 알려 드릴게요.",
    "alerts": "알림 설정을 바로 적용합니다. 실제 저장된 설정은 아래와 같습니다.",
    "quiet": "알림 설정을 바로 적용합니다. 실제 저장된 설정은 아래와 같습니다.",
    "files": "결과물 목록을 띄웁니다. 받을 파일 버튼을 누르면 그 파일이 갑니다.",
}
APPROVAL_CLAIM_RE = re.compile(r"승인|카드")
PENDING_ATTACHMENT_WINDOW = timedelta(minutes=30)
CALLBACK_RE = re.compile(r"(al|jf|jl|f|q):(\d{1,9})")


# 처리 구분(SYSTEM_SPEC.md): A 직접 답함 · B 직접 실행(승인) · C 넘기고 결과를 받아 옴 · D 대표가 직접 · E 거절.
HANDLING_BY_INTENT = {
    **{intent: "A" for intent in (
        "help", "usage", "status", "list_tasks", "task_detail", "jobs", "files", "logs", "hub", "git", "work",
        "model", "codex", "brief", "quiet", "alerts", "todo_list",
    )},
    **{intent: "B" for intent in ("create_task", "retry", "stop_task", "merge", "push", "deploy")},
    **{intent: "C" for intent in ("todo_add", "todo_done", "visual_request", "visual_suggest")},
    "ask_project": "D",
}


def handling_class(decision: Decision | None, model_handling: str | None = None) -> str:
    """실제로 받아들인 결정으로 처리 구분을 정한다. 동작이 없으면 모델이 밝힌 A·D·E 를 쓰고, 모르면 A."""
    if decision is not None and decision.intent in HANDLING_BY_INTENT:
        return HANDLING_BY_INTENT[decision.intent]
    return model_handling if model_handling in {"A", "D", "E"} else "A"


def _button_label(text: str, limit: int = 48) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fit(text: str, limit: int = 4_000) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # 잘린 HTML 태그가 남지 않게 마지막 줄을 버리고, 열린 <pre> 를 닫는다.
    cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
    if cut.count("<pre>") > cut.count("</pre>"):
        cut += "</pre>"
    return cut + "\n…(생략)"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.load()
    bot = ChiefBot(settings)
    signal.signal(signal.SIGTERM, bot.stop)
    signal.signal(signal.SIGINT, bot.stop)
    bot.run()


if __name__ == "__main__":
    main()

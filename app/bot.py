from __future__ import annotations

import html
import json
import logging
import signal
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from adapters.gemini_rest import GeminiInterpreter
from adapters.telegram import TelegramAPI, TelegramError, inline_keyboard
from app.collector import bytes_human, collect_host_status
from app.config import Settings
from app.policy import Decision, Policy
from app.release import ReleaseError, ReleaseManager
from app.runner import RunnerError, TaskRunner
from app.redact import redact_text
from app.state import ConflictError, StateStore
from app.verify import VerificationError, Verifier


LOG = logging.getLogger("deskmini-chief")
ACTIVE_STATES = (
    "QUEUED",
    "STARTING",
    "RUNNING",
    "VERIFYING",
    "STOP_REQUESTED",
    "STOPPING",
)


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


class ChiefBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = StateStore(settings.db_path, settings.root / "schema.sql")
        self.telegram = TelegramAPI(
            settings.telegram_token,
            request_timeout=settings.telegram_timeout_seconds + 15,
        )
        self.gemini = GeminiInterpreter(settings.gemini_script)
        self.policy = Policy(self.gemini, list(settings.projects))
        self.runner = TaskRunner(settings, self.store)
        self.verifier = Verifier(settings, self.store, self.gemini)
        self.release = ReleaseManager(settings, self.store)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="chief")
        self.verification_futures: dict[str, Future[dict[str, Any]]] = {}
        self.release_futures: dict[str, Future[dict[str, Any]]] = {}
        self.last_card_update: dict[str, float] = {}
        self.stopping = False

    def run(self) -> None:
        me = self.telegram.get_me()
        LOG.info("Telegram connection verified for bot id=%s", me.get("id"))
        self._recover_state()
        self._notify(
            "chief-started",
            "🏛️ <b>DeskMini 비서실장 시작</b>\n\n"
            "상태 조회와 작업 접수를 받을 준비가 됐습니다.\n"
            "명령: /status · /tasks · /new",
        )
        while not self.stopping:
            self._process_futures()
            self._reconcile_tasks()
            self._flush_outbox()
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
        for approval in self.store.recoverable_consumed_approvals():
            try:
                if approval["action"] == "start":
                    self.runner.start(approval["task_id"])
                else:
                    self.release_futures[approval["task_id"]] = self.executor.submit(
                        self.release.execute, approval
                    )
            except Exception as exc:
                LOG.error(
                    "Could not recover consumed approval %s: %s",
                    approval["id"],
                    type(exc).__name__,
                )
        for update in self.store.pending_updates():
            self._handle_update(update, replay=True)

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
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send("텍스트 메시지만 처리할 수 있습니다.")
            return
        decision = self.policy.decide(text)
        self._execute_decision(decision, text, update_id)

    def _execute_decision(
        self, decision: Decision, original_text: str, source_update_id: int | None = None
    ) -> None:
        if decision.intent == "help":
            self._send(self._help_text())
            return
        if decision.intent == "status":
            self._send(self._status_text())
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
            self.telegram.answer_callback(callback_id)
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
            if action == "start":
                task = self.store.get_task(approval["task_id"])
                if approval["payload"] != self._start_payload(task):
                    raise ConflictError("Approved start payload no longer matches task")
                started = self.runner.start(task["id"])
                self._update_task_card(
                    started["id"],
                    self._task_text(started["id"]),
                    reply_markup=self._running_keyboard(started["id"]),
                )
            elif action in {"merge", "push", "deploy"}:
                future = self.executor.submit(self.release.execute, approval)
                self.release_futures[approval["task_id"]] = future
                self._send(f"⏳ <b>{esc(action)}</b> 실행을 시작했습니다: <code>{esc(approval['task_id'])}</code>")
            return
        if data.startswith("stop:"):
            self._stop_task(data.split(":", 1)[1])
            return
        if data.startswith("cancel:"):
            task_id = data.split(":", 1)[1]
            self.store.transition(task_id, expected_state="AWAITING_START", new_state="CANCELLED")
            self._send(f"취소했습니다: <code>{esc(task_id)}</code>")

    def _reconcile_tasks(self) -> None:
        for task in self.store.list_tasks_in_states(ACTIVE_STATES):
            task_id = task["id"]
            if task["state"] in {"STARTING", "RUNNING", "STOP_REQUESTED", "STOPPING"}:
                previous = task["state"]
                current = self.runner.reconcile(task_id)
                if current != previous:
                    self._update_task_card(task_id, self._task_text(task_id))
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
            else:
                self._send(self._task_text(task_id))

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
        text += "• 병합 승인" if task["state"] == "READY" else "• 상태 카드의 가능한 동작 확인"
        return text

    def _task_confirmation_text(self, task: dict[str, Any]) -> str:
        spec = task["specification"]
        boundaries = "\n".join(f"• {esc(item)}" for item in spec.get("boundaries", [])) or "• 없음"
        acceptance = "\n".join(f"• {esc(item)}" for item in spec.get("acceptance", [])) or "• 등록된 검증 통과"
        return (
            "<b>작업 발주 확인</b>\n\n"
            f"ID: <code>{esc(task['id'])}</code>\n"
            f"프로젝트: <code>{esc(task['project_id'])}</code>\n"
            f"목표: {esc(spec.get('objective', ''))}\n"
            f"산출물: {esc(spec.get('output_format', ''))}\n\n"
            f"<b>범위 밖</b>\n{boundaries}\n\n"
            f"<b>완료 기준</b>\n{acceptance}"
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
            f"이전: <code>{esc(json.dumps(operation.get('before_evidence'), ensure_ascii=False)[:700])}</code>\n"
            f"이후: <code>{esc(json.dumps(operation.get('after_evidence'), ensure_ascii=False)[:900])}</code>\n\n"
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
            "/stop T-... — 작업 중단\n\n"
            "자연어 질문과 작업 지시도 받을 수 있습니다. 병합·Push·배포는 각각 별도 승인합니다."
        )

    def _send(self, text: str, *, reply_markup: dict[str, Any] | None = None) -> dict[str, Any] | None:
        text = redact_text(text, 4_096)
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

    def _notify(self, dedupe_key: str, text: str) -> None:
        text = redact_text(text, 4_096)
        self.store.queue_outbox(
            dedupe_key=dedupe_key,
            chat_id=self.settings.allowed_chat_id,
            method="sendMessage",
            payload={"chat_id": self.settings.allowed_chat_id, "text": text, "parse_mode": "HTML"},
        )
        self._flush_outbox()

    def _flush_outbox(self) -> None:
        for item in self.store.pending_outbox():
            try:
                self.telegram.call_with_retry(item["method"], item["payload"], attempts=1)
            except TelegramError as exc:
                self.store.update_outbox(
                    item["id"],
                    success=False,
                    retry_after_seconds=min(exc.retry_after or 10, 300),
                )
            else:
                self.store.update_outbox(item["id"], success=True)


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

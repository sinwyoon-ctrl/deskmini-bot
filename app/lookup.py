"""Bounded local evidence lookup. No caller-selected paths, SQL, commands or writes."""
from __future__ import annotations

import fnmatch
import json
import re
import time
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path

from app.files import check_deliverable, open_checked, resolve_allowed
from app.redact import redact, redact_text
from app.todo import TODO_ID_RE
from app.visual import SAFE_REL_RE

SOURCES = {"tasks", "orca", "hub", "history", "results"}
OUTPUT_LIMIT = 5_000


class ReadOnlyLookup:
    def __init__(self, store, paths, chat_id, file_roots, jobs_config):
        self.store, self.paths, self.chat_id = store, paths, chat_id
        self.file_roots, self.jobs_config = file_roots, jobs_config

    def __call__(self, request, *, deadline=None):
        deadline = min(deadline or float("inf"), time.monotonic() + 3)
        source = request.get("source") if isinstance(request, dict) else None
        query = request.get("query") if isinstance(request, dict) else None
        valid_source = isinstance(source, str) and source in SOURCES
        observation = {"source": source if valid_source else "invalid",
                       "observed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                       "status": "error", "scope": "bounded local records", "items": []}
        # Literal words only: never interpret a query as a path, SQL, glob or shell.
        if (not isinstance(request, dict) or set(request) != {"source", "query"}
                or not valid_source or not isinstance(query, str) or len(query) > 80
                or not re.fullmatch(r"[\w가-힣 .:·-]*", query) or ".." in query):
            observation["error"] = "invalid lookup request"
            return observation
        query = query.strip().casefold()
        observation["query"] = redact_text(query, 80)
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            items, scope = getattr(self, "_" + source)(query, deadline)
            observation["scope"] = scope
            observation["status"] = ("empty" if not items else "ambiguous"
                                     if source in {"tasks", "orca", "results"} and query and len(items) > 1 else "ok")
            # Redact complete values before bounding; always return valid JSON.
            for item in items[:12]:
                clean = redact(item)
                observation["items"].append(clean)
                if len(json.dumps(observation, ensure_ascii=False)) > OUTPUT_LIMIT - 100:
                    observation["items"].pop()
                    observation["truncated"] = True
                    break
            if len(items) > len(observation["items"]):
                observation["truncated"] = True
        except Exception as exc:
            observation.update(status="error", items=[], error=type(exc).__name__)
        return observation

    def _rows(self, sql, params, deadline):
        with self.store.connect() as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=100")
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            return [dict(row) for row in db.execute(sql, params)]

    @staticmethod
    def _matches(item, query):
        return not query or query in json.dumps(item, ensure_ascii=False).casefold()

    def _tasks(self, query, deadline):
        rows = self._rows("SELECT id,state,project_id,specification_json,updated_at FROM tasks "
                          "WHERE chat_id=? ORDER BY rowid DESC LIMIT 100", (self.chat_id,), deadline)
        for row in rows:
            spec = json.loads(row.pop("specification_json"))
            row["objective"] = redact_text(str(spec.get("objective", "")), 800)
        return [r for r in rows if self._matches(r, query)], "latest 100 bot tasks; stored state, not live process proof"

    def _orca(self, query, deadline):
        rows = self._rows("SELECT id,project,agent,model,state,request_text,summary,error,updated_at "
                          "FROM orca_requests WHERE chat_id=? OR chat_id IS NULL ORDER BY rowid DESC LIMIT 100",
                          (self.chat_id,), deadline)
        matches = [r for r in rows if self._matches(r, query)]
        for row in matches[:12]:
            for key in ("request_text", "summary", "error"):
                row[key] = redact_text(str(row[key] or ""), 1_200)
            row["questions"] = self._rows("SELECT body,state FROM orca_questions WHERE request_id=? LIMIT 3",
                                          (row["id"],), deadline)
            for question in row["questions"]:
                question["body"] = redact_text(str(question["body"]), 600)
        if not matches and query:
            matches = self._rows("SELECT project,body,observed_at FROM orca_project_context WHERE project=? LIMIT 1",
                                 (query,), deadline)
            for row in matches:
                row["body"] = redact_text(row["body"], 3_000)
        from app.orca_sessions import read_sessions
        live = read_sessions(self.store)
        sessions = [{"kind": "agent_session", "observed_at": live.get("observed_at"), **item}
                    for item in live["items"] if self._matches(item, query)]
        if live["status"] not in {"ok", "partial"}:
            sessions = [{"kind": "live_observation", "status": live["status"], "items": []}]
        return sessions + matches, "Orca live session observation (120s maximum age) plus latest 100 supervised records; agent turn completion is not project completion"

    def _history(self, query, deadline):
        rows = self.store.recent_chat_messages(self.chat_id, 200)
        items = [r for r in reversed(rows) if self._matches(r, query)]
        for row in items:
            row["text"] = redact_text(row["text"], 1_200)
        return items, "latest 200 messages, newest first; receipt:* is factual, model text is not execution evidence"

    def _safe_text(self, path, roots=None):
        roots = roots or (self.paths.agent_hub,)
        if path.is_symlink():
            return ""
        check = check_deliverable(path, roots, max_bytes=1_000_000)
        if not check.ok:
            return ""
        # Retain the stable outer boundary even if an event-linked directory is swapped later.
        with open_checked(check, (self.paths.agent_hub,), max_bytes=1_000_000) as handle:
            return redact_text(handle.read(32_768 + 4_096).decode("utf-8", errors="replace"), 32_768)

    def _hub(self, query, deadline):
        root = self.paths.agent_hub
        # Enumeration and reads are capped independently; no recursive caller-controlled traversal.
        months = sorted(islice((root / "log").glob("*"), 120), reverse=True)[:3]
        files = [root / "BOARD.md"]
        for month in months:
            files.extend(sorted(islice(month.glob("*.md"), 100), reverse=True)[:20])
        items = self._handoffs(query, deadline)
        for path in files[:61]:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            text = self._safe_text(path)
            if not text or (query and query not in text.casefold() and query not in path.name.casefold()):
                continue
            position = max(0, text.casefold().find(query) - 200) if query else 0
            items.append({"file": str(path.relative_to(root)), "text": text[position:position + 1_500],
                          "excerpt": True})
            if len(items) >= 12:
                break
        return items, "recent HANDOFF_CREATED/todo/visual records; BOARD first 32KiB and up to 60 logs from 3 recent month folders; safe excerpts"

    def _scoped_root(self, path):
        safe, _ = resolve_allowed(path, (self.paths.agent_hub,))
        # Do not trust a directory link as a new allowlisted root.
        return safe if safe == path and safe.is_dir() else None

    def _handoffs(self, query, deadline):
        """Resolve only event-linked, validated legacy IDs, never caller-supplied paths."""
        root = self.paths.agent_hub
        rows = self._rows("SELECT created_at,payload_json FROM events WHERE event_type='HANDOFF_CREATED' "
                          "ORDER BY id DESC LIMIT 100", (), deadline)
        # Prefer matching event identities/text, then inspect a small recent window for richer file text.
        rows.sort(key=lambda r: not self._matches(r, query))
        todo_root = self._scoped_root(root / "todo")
        done = todo_root / "done" if todo_root else None
        months = (sorted((p for p in islice(done.iterdir(), 120) if re.fullmatch(r"\d{4}-\d{2}", p.name)),
                         reverse=True)[:12] if done and done.is_dir() and not done.is_symlink() else [])
        items = []
        for row in rows[:20]:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            payload = json.loads(row["payload_json"])
            if not isinstance(payload, dict):
                continue
            item = {"kind": "handoff", "source": "HANDOFF_CREATED", "created_at": row["created_at"],
                    "to": redact_text(str(payload.get("to") or ""), 40),
                    "text": redact_text(str(payload.get("text") or ""), 400),
                    "todo_status": "unknown", "result_status": "no_result_record", "files": [],
                    "sendable": False, "delivery": "metadata_only; outside existing job/file send route"}
            todo_text = ""
            todo_id = str(payload.get("todo_id") or "")
            if todo_root and TODO_ID_RE.fullmatch(todo_id):
                item["todo_id"] = todo_id
                for folder in [todo_root, *months]:
                    if self._scoped_root(folder) is None:
                        continue
                    body = self._safe_text(folder / (todo_id + ".md"), (todo_root,))
                    if body:
                        todo_text = body
                        item.update(todo_status="open" if folder == todo_root else "done",
                                    todo_file=str((folder / (todo_id + ".md")).relative_to(root)),
                                    todo_excerpt=body[:600])
                        break
            relative = str(payload.get("path") or "")
            if not relative:
                candidates = [p for p in re.findall(r"visual/[a-z0-9_/-]+/request\.md", todo_text)
                              if SAFE_REL_RE.fullmatch(p)]
                relative = candidates[0] if len(set(candidates)) == 1 else ""
            if SAFE_REL_RE.fullmatch(relative):
                request = root / relative
                request_id = str(payload.get("request_id") or request.parent.name)
                folder = self._scoped_root(request.parent) if request_id == request.parent.name else None
                if folder:
                    item.update(request_id=request_id, request_file=relative)
                    request_text = self._safe_text(request, (folder,))
                    item["request_excerpt"] = request_text[:600]
                    card = self._safe_text(folder / "card.md", (folder,))
                    if card:
                        conclusion = re.search(r"(?m)^#+ 결론\s*\n+([^#\n][^\n]*)", card)
                        verdict = re.search(r"(?m)^verdict:\s*(.+)$", card)
                        item.update(result_status="card_available", card_file=str((folder / "card.md").relative_to(root)),
                                    conclusion=(conclusion.group(1) if conclusion else verdict.group(1) if verdict else card)[:600])
                    output = folder / "outputs"
                    if self._scoped_root(output) and not output.is_symlink():
                        for path in islice(output.iterdir(), 128):
                            if time.monotonic() >= deadline:
                                raise TimeoutError()
                            if path.is_symlink():
                                continue
                            check = check_deliverable(path, (output,))
                            if check.ok and check.size:
                                with open_checked(check, (root,)):
                                    pass  # Validate inode/links, never read media contents.
                                item["files"].append({"name": redact_text(check.path.name, 120), "kind": check.kind, "bytes": check.size})
                            if len(item["files"]) >= 8:
                                break
                    if item["files"]:
                        item["result_status"] = "artifacts_available"
            if self._matches(item, query):
                items.append(item)
            if len(items) >= 8:
                break
        return items

    def _results(self, query, deadline):
        rows = self._rows("SELECT id,path,label,status,updated_at FROM jobs ORDER BY updated_at DESC LIMIT 40", (), deadline)
        matches = [r for r in rows if self._matches({"label": r["label"], "name": Path(r["path"]).name}, query)]
        items = []
        for row in matches[:8]:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            directory, _ = resolve_allowed(row["path"], self.file_roots)
            if directory is None or not directory.is_dir():
                continue
            children = list(islice(directory.iterdir(), 128))
            candidates = list(children)
            folders = [p for p in children if any(fnmatch.fnmatch(p.name, pat) for pat in self.jobs_config.deliverable_dirs)][:8]
            for folder in folders:
                safe, _ = resolve_allowed(folder, self.file_roots)
                if safe and safe.is_dir():
                    candidates.extend(islice(safe.iterdir(), 128))
            files = []
            for candidate in candidates:
                check = check_deliverable(candidate, self.file_roots)
                if check.ok and check.size:
                    files.append({"name": check.path.name, "kind": check.kind, "bytes": check.size})
                if len(files) >= 12:
                    break
            items.append({"kind": "job", "sendable": True, "job": row["label"], "job_hint": directory.name, "status": row["status"],
                          "updated_at": row["updated_at"], "files": files})
        legacy = self._handoffs(query, deadline)
        return legacy + items, "legacy handoff/todo/visual metadata plus up to 8 of latest 40 known jobs; no media contents/web search; only kind=job is sendable"

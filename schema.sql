PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    owner_user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    request_text TEXT NOT NULL,
    specification_json TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    base_sha TEXT,
    head_sha TEXT,
    branch_name TEXT,
    worktree_path TEXT,
    telegram_message_id INTEGER,
    source_update_id INTEGER UNIQUE,
    policy_hash TEXT,
    budget_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    attempt INTEGER NOT NULL,
    unit_name TEXT,
    invocation_id TEXT,
    boot_id TEXT,
    session_id TEXT,
    requested_model TEXT,
    actual_model TEXT,
    state TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    heartbeat_at TEXT,
    exit_code INTEGER,
    result_json TEXT,
    usage_json TEXT,
    UNIQUE(task_id, attempt)
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    owner_user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    task_version INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL,
    -- 소비 뒤 실행 결과: EXECUTED | REFUSED | FAILED | ABANDONED. NULL = 소비만 되고 실행 시도 전(크래시 구간).
    outcome TEXT,
    outcome_at TEXT,
    outcome_note TEXT
);

CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    approval_id TEXT NOT NULL REFERENCES approvals(id),
    target_json TEXT NOT NULL,
    expected_before_json TEXT NOT NULL,
    desired_after_json TEXT NOT NULL,
    state TEXT NOT NULL,
    before_evidence_json TEXT,
    after_evidence_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT REFERENCES tasks(id),
    run_id TEXT REFERENCES runs(id),
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    evidence_hash TEXT
);

CREATE TABLE IF NOT EXISTS inbox (
    update_id INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS claude_permission_runtime (
    id INTEGER PRIMARY KEY CHECK(id=1), epoch TEXT NOT NULL, heartbeat REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_agent_targets (
    chat_id INTEGER NOT NULL, agent TEXT NOT NULL, target_id TEXT,
    source_order INTEGER NOT NULL, PRIMARY KEY(chat_id,agent)
);
CREATE TABLE IF NOT EXISTS telegram_agent_focus (
    chat_id INTEGER PRIMARY KEY, agent TEXT NOT NULL, source_order INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_agent_inputs (
    source TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, plan TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orca_chat_commands (
    id TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL, request_id TEXT NOT NULL,
    dispatch_id TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL,
    receipt TEXT, created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orca_request_conversations (
    request_id TEXT PRIMARY KEY, context_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claude_permission_sessions (
    request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, pid INTEGER NOT NULL,
    process_start TEXT NOT NULL, workspace TEXT NOT NULL, dispatch_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claude_permission_installs (
    workspace TEXT PRIMARY KEY, digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claude_permissions (
    id TEXT PRIMARY KEY, request_id TEXT NOT NULL, session_id TEXT NOT NULL,
    dispatch_id TEXT NOT NULL, epoch TEXT NOT NULL, pid INTEGER NOT NULL,
    process_start TEXT NOT NULL, hook_pid INTEGER NOT NULL, hook_start TEXT NOT NULL,
    digest TEXT NOT NULL, display TEXT NOT NULL, state TEXT NOT NULL,
    can_allow INTEGER NOT NULL, expires_at REAL NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    chat_id INTEGER NOT NULL,
    method TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text TEXT NOT NULL,
    provider TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_chat ON chat_messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_outbox_state ON outbox(state, next_attempt_at);

-- 감시기(먼저 알리기)·결과물 전송. 모두 추가 테이블이며 기존 테이블은 바꾸지 않는다.
CREATE TABLE IF NOT EXISTS watch_state (
    name TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('critical', 'warn', 'info')),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    ref_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliverables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    path TEXT NOT NULL UNIQUE,
    size INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts(dedupe_key, id);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, created_at);

-- 4라운드: 첨부(사진·문서)와 예약 재시도. 추가 테이블만 만든다.
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id INTEGER,
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    stored_path TEXT NOT NULL UNIQUE,
    mime TEXT,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    caption TEXT,
    task_id TEXT REFERENCES tasks(id),
    created_at TEXT NOT NULL,
    file_unique_id TEXT
);

CREATE TABLE IF NOT EXISTS scheduled_retries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    approval_id TEXT NOT NULL UNIQUE REFERENCES approvals(id),
    task_version INTEGER NOT NULL,
    run_at TEXT NOT NULL,
    state TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_attachments_task ON attachments(task_id);
CREATE INDEX IF NOT EXISTS idx_attachments_update ON attachments(update_id);
CREATE INDEX IF NOT EXISTS idx_scheduled_retries_state ON scheduled_retries(state, run_at);

-- Native Orca requests are never consumed by the legacy tasks/worker runner.
CREATE TABLE IF NOT EXISTS orca_requests (
 id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
 project TEXT NOT NULL, agent TEXT NOT NULL, request_text TEXT NOT NULL,
 chat_id INTEGER, state TEXT NOT NULL, run_id TEXT, task_id TEXT, dispatch_id TEXT,
 summary TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orca_questions (
 message_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES orca_requests(id),
 body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
 reply_text TEXT, reply_source TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS orca_intents (
 request_id TEXT PRIMARY KEY REFERENCES orca_requests(id),
 create_args TEXT NOT NULL, start_template TEXT NOT NULL,
 recover_requested INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orca_anomalies (
 message_id TEXT PRIMARY KEY, reason TEXT NOT NULL, observed_at TEXT NOT NULL
);

-- A manual switch is a journaled saga, separate from the original submit intent.
CREATE TABLE IF NOT EXISTS orca_changes (
 source_key TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES orca_requests(id),
 agent TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', instruction TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL, old_task TEXT, old_dispatch TEXT, new_task TEXT, new_dispatch TEXT,
 stop_args TEXT, create_args TEXT, start_args TEXT, context_json TEXT, error TEXT,
 created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS orca_one_change ON orca_changes(request_id) WHERE state != 'DONE';
CREATE TABLE IF NOT EXISTS orca_attempts (
 dispatch_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES orca_requests(id),
 task_id TEXT NOT NULL, agent TEXT NOT NULL, model TEXT NOT NULL, source_key TEXT NOT NULL,
 context_json TEXT, receipt_json TEXT NOT NULL, outcome TEXT, summary TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orca_project_context (
 project TEXT PRIMARY KEY, body TEXT NOT NULL, snapshot_json TEXT NOT NULL, observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orca_task_contracts (task_id TEXT PRIMARY KEY, context_path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orca_project_bindings (request_id TEXT PRIMARY KEY REFERENCES orca_requests(id), config_json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS codex_sessions (
 id TEXT PRIMARY KEY, project TEXT NOT NULL, cwd TEXT NOT NULL, chat_id INTEGER NOT NULL,
 thread_id TEXT UNIQUE, epoch TEXT, state TEXT NOT NULL, turn_id TEXT,
 last_text TEXT NOT NULL DEFAULT '', observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codex_focus (chat_id INTEGER PRIMARY KEY, session_id TEXT REFERENCES codex_sessions(id));
CREATE TABLE IF NOT EXISTS codex_commands (
 id TEXT PRIMARY KEY, source TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL REFERENCES codex_sessions(id),
 op TEXT NOT NULL, body TEXT NOT NULL, expected_turn TEXT, state TEXT NOT NULL,
 error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codex_questions (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES codex_sessions(id), epoch TEXT NOT NULL,
 request_id TEXT NOT NULL, turn_id TEXT NOT NULL, method TEXT NOT NULL, params TEXT NOT NULL,
 state TEXT NOT NULL, response TEXT, expires_at TEXT NOT NULL,
 UNIQUE(epoch, request_id)
);
CREATE TABLE IF NOT EXISTS codex_runtime (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch TEXT NOT NULL,
 state TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codex_activity (
 session_id TEXT PRIMARY KEY REFERENCES codex_sessions(id), epoch TEXT NOT NULL,
 turn_id TEXT, summary TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_model_sources (
 dedupe_key TEXT PRIMARY KEY, agent TEXT NOT NULL CHECK(agent IN ('codex','claude'))
);

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
    created_at TEXT NOT NULL
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

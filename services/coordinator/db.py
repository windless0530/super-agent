"""sa-coordinator 数据库层:连接、PRAGMA、建表与类型种子。

设计依据:落地方案 v1.2「任务登记表」一节。
- registry/tasks.db 只有 sa-coordinator 进程写(单写者原则)。
- WAL 提升并发读;外键约束开启。
- init_schema / seed_task_types 均幂等,支持 reconciler 重启续跑。
"""

import sqlite3

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    taskid            TEXT PRIMARY KEY,
    parent_task_id    TEXT,
    type              TEXT NOT NULL,
    title             TEXT NOT NULL,
    thread_a_msg_id   TEXT,
    thread_a_chat_id  TEXT,
    group_chat_id     TEXT,
    topic_root_msg_id TEXT,
    doc_links         TEXT DEFAULT '[]',
    status            TEXT NOT NULL,
    phase             TEXT,
    active_role       TEXT,
    version           INTEGER NOT NULL DEFAULT 0,
    rounds_by_phase   TEXT DEFAULT '{}',
    decisions         TEXT DEFAULT '[]',
    skill_versions    TEXT DEFAULT '{}',
    model             TEXT,
    repo_commit       TEXT,
    worktree          TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox_events (
    event_id    TEXT PRIMARY KEY,
    message_id  TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    taskid      TEXT,
    payload     TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    taskid          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_retry_at   TEXT,
    sent_msg_id     TEXT,
    needs_reconcile INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comment_inbox (
    reply_id     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    kind         TEXT NOT NULL,
    taskid       TEXT,
    doc_token    TEXT NOT NULL,
    payload      TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (reply_id, content_hash, kind)
);

CREATE TABLE IF NOT EXISTS bot_ownership (
    bot_app_id      TEXT PRIMARY KEY,
    owner_instance  TEXT NOT NULL,
    lease_token     TEXT,
    lease_expires_at TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_types (
    type_code     TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    group_chat_id TEXT,
    roles         TEXT NOT NULL,
    doc_template  TEXT,
    rounds_limit  INTEGER NOT NULL DEFAULT 3,
    needs_doc     INTEGER NOT NULL DEFAULT 1,
    can_dev       INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS taskid_seq (
    taskid_date TEXT PRIMARY KEY,
    last        INTEGER NOT NULL
);
"""

# 默认类型注册表;group_chat_id 待 P0 实测建群后回填
_DEFAULT_TASK_TYPES = [
    # (type_code, name, group_chat_id, roles, doc_template, rounds_limit, needs_doc, can_dev, enabled)
    ("RD", "研发调研", None, '["design","review"]', "方案模板", 3, 1, 1, 1),
    ("RS", "投研/行业报告", None, '["design","review"]', "研究报告模板", 3, 1, 0, 1),
    ("DEV", "开发实现", None, '["planner","executor","devreview"]', "Spec模板", 3, 1, 0, 1),
]


def connect(path: str) -> sqlite3.Connection:
    """打开连接并设置 WAL 与外键;row_factory 用 sqlite3.Row。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """幂等建表。"""
    conn.executescript(_DDL)
    conn.commit()


def seed_task_types(conn: sqlite3.Connection) -> None:
    """幂等写入默认类型注册表(RD/RS/DEV)。"""
    conn.executemany(
        "INSERT OR IGNORE INTO task_types"
        " (type_code, name, group_chat_id, roles, doc_template,"
        "  rounds_limit, needs_doc, can_dev, enabled)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        _DEFAULT_TASK_TYPES,
    )
    conn.commit()

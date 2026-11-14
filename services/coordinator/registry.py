"""任务登记表:taskid 分配、任务 CRUD 与 CAS 状态迁移。

依据:落地方案 v1.2「任务登记表:状态机」。
- taskid = yyyymmdd-NNN(000–999,当日重置),登记表保证唯一。
- CAS:tasks.version 即 expected_version 令牌,UPDATE ... WHERE version=?,成功 +1。
- doc_links / decisions / rounds_by_phase / skill_versions 在库中存 JSON 字符串,
  Task dataclass 上已是反序列化后的 Python 对象。
"""

import json
import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timezone

from .state import validate_transition

# 存 JSON 字符串的列
_JSON_FIELDS = {"doc_links", "decisions", "rounds_by_phase", "skill_versions"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UnknownTaskType(Exception):
    """类型未注册或已停用。"""


class TaskNotFound(Exception):
    """任务不存在。"""


class StaleVersion(Exception):
    """CAS 失败:期望版本与库中不一致。"""

    def __init__(self, taskid: str, expected_version: int):
        self.taskid = taskid
        self.expected_version = expected_version
        super().__init__(f"CAS 失败: {taskid} 期望 version={expected_version}")


@dataclass
class Task:
    taskid: str
    type: str
    title: str
    status: str
    version: int
    created_at: str
    updated_at: str
    parent_task_id: str | None = None
    thread_a_msg_id: str | None = None
    group_chat_id: str | None = None
    topic_root_msg_id: str | None = None
    doc_links: list = None  # type: ignore[assignment]
    phase: str | None = None
    active_role: str | None = None
    rounds_by_phase: dict = None  # type: ignore[assignment]
    decisions: list = None  # type: ignore[assignment]
    skill_versions: dict = None  # type: ignore[assignment]
    model: str | None = None
    repo_commit: str | None = None
    worktree: str | None = None

    def __post_init__(self):
        if self.doc_links is None:
            self.doc_links = []
        if self.decisions is None:
            self.decisions = []
        if self.rounds_by_phase is None:
            self.rounds_by_phase = {}
        if self.skill_versions is None:
            self.skill_versions = {}

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        kwargs = {}
        for f in fields(cls):
            val = row[f.name]
            if f.name in _JSON_FIELDS:
                val = json.loads(val) if val else ({} if f.name in ("rounds_by_phase", "skill_versions") else [])
            kwargs[f.name] = val
        return cls(**kwargs)


_TASK_FIELDS = {f.name for f in fields(Task)}


def allocate_taskid(conn: sqlite3.Connection, today: str) -> str:
    """分配当日序号 taskid(yyyymmdd-NNN),taskid_seq 保留行保证并发唯一。"""
    conn.execute(
        "INSERT OR IGNORE INTO taskid_seq (taskid_date, last) VALUES (?, 0)", (today,)
    )
    for _ in range(3):  # 极端写锁冲突时重试
        try:
            cur = conn.execute(
                "UPDATE taskid_seq SET last = last + 1 WHERE taskid_date = ?"
                " RETURNING last",
                (today,),
            )
            row = cur.fetchone()
            conn.commit()
            return f"{today}-{row[0]:03d}"
        except sqlite3.OperationalError:
            continue
    raise RuntimeError(f"taskid 分配失败: {today}")


def create_task(
    conn: sqlite3.Connection,
    *,
    type: str,
    title: str,
    message_id: str,
    event_id: str,
    thread_a_msg_id: str | None = None,
    parent_task_id: str | None = None,
    payload: dict | None = None,
) -> Task:
    """建任务(status=received,version=0),同事务幂等写 inbox_events(kind='task')。"""
    row = conn.execute(
        "SELECT 1 FROM task_types WHERE type_code=? AND enabled=1", (type,)
    ).fetchone()
    if row is None:
        raise UnknownTaskType(type)
    now = _now()
    today = now[:10].replace("-", "")
    # 当日序号(循环防并发冲突,taskid_seq 已保证唯一,此处仅防御)
    taskid = allocate_taskid(conn, today)
    conn.execute(
        "INSERT INTO tasks (taskid, parent_task_id, type, title, thread_a_msg_id,"
        " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (taskid, parent_task_id, type, title, thread_a_msg_id, "received", now, now),
    )
    conn.execute(
        "INSERT OR IGNORE INTO inbox_events"
        " (event_id, message_id, kind, taskid, payload, received_at)"
        " VALUES (?,?,?,?,?,?)",
        (event_id, message_id, "task", taskid, json.dumps(payload or {}, ensure_ascii=False), now),
    )
    conn.commit()
    return get_task(conn, taskid)


def get_task(conn: sqlite3.Connection, taskid: str) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE taskid=?", (taskid,)).fetchone()
    return Task.from_row(row) if row else None


def list_non_terminal(conn: sqlite3.Connection) -> list[Task]:
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status != 'done' ORDER BY taskid"
    ).fetchall()
    return [Task.from_row(r) for r in rows]


def cas_update(
    conn: sqlite3.Connection,
    taskid: str,
    expected_version: int,
    *,
    status: str | None = None,
    **fields,
) -> Task:
    """CAS 更新:version 匹配才落库并 +1;status 变更先过状态机校验。"""
    unknown = set(fields) - _TASK_FIELDS
    if unknown:
        raise TypeError(f"未知任务字段: {unknown}")
    current = conn.execute(
        "SELECT status, version FROM tasks WHERE taskid=?", (taskid,)
    ).fetchone()
    if current is None:
        raise TaskNotFound(taskid)
    # 陈旧版本优先报错:调用方视图过期时,先让其对账再谈迁移合法性
    if current["version"] != expected_version:
        raise StaleVersion(taskid, expected_version)
    if status is not None and status != current["status"]:
        validate_transition(current["status"], status)
    sets, params = [], []
    for name, val in fields.items():
        if name in _JSON_FIELDS:
            val = json.dumps(val, ensure_ascii=False)
        sets.append(f"{name}=?")
        params.append(val)
    if status is not None:
        sets.append("status=?")
        params.append(status)
    sets.append("version=version+1")
    sets.append("updated_at=?")
    params.extend([_now(), taskid, expected_version])
    cur = conn.execute(
        f"UPDATE tasks SET {', '.join(sets)} WHERE taskid=? AND version=?", params
    )
    if cur.rowcount == 0:
        raise StaleVersion(taskid, expected_version)
    conn.commit()
    task = get_task(conn, taskid)
    assert task is not None
    return task


def append_decision(
    conn: sqlite3.Connection, taskid: str, expected_version: int, decision: dict
) -> Task:
    """向 decisions 追加一条决策记录(CAS)。"""
    task = get_task(conn, taskid)
    if task is None:
        raise TaskNotFound(taskid)
    decisions = list(task.decisions) + [decision]
    return cas_update(conn, taskid, expected_version, decisions=decisions)

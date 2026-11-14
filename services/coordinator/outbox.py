"""outbox:持久化外发队列(可靠投递的唯一出口)。

依据:落地方案 v1.2 sa-coordinator 职责 2/3——
状态迁移与 outbox 入队同事务提交;sender 发送失败按行重试,
needs_reconcile 行在重发前必须先对账(防止重复话题/重复回投)。
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

OUTBOX_KINDS = {"dispatch", "relay", "decision", "notify", "reaction"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class OutboxRow:
    id: int
    taskid: str
    kind: str
    payload: dict
    status: str
    attempts: int
    next_retry_at: str | None
    sent_msg_id: str | None
    needs_reconcile: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "OutboxRow":
        return cls(
            id=row["id"],
            taskid=row["taskid"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            status=row["status"],
            attempts=row["attempts"],
            next_retry_at=row["next_retry_at"],
            sent_msg_id=row["sent_msg_id"],
            needs_reconcile=bool(row["needs_reconcile"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def enqueue(
    conn: sqlite3.Connection, *, taskid: str, kind: str, payload: dict,
    commit: bool = True,
) -> int:
    """入队一行待发消息;commit=False 时并入调用方事务。"""
    if kind not in OUTBOX_KINDS:
        raise ValueError(f"未知 outbox kind: {kind}")
    now = _now()
    cur = conn.execute(
        "INSERT INTO outbox (taskid, kind, payload, status, created_at, updated_at)"
        " VALUES (?,?,?,'pending',?,?)",
        (taskid, kind, json.dumps(payload, ensure_ascii=False), now, now),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def mark_sent(conn: sqlite3.Connection, outbox_id: int, sent_msg_id: str) -> None:
    conn.execute(
        "UPDATE outbox SET status='sent', sent_msg_id=?, needs_reconcile=0,"
        " updated_at=? WHERE id=?",
        (sent_msg_id, _now(), outbox_id),
    )
    conn.commit()


def mark_inflight(
    conn: sqlite3.Connection, outbox_id: int, *, now: str, next_retry_at: str
) -> bool:
    """发送前原子认领;崩溃留下 needs_reconcile=1,重启不得盲发。"""
    cur = conn.execute(
        "UPDATE outbox SET needs_reconcile=1, next_retry_at=?, updated_at=?"
        " WHERE id=? AND status='pending'"
        " AND (next_retry_at IS NULL OR next_retry_at<=?)",
        (next_retry_at, _now(), outbox_id, now),
    )
    conn.commit()
    return cur.rowcount == 1


def mark_failed_attempt(
    conn: sqlite3.Connection,
    outbox_id: int,
    next_retry_at: str,
    needs_reconcile: bool = False,
) -> None:
    """记一次失败:attempts+1,排下次重试;明确失败可清除发送前对账标记。"""
    conn.execute(
        "UPDATE outbox SET attempts=attempts+1, next_retry_at=?,"
        " needs_reconcile=?, updated_at=? WHERE id=?",
        (next_retry_at, 1 if needs_reconcile else 0, _now(), outbox_id),
    )
    conn.commit()


def defer(conn: sqlite3.Connection, outbox_id: int, next_retry_at: str) -> None:
    """推迟一行(attempts 不变):用于无 probe 时的安全滞留。"""
    conn.execute(
        "UPDATE outbox SET next_retry_at=?, updated_at=? WHERE id=?",
        (next_retry_at, _now(), outbox_id),
    )
    conn.commit()


def pending_due(conn: sqlite3.Connection, now: str) -> list[OutboxRow]:
    """status=pending 且(无重试时间或已到点)的行,按 id 升序。"""
    rows = conn.execute(
        "SELECT * FROM outbox WHERE status='pending'"
        " AND (next_retry_at IS NULL OR next_retry_at <= ?)"
        " ORDER BY id",
        (now,),
    ).fetchall()
    return [OutboxRow.from_row(r) for r in rows]

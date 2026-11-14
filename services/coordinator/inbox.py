"""inbox:飞书事件接入的第一道幂等闸。

依据:落地方案 v1.2 sa-coordinator 职责 1——
「按飞书 event_id/message_id 建唯一约束,任务接入先落库;重复事件不重复建任务」。
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ClaimResult:
    new: bool
    taskid: str | None
    prev_taskid: str | None  # 命中已存在事件时返回其登记的 taskid


def claim_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    message_id: str,
    kind: str,
    payload: dict,
    commit: bool = True,
) -> ClaimResult:
    """尝试占位一个事件;event_id 或 message_id 任一重复即判定为重复事件。

    commit=False 时并入调用方事务(原子化路径);此时失败不回滚外层事务,
    由调用方统一 commit/rollback。
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT INTO inbox_events"
            " (event_id, message_id, kind, taskid, payload, received_at)"
            " VALUES (?,?,?,?,?,?)",
            (event_id, message_id, kind, None,
             json.dumps(payload, ensure_ascii=False), now),
        )
        if commit:
            conn.commit()
        return ClaimResult(new=True, taskid=None, prev_taskid=None)
    except sqlite3.IntegrityError:
        if commit:
            conn.rollback()
        prev = conn.execute(
            "SELECT taskid FROM inbox_events WHERE event_id=? OR message_id=?",
            (event_id, message_id),
        ).fetchone()
        prev_taskid = prev[0] if prev else None
        return ClaimResult(new=False, taskid=None, prev_taskid=prev_taskid)


def bind_taskid(conn: sqlite3.Connection, event_id: str, taskid: str) -> None:
    """占位成功后回填 taskid(claim 与建任务通常同事务,这里显式提交)。"""
    conn.execute(
        "UPDATE inbox_events SET taskid=? WHERE event_id=?", (taskid, event_id)
    )
    conn.commit()

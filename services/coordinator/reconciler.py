"""reconciler:启动对账、崩溃窗口恢复与卡死任务巡检。

依据:落地方案 v1.2 sa-coordinator 职责 5 与独立 Watchdog 超时口径——
running 超 4 小时未推进、report_pending 超 10 分钟未回投、
awaiting_decision 超 24 小时未拍板;received/clarifying 属前置阶段,
超过 30 分钟视为卡死(派单/澄清未推进)。Phase A 只报告不改状态,
告警外发由独立 Watchdog/systemd 承担;例外:dispatch 行已发出但
CAS 未落(发送后崩溃窗口)用已存 sent_msg_id 幂等补迁移。
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .coordinator import Coordinator
from .registry import cas_update, get_task, list_non_terminal
from . import outbox

_THRESHOLDS = {
    "received": timedelta(minutes=30),
    "clarifying": timedelta(minutes=30),
    "running": timedelta(hours=4),
    "report_pending": timedelta(minutes=10),
    "awaiting_decision": timedelta(hours=24),
}


@dataclass
class ReconcileReport:
    non_terminal: int
    resent: int
    stuck: list  # [{taskid, status, reason}]


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=dt.tzinfo or timezone.utc)  # naive 视为 UTC


def _backfill_sent_dispatches(conn) -> int:
    """崩溃窗口恢复:dispatch 行已 sent 但任务仍 received → 补 CAS。

    不变式:dispatch 行 status='sent' 意味着种子消息已发出且 msg_id 已存,
    用它幂等补做 received→running + topic_root/active_role 写回,绝不重发。
    """
    fixed = 0
    rows = conn.execute(
        "SELECT o.taskid, o.sent_msg_id, o.payload FROM outbox o"
        " JOIN tasks t ON t.taskid = o.taskid"
        " WHERE o.kind='dispatch' AND o.status='sent'"
        " AND t.status='received' AND t.topic_root_msg_id IS NULL"
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload"])
        task = get_task(conn, row["taskid"])
        if task is None:
            continue
        fields = {"topic_root_msg_id": row["sent_msg_id"],
                  "group_chat_id": payload.get("chat_id")}
        if payload.get("active_role"):
            fields["active_role"] = payload["active_role"]
        try:
            cas_update(conn, task.taskid, task.version,
                       status="running", **fields)
            fixed += 1
        except Exception:  # noqa: BLE001 — 状态已被并发推进等,以现状为准
            continue
    return fixed


def scan_stuck(conn, now: str) -> list[dict]:
    """检出卡死任务(只报告不改状态);供 reconciler 与 serve 周期巡检共用。"""
    now_dt = _parse_iso(now)
    stuck = []
    for t in list_non_terminal(conn):
        threshold = _THRESHOLDS.get(t.status)
        if threshold is None:
            continue
        age = now_dt - _parse_iso(t.updated_at)
        if age > threshold:
            stuck.append({
                "taskid": t.taskid,
                "status": t.status,
                "reason": f"{t.status} 停留 {age} 超过 {threshold}",
            })
    return stuck


def reconcile(conn, sender, now: str) -> ReconcileReport:
    """启动/定时对账:崩溃窗口恢复 + 重发到期 outbox + 检出卡死任务(只报告)。"""
    co = Coordinator(conn, sender)
    _backfill_sent_dispatches(conn)
    # needs_reconcile 行与普通 pending 行统一交给 process_due_outbox;
    # 其内部对 needs_reconcile 行执行 probe 对账(无 probe 时安全滞留)
    resent = co.process_due_outbox(now=now)
    tasks = list_non_terminal(conn)
    stuck = scan_stuck(conn, now)
    return ReconcileReport(non_terminal=len(tasks), resent=resent, stuck=stuck)

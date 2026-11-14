"""reconciler:启动对账与卡死任务巡检。

依据:落地方案 v1.2 sa-coordinator 职责 5 与独立 Watchdog 超时口径——
running 超 4 小时未推进、report_pending 超 10 分钟未回投、
awaiting_decision 超 24 小时未拍板。Phase A 只报告不改状态,
告警外发由独立 Watchdog/systemd 承担。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .coordinator import Coordinator
from .registry import list_non_terminal
from . import outbox

_THRESHOLDS = {
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


def reconcile(conn, sender, now: str) -> ReconcileReport:
    """启动/定时对账:重发到期 outbox + 检出卡死任务(只报告)。"""
    co = Coordinator(conn, sender)
    # needs_reconcile 行与普通 pending 行统一交给 process_due_outbox;
    # 其内部对重试行执行 probe 对账逻辑(probe 缺省时直接重发)
    resent = co.process_due_outbox(now=now)
    now_dt = _parse_iso(now)
    stuck = []
    tasks = list_non_terminal(conn)
    for t in tasks:
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
    return ReconcileReport(non_terminal=len(tasks), resent=resent, stuck=stuck)

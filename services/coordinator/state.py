"""任务状态机:status 合法迁移表与校验。

依据:落地方案 v1.2「任务登记表:状态机」与 Watchdog 超时口径。
- received → clarifying(歧义澄清)/ running(直接派单)
- clarifying → received(澄清答复已记录,可继续路由;v1.2 澄清只发生在派单前)
- running → report_pending(收到 sactl report)
- report_pending → running(需继续工作)/ done(completed 回投完成)/ awaiting_decision(escalated 待拍板)
- awaiting_decision → running(决策已转发话题)
- done 为唯一终态
"""

STATUS = {
    "received",
    "clarifying",
    "running",
    "report_pending",
    "awaiting_decision",
    "done",
}

TERMINAL = {"done"}

TRANSITIONS: dict[str, set[str]] = {
    "received": {"clarifying", "running"},
    "clarifying": {"received"},
    "running": {"report_pending"},
    "report_pending": {"running", "done", "awaiting_decision"},
    "awaiting_decision": {"running"},
    "done": set(),
}


class InvalidTransition(Exception):
    """非法状态迁移。"""

    def __init__(self, status_from: str, status_to: str):
        self.status_from = status_from
        self.status_to = status_to
        super().__init__(f"非法状态迁移: {status_from} → {status_to}")


def validate_transition(status_from: str, status_to: str) -> None:
    """校验迁移合法性,非法抛 InvalidTransition;相同状态也视为非法。"""
    if status_from not in STATUS:
        raise InvalidTransition(status_from, status_to)
    if status_to not in STATUS:
        raise InvalidTransition(status_from, status_to)
    if status_to == status_from:
        raise InvalidTransition(status_from, status_to)
    if status_to not in TRANSITIONS[status_from]:
        raise InvalidTransition(status_from, status_to)


def is_terminal(status: str) -> bool:
    return status in TERMINAL

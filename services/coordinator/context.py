"""任务规范上下文:原文、澄清/修改与当前输入的单一事实来源。"""

import json

from .registry import get_task


def task_context(conn, taskid: str, current: str = "") -> str:
    task = get_task(conn, taskid)
    if task is None:
        from .registry import TaskNotFound
        raise TaskNotFound(taskid)
    row = conn.execute(
        "SELECT payload FROM inbox_events WHERE taskid=?"
        " ORDER BY received_at, event_id LIMIT 1", (taskid,),
    ).fetchone()
    original = ""
    if row is not None:
        try:
            original = str((json.loads(row["payload"]) or {}).get("text") or "")
        except (ValueError, TypeError):
            original = ""
    lines = [f"用户原文: {original or task.title}"]
    for index, decision in enumerate(task.decisions, 1):
        text = str(decision.get("text") or "") if isinstance(decision, dict) else str(decision)
        if text.strip():
            lines.append(f"澄清/修改 {index}: {text}")
    if current.strip():
        lines.append(f"当前输入: {current}")
    return "\n".join(lines)

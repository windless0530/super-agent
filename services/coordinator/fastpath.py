"""控制会话快路径:单次 inbox claim、锁外 Get/verdict、thread 级状态分类。"""

import logging
import threading
from dataclasses import dataclass

from .context import task_context
from .inbox import bind_taskid, claim_event
from .registry import cas_update, create_task, get_task

logger = logging.getLogger("coordinator")


class InvalidInbox(ValueError):
    """控制消息缺少必要字段。"""


@dataclass
class _Prepared:
    action: str
    event: dict
    taskid: str | None
    duplicate: bool
    react: bool
    kind: str


class Fastpath:
    def __init__(self, co, verdict, db_lock, wake_outbox=None):
        self.co = co
        self.verdict = verdict
        self.db_lock = db_lock
        self.wake_outbox = wake_outbox or (lambda: None)
        self._thread_locks = {}
        self._thread_locks_guard = threading.Lock()

    def handle(self, event: dict) -> dict:
        event = self._validate(event)
        key = (event["chat_id"], event["root_msg_id"])
        with self._thread_locks_guard:
            thread_lock = self._thread_locks.setdefault(key, threading.Lock())
        with thread_lock:
            return self._handle_serialized(event)

    def _handle_serialized(self, event: dict) -> dict:
        prepared = self._prepare(event)
        if prepared.react:
            try:
                self.co.sender.add_reaction(event["chat_id"], event["message_id"], "Get")
            except Exception:  # noqa: BLE001
                logger.exception("控制消息 Get 表情失败(继续): %s", event["message_id"])

        if prepared.duplicate:
            task = get_task(self.co.conn, prepared.taskid) if prepared.taskid else None
            retryable = task is not None and (
                task.status == "received"
                or (prepared.action == "clarified" and task.status == "clarifying")
                or (prepared.action == "confirm" and task.status == "awaiting_go")
            )
            if not retryable:
                return {"ok": True, "action": prepared.action,
                        "taskid": prepared.taskid, "duplicate": True,
                        "state": task.status if task else None}

        if prepared.action == "confirm":
            with self.db_lock:
                result = self.co.confirm_task(prepared.taskid)
            self.wake_outbox()
            return {"ok": True, "action": "confirm", "taskid": prepared.taskid,
                    "duplicate": prepared.duplicate, **result}

        if prepared.action == "get":
            if prepared.taskid is None and not prepared.duplicate:
                self.co.sender.send_reply(
                    event["chat_id"], event["root_msg_id"],
                    "请在对应任务的 thread A(话题)回复开工,避免多任务串单。",
                )
            return {"ok": True, "action": "get", "taskid": prepared.taskid,
                    "duplicate": prepared.duplicate}

        result = self._verdict_and_route(prepared)
        return {"ok": True, "action": prepared.action,
                "taskid": result["taskid"], "duplicate": prepared.duplicate,
                "state": result["state"]}

    @staticmethod
    def _validate(event):
        if not isinstance(event, dict):
            raise InvalidInbox("body 必须是 JSON 对象")
        out = dict(event)
        for key in ("message_id", "chat_id", "text"):
            out[key] = str(out.get(key) or "").strip()
            if not out[key]:
                raise InvalidInbox(f"缺少 {key}")
        out["root_msg_id"] = str(out.get("root_msg_id") or out["message_id"]).strip()
        out["sender_open_id"] = str(out.get("sender_open_id") or "")
        return out

    def _bound_task(self, event):
        row = self.co.conn.execute(
            "SELECT taskid FROM tasks WHERE thread_a_chat_id=?"
            " AND thread_a_msg_id=?", (event["chat_id"], event["root_msg_id"]),
        ).fetchone()
        return get_task(self.co.conn, row[0]) if row else None

    @staticmethod
    def _kind(task, text):
        if task is None:
            return "control" if text == "开工" else "task"
        if task.status == "awaiting_go":
            return "confirm" if text == "开工" else "revise"
        if task.status == "clarifying":
            return "clarify"
        return "control"

    def _prepare(self, event) -> _Prepared:
        with self.db_lock:
            try:
                bound = self._bound_task(event)
                kind = self._kind(bound, event["text"])
                claim = claim_event(
                    self.co.conn, event_id=f"msg:{event['message_id']}",
                    message_id=event["message_id"], kind=kind,
                    payload=event, commit=False,
                )
                if not claim.new:
                    return self._resume_duplicate(event, kind)
                taskid = None
                action = "get"
                if bound is not None:
                    taskid = bound.taskid
                    bind_taskid(self.co.conn, f"msg:{event['message_id']}", taskid,
                                commit=False)
                    if kind == "confirm":
                        action = "confirm"
                    elif kind == "revise":
                        task = cas_update(
                            self.co.conn, taskid, bound.version,
                            status="received", commit=False,
                        )
                        cas_update(
                            self.co.conn, taskid, task.version,
                            decisions=list(bound.decisions) + [{"text": event["text"]}],
                            commit=False,
                        )
                        action = "ingest"
                    elif kind == "clarify":
                        action = "clarified"
                elif kind == "task":
                    created = self.co.ingest_task(
                        event_id=f"msg:{event['message_id']}",
                        message_id=event["message_id"], text=event["text"],
                        task_type="PENDING", title=event["text"][:60],
                        thread_chat_id=event["chat_id"],
                        thread_root_msg_id=event["root_msg_id"],
                        allow_placeholder_type=True, commit=False,
                    )
                    taskid = created.taskid
                    action = "ingest"
                self.co.conn.commit()
                return _Prepared(action, event, taskid, False, True, kind)
            except Exception:
                self.co.conn.rollback()
                raise

    def _resume_duplicate(self, event, attempted_kind) -> _Prepared:
        event_id = f"msg:{event['message_id']}"
        row = self.co.conn.execute(
            "SELECT kind, taskid FROM inbox_events WHERE event_id=?", (event_id,)
        ).fetchone()
        kind = row["kind"] if row else attempted_kind
        taskid = row["taskid"] if row else None
        task = get_task(self.co.conn, taskid) if taskid else None
        if task is None and kind == "task":
            created = self.co.ingest_task(
                event_id=event_id, message_id=event["message_id"], text=event["text"],
                task_type="PENDING", title=event["text"][:60],
                thread_chat_id=event["chat_id"],
                thread_root_msg_id=event["root_msg_id"],
                allow_placeholder_type=True, commit=False,
            )
            self.co.conn.commit()
            return _Prepared("ingest", event, created.taskid, True, False, kind)
        action = {
            "task": "ingest", "revise": "ingest", "clarify": "clarified",
            "confirm": "confirm", "control": "get",
        }.get(kind, "get")
        # received 表示此前 verdict 失败;澄清/修改已提交时只需重做 task verdict。
        if task is not None and task.status == "received":
            action = "clarified" if kind == "clarify" else "ingest"
        return _Prepared(action, event, taskid, True, False, kind)

    def _verdict_and_route(self, prepared):
        from .cli import _handle_route

        event = prepared.event
        taskid = prepared.taskid
        task = get_task(self.co.conn, taskid)
        if task is None:
            raise InvalidInbox("无法绑定任务")
        if prepared.action == "clarified" and task.status == "clarifying":
            verdict = self.verdict.decide(
                mode="clarification",
                context=task_context(self.co.conn, taskid, event["text"]),
            )
            with self.db_lock:
                if verdict["is_new_task"]:
                    new_task = create_task(
                        self.co.conn, type="PENDING", title=event["text"][:60],
                        message_id=event["message_id"],
                        event_id=f"msg:{event['message_id']}",
                        thread_a_msg_id=event["message_id"],
                        thread_a_chat_id=event["chat_id"],
                        payload={"text": event["text"]}, placeholder=True,
                        commit=False,
                    )
                    bind_taskid(self.co.conn, f"msg:{event['message_id']}",
                                new_task.taskid, commit=False)
                    self.co.conn.commit()
                    taskid = new_task.taskid
                else:
                    self.co.handle_clarified({
                        "taskid": taskid, "answer": event["text"],
                        "is_new_task": False, "message_id": "",
                    })
        else:
            verdict = self.verdict.decide(
                mode="task", context=task_context(self.co.conn, taskid),
            )
        with self.db_lock:
            result = _handle_route(self.co, {"taskid": taskid, **{
                k: v for k, v in verdict.items() if k != "is_new_task"
            }})
        self.wake_outbox()
        return result

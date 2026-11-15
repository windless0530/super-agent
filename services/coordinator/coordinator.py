"""sa-coordinator 核心:任务接入、派单、report 处理、决策转发、outbox 推进。

依据:落地方案 v1.2「sa-coordinator:确定性控制层」五类职责。
- inbox/事务/状态:复用 inbox / registry / state 模块。
- sender:所有飞书外发经 Sender 适配器;模糊失败(AmbiguousSendError)
  置 needs_reconcile,重发前必经 MsgProbe 对账,杜绝重复话题/重复回投。
- 事务顺序(dispatch):enqueue(dispatch) 先行,CAS received→running 在
  发送成功后完成——崩溃恢复不变式为「dispatch 行 pending 即派单未完成」。
"""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol, runtime_checkable

from . import outbox
from .inbox import bind_taskid, claim_event
from .registry import (
    Task,
    TaskNotFound,
    cas_update,
    create_task,
    get_task,
)
from .report import InvalidReport, validate_report
from .sender import AmbiguousSendError, SendError
from .state import InvalidTransition

logger = logging.getLogger("coordinator")

RETRY_DELAY_SECONDS = 60


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plus_seconds(iso: str, seconds: int) -> str:
    dt = datetime.fromisoformat(iso)
    return (dt + timedelta(seconds=seconds)).isoformat()


@dataclass
class IngestResult:
    taskid: str
    new: bool


class InvalidClarified(ValueError):
    """clarified 请求字段缺失或非法。"""


class ClarifyRejected(Exception):
    """确定性拒绝(任务非 clarifying);body 随异常携带。"""

    def __init__(self, body: dict):
        self.body = body
        super().__init__(str(body))


class ConfirmRejected(Exception):
    """确认派单被确定性拒绝(任务不在 awaiting_go 或配置不完整)。"""

    def __init__(self, body: dict):
        self.body = body
        super().__init__(str(body))


class ReportRejected(Exception):
    """report 被确定性拒绝(如任务非 running);body 携带原因。"""

    def __init__(self, body: dict):
        self.body = body
        super().__init__(str(body))


@runtime_checkable
class MsgProbe(Protocol):
    def find_seed(self, group_chat_id: str, taskid: str) -> str | None: ...


class Coordinator:
    def __init__(
        self,
        conn,
        sender,
        now: Callable[[], str] = utcnow_iso,
        probe: MsgProbe | None = None,
        db_lock: threading.Lock | None = None,
    ):
        self.conn = conn
        self.sender = sender
        self.now = now
        self.probe = probe
        self.db_lock = db_lock or threading.Lock()

    # ---------- 接入 ----------

    def ingest_task(
        self,
        *,
        event_id: str,
        message_id: str,
        text: str,
        task_type: str,
        title: str,
        thread_chat_id: str | None = None,
        thread_root_msg_id: str | None = None,
        allow_placeholder_type: bool = False,
        commit: bool = True,
    ) -> IngestResult:
        """任务消息接入:幂等(重复事件返回既有任务,不再 Get)。"""
        claim = claim_event(
            self.conn, event_id=event_id, message_id=message_id,
            kind="task", payload={"text": text, "title": title},
            commit=commit,
        )
        if not claim.new and claim.prev_taskid:
            if commit:
                self.conn.commit()
            return IngestResult(taskid=claim.prev_taskid, new=False)
        # 孤儿占位恢复(claim 已入库但 taskid 未回填):
        # ① 任务其实已建(create 提交后、bind 前崩溃)→ 按 thread_a_msg_id 找回;
        # ② 任务未建(claim 后、create 前崩溃)→ 继续建。
        orphan = self.conn.execute(
            "SELECT taskid FROM tasks WHERE thread_a_msg_id=?",
            (thread_root_msg_id or message_id,),
        ).fetchone()
        if orphan is not None:
            bind_taskid(self.conn, event_id, orphan[0], commit=commit)
            return IngestResult(taskid=orphan[0], new=False)
        task = create_task(
            self.conn, type=task_type, title=title,
            message_id=message_id, event_id=event_id,
            thread_a_msg_id=thread_root_msg_id or message_id,
            thread_a_chat_id=thread_chat_id,
            payload={"text": text},
            placeholder=allow_placeholder_type,
            commit=commit,
        )
        bind_taskid(self.conn, event_id, task.taskid, commit=commit)
        # Get 表情:非可靠链路,失败仅记日志
        if thread_chat_id and commit:
            try:
                self.sender.add_reaction(thread_chat_id, thread_root_msg_id or message_id, "Get")
            except Exception:  # noqa: BLE001
                logger.exception("Get 表情失败(忽略): %s", task.taskid)
        return IngestResult(taskid=task.taskid, new=True)

    # ---------- 派单 ----------

    def _group_chat_id(self, task: Task) -> str:
        row = self.conn.execute(
            "SELECT group_chat_id FROM task_types WHERE type_code=?", (task.type,)
        ).fetchone()
        chat_id = row["group_chat_id"] if row else None
        if not chat_id:
            raise ValueError(f"类型 {task.type} 未配置 group_chat_id(P0 建群后回填)")
        return chat_id

    def dispatch(self, taskid: str, *, seed_text: str, mentions: tuple[str, ...],
                 commit: bool = True) -> None:
        """入队种子消息(发送由 process_due_outbox 承担;enqueue-first)。"""
        task = get_task(self.conn, taskid)
        if task is None:
            raise TaskNotFound(taskid)
        chat_id = self._group_chat_id(task)
        outbox.enqueue(
            self.conn, taskid=taskid, kind="dispatch",
            payload={
                "chat_id": chat_id,
                "seed_text": seed_text,
                "mentions": list(mentions),
                "active_role": mentions[0] if mentions else None,
            },
            commit=commit,
        )

    def confirm_task(self, taskid: str) -> dict:
        """awaiting_go → running + dispatch 入队;唯一允许新派单的入口。"""
        import json
        from .context import task_context
        from .seed import render_seed

        task = get_task(self.conn, taskid)
        if task is None:
            raise TaskNotFound(taskid)
        if task.status == "running":
            existing = self.conn.execute(
                "SELECT 1 FROM outbox WHERE taskid=? AND kind='dispatch'", (taskid,)
            ).fetchone()
            if existing:
                return {"ok": True, "taskid": taskid,
                        "state": "running", "duplicate": True}
        if task.status != "awaiting_go":
            raise ConfirmRejected({"ok": False, "reason": "not_awaiting_go",
                                   "status": task.status})
        trow = self.conn.execute(
            "SELECT name, group_chat_id, roles, rounds_limit FROM task_types"
            " WHERE type_code=? AND enabled=1", (task.type,),
        ).fetchone()
        if trow is None or not trow["group_chat_id"]:
            raise ConfirmRejected({"ok": False, "reason": "group_not_configured",
                                   "type": task.type})
        roles = json.loads(trow["roles"])
        mention_rows = {
            r["role"]: r["mention"]
            for r in self.conn.execute("SELECT role, mention FROM role_members")
        }
        mentions = tuple(mention_rows.get(role, role) for role in roles)
        try:
            cas_update(self.conn, taskid, task.version,
                       status="running", commit=False)
            self.dispatch(
                taskid,
                seed_text=render_seed(
                    taskid=taskid, title=task.title, type_name=trow["name"],
                    roles=roles, rounds_limit=trow["rounds_limit"],
                    brief=task_context(self.conn, taskid),
                ),
                mentions=mentions,
                commit=False,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"ok": True, "taskid": taskid,
                "state": "running", "duplicate": False}

    # ---------- report ----------

    def handle_report(self, event: dict) -> dict:
        validate_report(event)
        # 先校验任务存在与状态,再占位事件(C3 原子化的前置守卫):
        # 非 running 的合法重复(已处理过的同 event_id)→ duplicate;
        # 新 event_id 对非 running 任务 → 拒绝,杜绝 report_pending 期间重复回投(C1)
        task = get_task(self.conn, event["taskid"])
        if task is None:
            raise TaskNotFound(event["taskid"])
        if task.status != "running":
            claimed = self.conn.execute(
                "SELECT 1 FROM inbox_events WHERE event_id=?", (event["event_id"],)
            ).fetchone()
            if claimed:
                return {"accepted": False, "duplicate": True}
            raise ReportRejected(
                {"accepted": False, "reason": "not_running", "status": task.status}
            )
        claim = claim_event(
            self.conn, event_id=event["event_id"],
            message_id=f"report:{event['event_id']}",
            kind="report", payload={"outcome": event["outcome"]},
            commit=False,
        )
        if not claim.new:
            return {"accepted": False, "duplicate": True}
        # 单事务:claim + CAS + 入队,失败整体回滚(C3)
        try:
            rounds = (event.get("payload") or {}).get("rounds") or {}
            merged = dict(task.rounds_by_phase)
            for phase, n in rounds.items():
                merged[phase] = n
            task = cas_update(
                self.conn, task.taskid, task.version,
                status="report_pending",
                phase=event["phase"],
                rounds_by_phase=merged,
                commit=False,
            )
            summary = (event.get("payload") or {}).get("summary") or task.title
            doc_links = (event.get("payload") or {}).get("doc_links") or []
            relay_text = f"任务 [{task.taskid}] {event['outcome']}: {summary}"
            if doc_links:
                relay_text += "\n" + "\n".join(doc_links)
            outbox.enqueue(
                self.conn, taskid=task.taskid, kind="relay",
                payload={
                    "chat_id": task.thread_a_chat_id or "",
                    "root": task.thread_a_msg_id,
                    "text": relay_text,
                    "outcome": event["outcome"],
                    "report_event_id": event["event_id"],
                },
                commit=False,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"accepted": True, "taskid": task.taskid, "status": "report_pending"}

    # ---------- 决策与澄清 ----------

    def relay_decision(self, taskid: str, decision_text: str) -> None:
        task = get_task(self.conn, taskid)
        if task is None:
            raise TaskNotFound(taskid)
        if task.status != "awaiting_decision":
            # 拍板只对 awaiting_decision 生效;report_pending 期提前拍板
            # 会借道 report_pending→running 产生半程迁移,必须拒绝
            raise ReportRejected(
                {"accepted": False, "reason": "not_awaiting_decision",
                 "status": task.status}
            )
        task = cas_update(self.conn, taskid, task.version, status="running")
        task = cas_update(
            self.conn, taskid, task.version, decisions=list(task.decisions) + [{"text": decision_text}]
        )
        outbox.enqueue(
            self.conn, taskid=taskid, kind="decision",
            payload={
                "chat_id": task.group_chat_id,
                "root": task.topic_root_msg_id,
                "text": decision_text,
                "mentions": [task.active_role] if task.active_role else [],
            },
        )

    def set_clarifying(self, taskid: str, question: str) -> None:
        task = get_task(self.conn, taskid)
        if task is None:
            raise TaskNotFound(taskid)
        cas_update(self.conn, taskid, task.version, status="clarifying")
        outbox.enqueue(
            self.conn, taskid=taskid, kind="relay",
            payload={
                "chat_id": task.thread_a_chat_id or "",
                "root": task.thread_a_msg_id,
                "text": question,
                "outcome": None,
            },
        )

    def apply_clarified(self, taskid: str, answer_note: str) -> None:
        """澄清答复落库并回到 received(可再次 /route 派单)。"""
        task = get_task(self.conn, taskid)
        if task is None:
            raise TaskNotFound(taskid)
        task = cas_update(self.conn, taskid, task.version, status="received")
        cas_update(
            self.conn, taskid, task.version,
            decisions=list(task.decisions) + [{"text": answer_note}],
        )

    def handle_clarified(self, event: dict) -> dict:
        """澄清答复端点逻辑(评审修复后的不变式):

        1. 校验前置(taskid/answer 非空、任务存在、状态必须 clarifying)——
           任何拒绝都不触碰 inbox,重试不被 duplicate 假成功吞掉;
        2. claim + 落库/建新任务同一事务,异常整体回滚,崩溃窗口不烧 message_id;
        3. is_new 分支用派生 message_id(clarify-answer:<id>),避免与 clarify
           占位行撞 message_id UNIQUE 导致新任务丢失 kind='task' inbox 行。
        """
        taskid = str(event.get("taskid") or "").strip()
        answer = str(event.get("answer") or "").strip()
        is_new = bool(event.get("is_new_task"))
        message_id = str(event.get("message_id") or "").strip()
        if not taskid:
            raise InvalidClarified("缺少 taskid")
        if not answer:
            raise InvalidClarified("缺少 answer")
        task = get_task(self.conn, taskid)
        if task is None:
            raise TaskNotFound(taskid)
        # 真重复(同 message_id 已成功处理过)在状态门之前返回:
        # 占位行只在成功事务里落库,存在即表示首次处理已完成
        if message_id:
            seen = self.conn.execute(
                "SELECT 1 FROM inbox_events WHERE event_id=?",
                (f"clarify:{message_id}",),
            ).fetchone()
            if seen is not None:
                return {"ok": True, "taskid": taskid,
                        "state": task.status, "duplicate": True}
        if task.status != "clarifying":
            raise ClarifyRejected(
                {"ok": False, "reason": "not_clarifying", "status": task.status})

        try:
            if message_id:
                claim = claim_event(
                    self.conn, event_id=f"clarify:{message_id}",
                    message_id=message_id, kind="clarify",
                    payload={"taskid": taskid, "answer": answer,
                             "is_new_task": is_new},
                    commit=False,
                )
                if not claim.new:
                    self.conn.rollback()  # 纯读判定,无未提交写
                    return {"ok": True, "taskid": taskid,
                            "state": task.status, "duplicate": True}
            if is_new:
                msg_key = (f"clarify-answer:{message_id}" if message_id
                           else f"clarify-new:{taskid}:{answer[:32]}")
                r = self.ingest_task(
                    event_id=f"msg:{msg_key}", message_id=msg_key,
                    text=answer, task_type="PENDING", title=answer[:60],
                    thread_chat_id=event.get("chat_id") or None,
                    thread_root_msg_id=msg_key,
                    allow_placeholder_type=True, commit=False,
                )
                self.conn.commit()
                return {"ok": True, "new_taskid": r.taskid, "duplicate": not r.new}
            t = cas_update(self.conn, taskid, task.version,
                           status="received", commit=False)
            cas_update(self.conn, taskid, t.version,
                       decisions=list(task.decisions) + [{"text": answer}],
                       commit=False)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"ok": True, "taskid": taskid, "state": "received",
                "duplicate": False}

    # ---------- outbox 推进 ----------

    def process_due_outbox(self, now: str | None = None) -> int:
        """DB 认领/落库持短锁;probe 与外部发送一律在锁外执行。"""
        now = now or self.now()
        with self.db_lock:
            rows = outbox.pending_due(self.conn, now)
        processed = 0
        for row in rows:
            payload = row.payload
            if row.needs_reconcile:
                if self.probe is None:
                    # 不变式:无对账手段就不重发(宁可滞留,不可重复话题/回投)
                    logger.error(
                        "outbox 行 %s(task=%s)needs_reconcile 且无 probe,跳过重发,"
                        "待 probe 注入或人工处置", row.id, row.taskid,
                    )
                    with self.db_lock:
                        outbox.defer(
                            self.conn, row.id,
                            next_retry_at=_plus_seconds(now, RETRY_DELAY_SECONDS),
                        )
                    continue
                # probe 可能访问网络,不得阻塞 HTTP 的数据库锁。
                found = self.probe.find_seed(payload.get("chat_id", ""), row.taskid)
                if found:
                    with self.db_lock:
                        outbox.mark_sent(self.conn, row.id, found)
                        self._post_send(row.kind, row.taskid, payload, found)
                    processed += 1
                    continue
            # 锁内持久化认领;发送后被 kill 仍会留下 needs_reconcile 行。
            with self.db_lock:
                claimed = outbox.mark_inflight(
                    self.conn, row.id, now=now,
                    next_retry_at=_plus_seconds(now, RETRY_DELAY_SECONDS),
                )
            if not claimed:
                continue
            try:
                sent_msg_id = self._send(row.kind, payload)
            except AmbiguousSendError:
                with self.db_lock:
                    outbox.mark_failed_attempt(
                        self.conn, row.id,
                        next_retry_at=_plus_seconds(now, RETRY_DELAY_SECONDS),
                        needs_reconcile=True,
                    )
                continue
            except SendError as e:
                logger.warning("outbox 行 %s 发送失败: %s", row.id, e)
                with self.db_lock:
                    outbox.mark_failed_attempt(
                        self.conn, row.id,
                        next_retry_at=_plus_seconds(now, RETRY_DELAY_SECONDS),
                    )
                continue
            with self.db_lock:
                outbox.mark_sent(self.conn, row.id, sent_msg_id)
                self._post_send(row.kind, row.taskid, payload, sent_msg_id)
            processed += 1
        return processed

    def _send(self, kind: str, payload: dict) -> str:
        mentions = tuple(payload.get("mentions") or ())
        if kind == "dispatch":
            return self.sender.send_seed(
                payload["chat_id"], payload["seed_text"], mentions
            )
        if kind == "relay":
            return self.sender.send_reply(
                payload["chat_id"], payload["root"], payload["text"], mentions
            )
        if kind in ("decision", "notify"):
            return self.sender.send_reply(
                payload["chat_id"], payload["root"], payload["text"], mentions
            )
        if kind == "reaction":
            self.sender.add_reaction(
                payload["chat_id"], payload["msg_id"], payload["emoji"]
            )
            return payload["msg_id"]
        raise ValueError(f"未知 outbox kind: {kind}")

    def _post_send(self, kind: str, taskid: str, payload: dict, sent_msg_id: str) -> None:
        """发送成功后的确定性状态落库;状态机异常仅记日志(人工/Watchdog 兜底)。"""
        try:
            if kind == "dispatch":
                task = get_task(self.conn, taskid)
                if task is None:
                    return
                fields = {"topic_root_msg_id": sent_msg_id,
                          "group_chat_id": payload["chat_id"]}
                if payload.get("active_role"):
                    fields["active_role"] = payload["active_role"]
                if task.status == "received":
                    cas_update(self.conn, taskid, task.version, status="running", **fields)
                else:
                    cas_update(self.conn, taskid, task.version, **fields)
            elif kind == "relay":
                outcome = payload.get("outcome")
                if outcome == "completed":
                    task = get_task(self.conn, taskid)
                    cas_update(self.conn, taskid, task.version, status="done")
                elif outcome == "escalated":
                    task = get_task(self.conn, taskid)
                    cas_update(self.conn, taskid, task.version, status="awaiting_decision")
        except (InvalidTransition, TaskNotFound):
            # 状态已被并发推进(如重复回投竞态):消息已发出,以登记表现状为准
            logger.exception("post-send 状态迁移跳过: %s %s", kind, taskid)

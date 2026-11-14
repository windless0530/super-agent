"""coordinator 核心测试:接入去重、派单、report 主线、决策转发、崩溃收敛。"""

import pytest

from services.coordinator import db
from services.coordinator.coordinator import Coordinator, IngestResult
from services.coordinator.registry import get_task
from services.coordinator.sender import AmbiguousSendError, SendError

ROUTER_CHAT = "oc_router"


class FakeSender:
    """记录全部调用;可脚本化失败。"""

    def __init__(self):
        self.top_level = []
        self.replies = []
        self.reactions = []
        self.fail_top_level = None  # 异常实例或 None

    def send_top_level(self, chat_id, text, mentions=()):
        if self.fail_top_level is not None:
            raise self.fail_top_level
        msg_id = f"om_top_{len(self.top_level) + 1}"
        self.top_level.append({"chat_id": chat_id, "text": text, "mentions": tuple(mentions), "msg_id": msg_id})
        return msg_id

    def send_reply(self, chat_id, root_msg_id, text, mentions=()):
        msg_id = f"om_reply_{len(self.replies) + 1}"
        self.replies.append({"chat_id": chat_id, "root": root_msg_id, "text": text,
                             "mentions": tuple(mentions), "msg_id": msg_id})
        return msg_id

    def add_reaction(self, chat_id, msg_id, emoji):
        self.reactions.append({"chat_id": chat_id, "msg_id": msg_id, "emoji": emoji})


class FakeProbe:
    def __init__(self, found=None):
        self.found = found  # find_seed 的固定返回
        self.calls = []

    def find_seed(self, group_chat_id, taskid):
        self.calls.append((group_chat_id, taskid))
        return self.found


@pytest.fixture()
def env(tmp_path):
    conn = db.connect(str(tmp_path / "tasks.db"))
    db.init_schema(conn)
    db.seed_task_types(conn)
    conn.execute("UPDATE task_types SET group_chat_id='oc_research' WHERE type_code='RD'")
    conn.commit()
    sender = FakeSender()
    probe = FakeProbe()
    co = Coordinator(conn, sender, probe=probe)
    return conn, sender, probe, co


def _ingest(co, **kw):
    params = dict(
        event_id="ev_1", message_id="om_1", text="帮我调研支付重连",
        task_type="RD", title="支付重连调研", thread_chat_id=ROUTER_CHAT,
    )
    params.update(kw)
    return co.ingest_task(**params)


class TestIngest:
    def test_new_task_creates_and_reacts_get(self, env):
        conn, sender, probe, co = env
        r = _ingest(co)
        assert isinstance(r, IngestResult)
        assert r.new is True
        assert r.taskid == "20261114-001"
        assert len(sender.reactions) == 1
        assert sender.reactions[0]["emoji"] == "Get"
        t = get_task(conn, r.taskid)
        assert t.status == "received"
        assert t.thread_a_chat_id == ROUTER_CHAT

    def test_duplicate_event_same_taskid_single_reaction(self, env):
        conn, sender, probe, co = env
        r1 = _ingest(co)
        r2 = _ingest(co)  # 同 event/message 重投
        assert r2.taskid == r1.taskid
        assert r2.new is False
        assert len(sender.reactions) == 1  # 只 Get 一次

    def test_orphan_claim_recovered(self, env):
        # 崩溃在 claim 之后、建任务之前:inbox 有行但无 taskid → 重建
        conn, sender, probe, co = env
        conn.execute(
            "INSERT INTO inbox_events (event_id, message_id, kind, payload, received_at)"
            " VALUES ('ev_1','om_1','task','{}','2026-11-14T00:00:00')"
        )
        conn.commit()
        r = _ingest(co)
        assert r.new is True
        assert r.taskid == "20261114-001"


class TestDispatch:
    def test_dispatch_sends_seed_and_transitions(self, env):
        conn, sender, probe, co = env
        r = _ingest(co)
        co.dispatch(r.taskid, seed_text="[20261114-001] 支付重连调研", mentions=("design",))
        n = co.process_due_outbox()
        assert n == 1
        assert len(sender.top_level) == 1
        call = sender.top_level[0]
        assert call["chat_id"] == "oc_research"
        assert "支付重连" in call["text"]
        assert call["mentions"] == ("design",)
        t = get_task(conn, r.taskid)
        assert t.status == "running"
        assert t.topic_root_msg_id == call["msg_id"]
        assert t.active_role == "design"

    def test_dispatch_ambiguous_failure_converges_via_probe(self, env):
        conn, sender, probe, co = env
        r = _ingest(co)
        co.dispatch(r.taskid, seed_text="[20261114-001] 标题", mentions=("design",))
        # 第一次发送:模糊失败(消息可能已发出)
        sender.fail_top_level = AmbiguousSendError(["x"], -1, "timeout")
        co.process_due_outbox()
        row = conn.execute("SELECT needs_reconcile, attempts FROM outbox").fetchone()
        assert row["needs_reconcile"] == 1 and row["attempts"] == 1
        assert get_task(conn, r.taskid).status == "received"  # 未迁移
        # 恢复后重跑(退避到点):probe 发现种子已存在 → 不再发送,直接收敛
        sender.fail_top_level = None
        probe.found = "om_seed_exists"
        co_later = Coordinator(conn, sender, now=lambda: "2999-01-01T00:00:00", probe=probe)
        co_later.process_due_outbox()
        assert sender.top_level == []  # 未产生第二个话题
        t = get_task(conn, r.taskid)
        assert t.status == "running"
        assert t.topic_root_msg_id == "om_seed_exists"
        assert probe.calls == [("oc_research", r.taskid)]

    def test_dispatch_senderror_retries(self, env):
        conn, sender, probe, co = env
        r = _ingest(co)
        co.dispatch(r.taskid, seed_text="[20261114-001] 标题", mentions=("design",))
        sender.fail_top_level = SendError(["x"], 1, "boom")
        co.process_due_outbox()
        # 到点后重试成功
        sender.fail_top_level = None
        # 直接把 next_retry_at 置空以模拟到点(时间由 now 注入控制)
        co2 = Coordinator(conn, sender, now=lambda: "2999-01-01T00:00:00", probe=probe)
        co2.process_due_outbox()
        assert len(sender.top_level) == 1

    def test_dispatch_senderror_retries_without_probe(self, env):
        conn, sender, probe, co = env
        r = _ingest(co)
        co.dispatch(r.taskid, seed_text="[20261114-001] 标题", mentions=("design",))
        sender.fail_top_level = SendError(["x"], 1, "definitely not sent")
        co.process_due_outbox()
        sender.fail_top_level = None
        # 生产 serve/once 没有 probe;确定未发出必须可直接重试。
        co2 = Coordinator(conn, sender, now=lambda: "2999-01-01T00:00:00")
        co2.process_due_outbox()
        assert len(sender.top_level) == 1


class TestReport:
    def _running(self, env):
        conn, sender, probe, co = env
        r = _ingest(co)
        co.dispatch(r.taskid, seed_text="s", mentions=("design",))
        co.process_due_outbox()
        return r.taskid

    def test_completed_report_relays_and_closes(self, env):
        conn, sender, probe, co = env
        taskid = self._running(env)
        resp = co.handle_report({
            "event_id": "ev_r1", "taskid": taskid, "outcome": "completed",
            "phase": "designing", "source_msg_id": "om_x", "doc_version": "2",
            "payload": {"summary": "方案完成", "doc_links": ["https://d/1"]},
        })
        assert resp == {"accepted": True, "taskid": taskid, "status": "report_pending"}
        assert get_task(conn, taskid).status == "report_pending"
        co.process_due_outbox()
        t = get_task(conn, taskid)
        assert t.status == "done"
        # 回投到 thread A(router 单聊,根消息 = thread_a_msg_id)
        relay = sender.replies[-1]
        assert relay["chat_id"] == ROUTER_CHAT
        assert relay["root"] == "om_1"
        assert taskid in relay["text"] and "completed" in relay["text"]
        assert "方案完成" in relay["text"]
        assert "https://d/1" in relay["text"]

    def test_escalated_report_awaits_decision(self, env):
        conn, sender, probe, co = env
        taskid = self._running(env)
        co.handle_report({
            "event_id": "ev_r1", "taskid": taskid, "outcome": "escalated",
            "phase": "designing", "source_msg_id": "om_x", "doc_version": "2",
            "payload": {"summary": "存在分歧"},
        })
        co.process_due_outbox()
        assert get_task(conn, taskid).status == "awaiting_decision"
        assert "escalated" in sender.replies[-1]["text"]

    def test_duplicate_report_event_single_relay(self, env):
        conn, sender, probe, co = env
        taskid = self._running(env)
        ev = {"event_id": "ev_r1", "taskid": taskid, "outcome": "completed",
              "phase": "designing", "source_msg_id": "om_x", "doc_version": "2"}
        co.handle_report(ev)
        resp2 = co.handle_report(ev)
        assert resp2["accepted"] is False and resp2["duplicate"] is True
        co.process_due_outbox()
        assert len([r for r in sender.replies if taskid in r["text"]]) == 1

    def test_report_merges_rounds(self, env):
        conn, sender, probe, co = env
        taskid = self._running(env)
        co.handle_report({**{
            "event_id": "ev_r1", "taskid": taskid, "outcome": "completed",
            "phase": "reviewing", "source_msg_id": "om_x", "doc_version": "2"},
            "payload": {"rounds": {"reviewing": 3}}})
        t = get_task(conn, taskid)
        assert t.rounds_by_phase == {"reviewing": 3}
        assert t.phase == "reviewing"

    def test_sent_relay_not_resent_after_mark(self, env):
        # relay 已 mark_sent(发送后、CAS done 前崩溃由 post 动作在同一调用内完成;
        # 这里验证:行 sent 后再跑 process_due_outbox 不再发送)
        conn, sender, probe, co = env
        taskid = self._running(env)
        co.handle_report({"event_id": "ev_r1", "taskid": taskid, "outcome": "completed",
                          "phase": "designing", "source_msg_id": "om_x", "doc_version": "2"})
        co.process_due_outbox()
        before = len(sender.replies)
        assert co.process_due_outbox() == 0
        assert len(sender.replies) == before


class TestDecisionAndClarify:
    def _awaiting(self, env):
        conn, sender, probe, co = env
        r = _ingest(co)
        co.dispatch(r.taskid, seed_text="s", mentions=("design",))
        co.process_due_outbox()
        co.handle_report({"event_id": "ev_r1", "taskid": r.taskid, "outcome": "escalated",
                          "phase": "designing", "source_msg_id": "om_x", "doc_version": "2"})
        co.process_due_outbox()
        return r.taskid

    def test_relay_decision_back_to_topic(self, env):
        conn, sender, probe, co = env
        taskid = self._awaiting(env)
        co.relay_decision(taskid, "拍板:采用方案 A")
        assert get_task(conn, taskid).status == "running"
        co.process_due_outbox()
        d = sender.replies[-1]
        assert d["chat_id"] == "oc_research"           # 发回 worker 群话题
        assert d["mentions"] == ("design",)           # @责任 bot
        assert "拍板:采用方案 A" in d["text"]
        t = get_task(conn, taskid)
        assert t.decisions and t.decisions[-1]["text"] == "拍板:采用方案 A"

    def test_clarify_flow(self, env):
        conn, sender, probe, co = env
        r = _ingest(co)
        co.set_clarifying(r.taskid, "验收标准是什么?")
        assert get_task(conn, r.taskid).status == "clarifying"
        co.process_due_outbox()
        q = sender.replies[-1]
        assert q["chat_id"] == ROUTER_CHAT and "验收标准" in q["text"]
        co.apply_clarified(r.taskid, "答复:重连成功率 > 99%")
        t = get_task(conn, r.taskid)
        assert t.status == "running"
        assert t.decisions[-1]["text"] == "答复:重连成功率 > 99%"


class TestReportGuards:
    """C1:report_pending 期间新 event_id 的 report 不得重复回投。"""

    def _running(self, env):
        conn, sender, probe, co = env
        r = _ingest(co)
        co.dispatch(r.taskid, seed_text="s", mentions=("design",))
        co.process_due_outbox()
        return r.taskid

    def test_new_event_report_while_pending_rejected(self, env):
        from services.coordinator.coordinator import ReportRejected
        conn, sender, probe, co = env
        taskid = self._running(env)
        co.handle_report({"event_id": "ev_r1", "taskid": taskid, "outcome": "completed",
                          "phase": "p", "source_msg_id": "m", "doc_version": "1"})
        # relay 尚未发出(report_pending),worker 用新 event_id 再报 → 拒绝
        with pytest.raises(ReportRejected) as ei:
            co.handle_report({"event_id": "ev_r2", "taskid": taskid,
                              "outcome": "completed", "phase": "p",
                              "source_msg_id": "m", "doc_version": "1"})
        assert ei.value.body["reason"] == "not_running"
        # 推进后 thread A 只收到一次回投
        co.process_due_outbox()
        assert len([x for x in sender.replies if taskid in x["text"]]) == 1

    def test_report_on_done_task_rejected(self, env):
        from services.coordinator.coordinator import ReportRejected
        conn, sender, probe, co = env
        taskid = self._running(env)
        co.handle_report({"event_id": "ev_r1", "taskid": taskid, "outcome": "completed",
                          "phase": "p", "source_msg_id": "m", "doc_version": "1"})
        co.process_due_outbox()
        with pytest.raises(ReportRejected):
            co.handle_report({"event_id": "ev_rX", "taskid": taskid,
                              "outcome": "completed", "phase": "p",
                              "source_msg_id": "m", "doc_version": "1"})

    def test_cas_same_status_param_rejected(self, env):
        # cas_update 显式传 status 与当前相同 → InvalidTransition(status 语义=迁移)
        from services.coordinator.state import InvalidTransition
        conn, sender, probe, co = env
        r = _ingest(co)
        with pytest.raises(InvalidTransition):
            from services.coordinator.registry import cas_update, get_task
            cas_update(conn, r.taskid, get_task(conn, r.taskid).version,
                       status="received")


class TestAtomicity:
    """C3:claim+CAS+enqueue 原子;崩溃后重发不丢报。"""

    def _running(self, env):
        conn, sender, probe, co = env
        r = _ingest(co)
        co.dispatch(r.taskid, seed_text="s", mentions=("design",))
        co.process_due_outbox()
        return co if False else r.taskid

    def test_handle_report_rollback_on_mid_failure(self, env, monkeypatch):
        conn, sender, probe, co = env
        taskid = self._running(env)
        # 入队阶段崩溃(模拟 kill -9 于事务内)
        from services.coordinator import outbox as outbox_mod
        real_enqueue = outbox_mod.enqueue

        def boom(*a, **kw):
            raise RuntimeError("kill -9")

        monkeypatch.setattr(outbox_mod, "enqueue", boom)
        with pytest.raises(RuntimeError):
            co.handle_report({"event_id": "ev_r9", "taskid": taskid,
                              "outcome": "completed", "phase": "p",
                              "source_msg_id": "m", "doc_version": "1"})
        monkeypatch.setattr(outbox_mod, "enqueue", real_enqueue)
        # 事务回滚:无 inbox 残留、状态仍 running
        assert conn.execute(
            "SELECT COUNT(*) FROM inbox_events WHERE event_id='ev_r9'"
        ).fetchone()[0] == 0
        assert get_task(conn, taskid).status == "running"
        # 同 event_id 重发(崩溃恢复)→ 正常受理,不吞
        resp = co.handle_report({"event_id": "ev_r9", "taskid": taskid,
                                 "outcome": "completed", "phase": "p",
                                 "source_msg_id": "m", "doc_version": "1"})
        assert resp["accepted"] is True

    def test_unknown_taskid_leaves_no_claim_row(self, env):
        conn, sender, probe, co = env
        with pytest.raises(Exception):
            co.handle_report({"event_id": "ev_bad", "taskid": "20261114-999",
                              "outcome": "completed", "phase": "p",
                              "source_msg_id": "m", "doc_version": "1"})
        assert conn.execute(
            "SELECT COUNT(*) FROM inbox_events WHERE event_id='ev_bad'"
        ).fetchone()[0] == 0  # 422 路径不落 claim(改正 taskid 重发不被吞)

    def test_orphan_ingest_returns_existing_task(self, env):
        # 崩溃在 create_task 提交后、bind 之前:重投不得建第二个任务
        conn, sender, probe, co = env
        r = _ingest(co)
        conn.execute("UPDATE inbox_events SET taskid=NULL WHERE event_id='ev_1'")
        conn.commit()
        r2 = _ingest(co)
        assert r2.taskid == r.taskid  # 同一任务
        n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert n == 1


class TestReconcileSafety:
    """C2:needs_reconcile 行在无 probe 时禁止盲目重发。"""

    def test_needs_reconcile_row_skipped_without_probe(self, env):
        conn, sender, probe, co = env
        r = _ingest(co)
        co.dispatch(r.taskid, seed_text="s", mentions=("design",))
        sender.fail_top_level = AmbiguousSendError(["x"], -1, "timeout")
        co.process_due_outbox()
        sender.fail_top_level = None
        sender.top_level.clear()
        # 生产 serve 形态:probe 未注入
        co_noprobe = Coordinator(conn, sender, now=lambda: "2999-01-01T00:00:00")
        n = co_noprobe.process_due_outbox()
        assert n == 0                      # 未发送
        assert sender.top_level == []      # 无第二个话题
        row = conn.execute(
            "SELECT status, needs_reconcile, attempts FROM outbox"
        ).fetchone()
        assert row["status"] == "pending"  # 行保留待 probe/人工
        assert row["needs_reconcile"] == 1

    def test_with_probe_still_converges(self, env):
        conn, sender, probe, co = env
        r = _ingest(co)
        co.dispatch(r.taskid, seed_text="s", mentions=("design",))
        sender.fail_top_level = AmbiguousSendError(["x"], -1, "timeout")
        co.process_due_outbox()
        sender.fail_top_level = None
        probe.found = "om_exists"
        co_later = Coordinator(conn, sender, now=lambda: "2999-01-01T00:00:00", probe=probe)
        assert co_later.process_due_outbox() == 1
        assert get_task(conn, r.taskid).status == "running"


class TestDecisionGuard:
    """演示发现:relay_decision 仅允许 awaiting_decision,不得借道 report_pending→running。"""

    def test_relay_decision_rejects_when_not_awaiting(self, env):
        from services.coordinator.coordinator import ReportRejected
        conn, sender, probe, co = env
        r = _ingest(co)
        co.dispatch(r.taskid, seed_text="s", mentions=("design",))
        co.process_due_outbox()
        # report_pending(relay 未发)时拍板 → 拒绝,不产生半程迁移
        co.handle_report({"event_id": "ev_r1", "taskid": r.taskid,
                          "outcome": "escalated", "phase": "p",
                          "source_msg_id": "m", "doc_version": "1"})
        with pytest.raises(ReportRejected):
            co.relay_decision(r.taskid, "拍板:某方案")
        assert get_task(conn, r.taskid).status == "report_pending"
        assert conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='decision'").fetchone()[0] == 0

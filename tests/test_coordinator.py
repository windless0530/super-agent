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

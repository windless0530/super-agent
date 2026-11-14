"""端到端冒烟:一条任务走完 收到→派单→report(escalated)→拍板→report(completed) 全生命周期。

覆盖:状态机全链路、崩溃模拟(重复推进不重复发送)、收尾无未决行。
"""

import json

from services.coordinator import db
from services.coordinator.cli import serve_in_thread
from services.coordinator.coordinator import Coordinator
from services.coordinator.registry import get_task
from tests.test_coordinator import FakeSender


class TestFullLifecycleInProcess:
    def test_research_task_full_lifecycle(self, tmp_path):
        conn = db.connect(str(tmp_path / "tasks.db"))
        db.init_schema(conn)
        db.seed_task_types(conn)
        conn.execute("UPDATE task_types SET group_chat_id='oc_research' WHERE type_code='RD'")
        conn.commit()
        sender = FakeSender()
        co = Coordinator(conn, sender)

        # 1) 接入(用户在 Router 会话发任务 A)
        r = co.ingest_task(event_id="ev_1", message_id="om_1", text="调研支付重连",
                           task_type="RD", title="支付重连调研",
                           thread_chat_id="oc_router")
        assert sender.reactions[0]["emoji"] == "Get"

        # 2) 派单到调研工场
        co.dispatch(r.taskid, seed_text=f"[{r.taskid}] 支付重连调研",
                    mentions=("design",))
        co.process_due_outbox()
        assert get_task(conn, r.taskid).status == "running"

        # 3) 设计 bot 上报分歧(三轮未决)
        co.handle_report({
            "event_id": "ev_r1", "taskid": r.taskid, "outcome": "escalated",
            "phase": "designing", "source_msg_id": "om_5", "doc_version": "2",
            "payload": {"summary": "重连策略存在分歧", "doc_links": ["https://d/1"]},
        })
        co.process_due_outbox()
        assert get_task(conn, r.taskid).status == "awaiting_decision"
        assert any("escalated" in x["text"] for x in sender.replies)

        # 4) 用户在 thread A 拍板 → 转发回话题
        co.relay_decision(r.taskid, "拍板:采用指数退避方案")
        co.process_due_outbox()
        t = get_task(conn, r.taskid)
        assert t.status == "running" and t.decisions[-1]["text"].startswith("拍板")

        # 5) 设计 bot 完成
        co.handle_report({
            "event_id": "ev_r2", "taskid": r.taskid, "outcome": "completed",
            "phase": "designing", "source_msg_id": "om_9", "doc_version": "3",
            "payload": {"summary": "方案定稿", "doc_links": ["https://d/1"]},
        })
        co.process_due_outbox()
        assert get_task(conn, r.taskid).status == "done"

        # 6) 收尾断言:outbox 无未决行;状态轨迹无重复副作用
        pending = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE status!='sent'"
        ).fetchone()[0]
        assert pending == 0
        seeds = [x for x in sender.top_level if r.taskid in x["text"]]
        assert len(seeds) == 1  # 全程只有一个话题

    def test_kill_simulation_no_duplicate_send(self, tmp_path):
        """CAS 前崩溃模拟:relay 行已 mark_sent 后重复推进,不再发送。"""
        conn = db.connect(str(tmp_path / "tasks.db"))
        db.init_schema(conn)
        db.seed_task_types(conn)
        conn.execute("UPDATE task_types SET group_chat_id='oc_r' WHERE type_code='RD'")
        conn.commit()
        sender = FakeSender()
        co = Coordinator(conn, sender)
        r = co.ingest_task(event_id="ev_1", message_id="om_1", text="t",
                           task_type="RD", title="A", thread_chat_id="oc_x")
        co.dispatch(r.taskid, seed_text="s", mentions=("design",))
        co.process_due_outbox()
        co.handle_report({"event_id": "ev_r1", "taskid": r.taskid,
                          "outcome": "completed", "phase": "p",
                          "source_msg_id": "m", "doc_version": "1"})
        co.process_due_outbox()
        replies_before = len(sender.replies)
        assert co.process_due_outbox() == 0            # 崩溃后重启:无事可做
        assert len(sender.replies) == replies_before   # 无重复回投


class TestOverHTTP:
    def test_report_via_real_http(self, tmp_path):
        """sactl 报文经真实 HTTP 进线(IngestResult→dispatch→report→done)。"""
        import urllib.request

        conn = db.connect(str(tmp_path / "tasks.db"))
        db.init_schema(conn)
        db.seed_task_types(conn)
        conn.execute("UPDATE task_types SET group_chat_id='oc_r' WHERE type_code='RD'")
        conn.commit()
        co = Coordinator(conn, FakeSender())
        r = co.ingest_task(event_id="ev_1", message_id="om_1", text="t",
                           task_type="RD", title="A", thread_chat_id="oc_x")
        co.dispatch(r.taskid, seed_text="s", mentions=("design",))
        co.process_due_outbox()

        port_holder = {}
        thread = serve_in_thread(co, port=0, ready=lambda p: port_holder.__setitem__("p", p))
        try:
            body = {"event_id": "ev_http_1", "taskid": r.taskid,
                    "outcome": "completed", "phase": "designing",
                    "source_msg_id": "om_2", "doc_version": "1"}
            req = urllib.request.Request(
                f"http://127.0.0.1:{port_holder['p']}/report",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
            co.process_due_outbox()
            assert get_task(conn, r.taskid).status == "done"
        finally:
            thread.join(timeout=5)

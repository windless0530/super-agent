"""reconciler 测试:启动对账、卡死任务检出。"""

import pytest

from services.coordinator import db
from services.coordinator.coordinator import Coordinator
from services.coordinator.reconciler import reconcile
from tests.test_coordinator import FakeSender


@pytest.fixture()
def env(tmp_path):
    conn = db.connect(str(tmp_path / "tasks.db"))
    db.init_schema(conn)
    db.seed_task_types(conn)
    conn.execute("UPDATE task_types SET group_chat_id='oc_r' WHERE type_code='RD'")
    conn.commit()
    sender = FakeSender()
    co = Coordinator(conn, sender)
    return conn, sender, co


def test_pending_outbox_resent(env):
    conn, sender, co = env
    r = co.ingest_task(event_id="ev_1", message_id="om_1", text="t",
                       task_type="RD", title="标题")
    co.dispatch(r.taskid, seed_text="[x] 标题", mentions=("design",))
    report = reconcile(conn, sender, now="2999-01-01T00:00:00")
    assert report.resent == 1
    assert len(sender.top_level) == 1


def test_stuck_tasks_detected(env):
    conn, sender, co = env
    r1 = co.ingest_task(event_id="ev_1", message_id="om_1", text="t",
                        task_type="RD", title="A")
    co.dispatch(r1.taskid, seed_text="s", mentions=("design",))
    co.process_due_outbox()
    # running 超 4 小时:把 updated_at 拨回 5 小时前
    conn.execute(
        "UPDATE tasks SET updated_at='2000-01-01T00:00:00' WHERE taskid=?", (r1.taskid,)
    )
    conn.commit()
    r2 = co.ingest_task(event_id="ev_2", message_id="om_2", text="t",
                        task_type="RD", title="B")
    from services.coordinator.registry import cas_update, get_task
    cas_update(conn, r2.taskid, get_task(conn, r2.taskid).version, status="running")
    cas_update(conn, r2.taskid, get_task(conn, r2.taskid).version, status="report_pending")
    conn.execute(
        "UPDATE tasks SET updated_at='2000-01-01T00:00:00' WHERE taskid=?", (r2.taskid,)
    )
    conn.commit()
    report = reconcile(conn, sender, now="2999-01-01T00:00:00")
    stuck_ids = {s["taskid"] for s in report.stuck}
    assert r1.taskid in stuck_ids  # running 超时
    assert r2.taskid in stuck_ids  # report_pending 超时
    assert report.non_terminal == 2


def test_fresh_tasks_not_stuck(env):
    conn, sender, co = env
    co.ingest_task(event_id="ev_1", message_id="om_1", text="t",
                   task_type="RD", title="A")
    report = reconcile(conn, sender, now="2000-01-01T00:00:01")
    assert report.stuck == []
    assert report.non_terminal == 1


class TestReviewFixes:
    """I1:dispatch 已发出但 CAS 未落(崩溃窗口)的恢复;received/clarifying 卡死检出。"""

    def test_sent_dispatch_row_backfills_cas(self, env):
        conn, sender, co = env
        r = co.ingest_task(event_id="ev_1", message_id="om_1", text="t",
                           task_type="RD", title="A")
        co.dispatch(r.taskid, seed_text="s", mentions=("design",))
        # 模拟:发送成功 + mark_sent 已落库,随后崩溃,_post_send 未执行
        from services.coordinator import outbox as om
        oid = conn.execute("SELECT id FROM outbox").fetchone()[0]
        om.mark_sent(conn, oid, sent_msg_id="om_seed_77")
        # reconcile 后:补 CAS received→running + topic_root 写回
        report = reconcile(conn, sender, now="2999-01-01T00:00:00")
        from services.coordinator.registry import get_task
        t = get_task(conn, r.taskid)
        assert t.status == "running"
        assert t.topic_root_msg_id == "om_seed_77"
        assert t.active_role == "design"
        assert report.resent == 0  # 不重发

    def test_received_and_clarifying_stuck_detected(self, env):
        conn, sender, co = env
        r1 = co.ingest_task(event_id="ev_1", message_id="om_1", text="t",
                            task_type="RD", title="A")
        r2 = co.ingest_task(event_id="ev_2", message_id="om_2", text="t",
                            task_type="RD", title="B")
        co.set_clarifying(r2.taskid, "验收标准?")
        conn.execute(
            "UPDATE tasks SET updated_at='2000-01-01T00:00:00'"
        )
        conn.commit()
        report = reconcile(conn, sender, now="2999-01-01T00:00:00")
        stuck = {s["taskid"] for s in report.stuck}
        assert r1.taskid in stuck  # received 超时
        assert r2.taskid in stuck  # clarifying 超时

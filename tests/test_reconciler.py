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

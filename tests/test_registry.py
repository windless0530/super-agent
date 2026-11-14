"""registry 模块测试:taskid 分配、建任务、CAS 更新、决策追加、并发。"""

import json

import pytest

from services.coordinator import db, registry
from services.coordinator.state import InvalidTransition
from services.coordinator.registry import (
    StaleVersion,
    TaskNotFound,
    UnknownTaskType,
    allocate_taskid,
    append_decision,
    cas_update,
    create_task,
    get_task,
    list_non_terminal,
)


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "tasks.db"))
    db.init_schema(c)
    db.seed_task_types(c)
    yield c
    c.close()


def _mk(conn, **kw):
    params = dict(
        type="RD",
        title="支付网关重连方案调研",
        message_id="om_1",
        event_id="ev_1",
        thread_a_msg_id="om_root_1",
    )
    params.update(kw)
    return create_task(conn, **params)


class TestAllocateTaskid:
    def test_increments_within_day(self, conn):
        assert allocate_taskid(conn, "20261114") == "20261114-001"
        assert allocate_taskid(conn, "20261114") == "20261114-002"

    def test_resets_next_day(self, conn):
        assert allocate_taskid(conn, "20261113") == "20261113-001"
        assert allocate_taskid(conn, "20261114") == "20261114-001"

    def test_continues_after_real_task_created(self, conn):
        _mk(conn)  # 创建真实任务占 20261114-001
        assert allocate_taskid(conn, "20261114") == "20261114-002"

    def test_concurrent_allocation_no_duplicate(self, conn):
        # 两个连接同日各分配 10 次,结果互不重复
        c2 = db.connect(conn.execute("PRAGMA database_list").fetchone()[2])
        got_a = {allocate_taskid(conn, "20261114") for _ in range(10)}
        got_b = {allocate_taskid(c2, "20261114") for _ in range(10)}
        c2.close()
        assert len(got_a) == 10 and len(got_b) == 10
        assert not (got_a & got_b), f"重复 taskid: {got_a & got_b}"


class TestCreateTask:
    def test_creates_with_defaults(self, conn):
        t = _mk(conn)
        assert t.taskid == "20261114-001"
        assert t.status == "received"
        assert t.version == 0
        assert t.doc_links == []
        assert t.decisions == []
        assert t.rounds_by_phase == {}

    def test_roundtrip_fields(self, conn):
        t = _mk(conn, parent_task_id=None)
        again = get_task(conn, t.taskid)
        assert again.title == t.title
        assert again.type == "RD"
        assert again.thread_a_msg_id == "om_root_1"

    def test_unknown_type_rejected(self, conn):
        with pytest.raises(UnknownTaskType):
            _mk(conn, type="XX", message_id="om_2", event_id="ev_2")

    def test_disabled_type_rejected(self, conn):
        conn.execute("UPDATE task_types SET enabled=0 WHERE type_code='RD'")
        conn.commit()
        with pytest.raises(UnknownTaskType):
            _mk(conn, message_id="om_3", event_id="ev_3")


class TestCasUpdate:
    def test_updates_and_bumps_version(self, conn):
        t = _mk(conn)
        t2 = cas_update(conn, t.taskid, t.version, status="running", active_role="design")
        assert t2.status == "running"
        assert t2.version == t.version + 1
        assert t2.active_role == "design"

    def test_stale_version_rejected(self, conn):
        t = _mk(conn)
        cas_update(conn, t.taskid, t.version, status="running")
        with pytest.raises(StaleVersion):
            cas_update(conn, t.taskid, t.version, status="done")

    def test_illegal_status_rejected(self, conn):
        t = _mk(conn)
        with pytest.raises(InvalidTransition):
            cas_update(conn, t.taskid, t.version, status="done")

    def test_json_fields_serialized(self, conn):
        t = _mk(conn)
        t2 = cas_update(
            conn, t.taskid, t.version,
            doc_links=["https://x/doc1"],
            rounds_by_phase={"designing": 2},
        )
        assert t2.doc_links == ["https://x/doc1"]
        assert t2.rounds_by_phase == {"designing": 2}
        # 库里存的是 JSON 字符串
        raw = conn.execute(
            "SELECT doc_links, rounds_by_phase FROM tasks WHERE taskid=?", (t.taskid,)
        ).fetchone()
        assert json.loads(raw[0]) == ["https://x/doc1"]

    def test_unknown_taskid_raises(self, conn):
        with pytest.raises(registry.TaskNotFound):
            cas_update(conn, "20261114-999", 0, status="running")


class TestDecisionsAndListing:
    def test_append_decision_appends(self, conn):
        t = _mk(conn)
        cas_update(conn, t.taskid, t.version, status="running")
        t = get_task(conn, t.taskid)
        t = append_decision(conn, t.taskid, t.version, {"q": "选 A 还是 B", "pick": "A"})
        t = append_decision(conn, t.taskid, t.version, {"q": "超时时间", "pick": "30s"})
        assert [d["pick"] for d in t.decisions] == ["A", "30s"]

    def test_list_non_terminal(self, conn):
        t = _mk(conn)
        t2 = _mk(conn, message_id="om_9", event_id="ev_9")
        cas_update(conn, t2.taskid, t2.version, status="running")
        t2 = get_task(conn, t2.taskid)
        cas_update(conn, t2.taskid, t2.version, status="report_pending")
        t2 = get_task(conn, t2.taskid)
        cas_update(conn, t2.taskid, t2.version, status="done")
        ids = [x.taskid for x in list_non_terminal(conn)]
        assert ids == [t.taskid]

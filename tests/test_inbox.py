"""inbox 模块测试:event_id/message_id 双唯一约束去重。"""

import pytest

from services.coordinator import db, inbox
from services.coordinator.inbox import ClaimResult, claim_event


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "tasks.db"))
    db.init_schema(c)
    yield c
    c.close()


def test_first_claim_is_new(conn):
    r = claim_event(conn, event_id="ev_1", message_id="om_1", kind="task", payload={"x": 1})
    assert isinstance(r, ClaimResult)
    assert r.new is True
    assert r.prev_taskid is None


def test_same_event_id_duplicate(conn):
    r1 = claim_event(conn, event_id="ev_1", message_id="om_1", kind="task", payload={})
    # 第一次 claim 后登记了 taskid(模拟 coordinator 回填)
    conn.execute("UPDATE inbox_events SET taskid='20261114-001' WHERE event_id='ev_1'")
    conn.commit()
    r2 = claim_event(conn, event_id="ev_1", message_id="om_1", kind="task", payload={})
    assert r2.new is False
    assert r2.prev_taskid == "20261114-001"


def test_same_message_id_different_event_id_is_duplicate(conn):
    claim_event(conn, event_id="ev_1", message_id="om_1", kind="task", payload={})
    conn.execute("UPDATE inbox_events SET taskid='20261114-001' WHERE event_id='ev_1'")
    conn.commit()
    r = claim_event(conn, event_id="ev_2", message_id="om_1", kind="task", payload={})
    assert r.new is False
    assert r.prev_taskid == "20261114-001"


def test_different_kinds_coexist(conn):
    # task 与 report 事件各自独立,互不去重(不同 event_id/message_id)
    r1 = claim_event(conn, event_id="ev_t", message_id="om_t", kind="task", payload={})
    r2 = claim_event(conn, event_id="ev_r", message_id="om_r", kind="report", payload={})
    assert r1.new and r2.new
    n = conn.execute("SELECT COUNT(*) FROM inbox_events").fetchone()[0]
    assert n == 2


def test_prev_taskid_none_when_row_has_no_task(conn):
    claim_event(conn, event_id="ev_1", message_id="om_1", kind="task", payload={})
    r = claim_event(conn, event_id="ev_1", message_id="om_1", kind="task", payload={})
    assert r.new is False
    assert r.prev_taskid is None


def test_payload_stored_as_json(conn):
    claim_event(conn, event_id="ev_1", message_id="om_1", kind="report", payload={"phase": "designing"})
    row = conn.execute("SELECT payload, kind FROM inbox_events WHERE event_id='ev_1'").fetchone()
    import json
    assert json.loads(row[0]) == {"phase": "designing"}
    assert row[1] == "report"

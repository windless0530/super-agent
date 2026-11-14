"""outbox 模块测试:入队、待发扫描、成功/失败标记。"""

import pytest

from services.coordinator import db, outbox
from services.coordinator.outbox import enqueue, mark_failed_attempt, mark_sent, pending_due


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "tasks.db"))
    db.init_schema(c)
    yield c
    c.close()


def _enqueue(conn, **kw):
    params = dict(taskid="20261114-001", kind="dispatch",
                  payload={"chat_id": "oc_1", "seed_text": "[20261114-001] 标题"})
    params.update(kw)
    return enqueue(conn, **params)


def test_enqueue_then_pending_due(conn):
    oid = _enqueue(conn)
    rows = pending_due(conn, now="2026-11-14T10:00:00")
    assert [r.id for r in rows] == [oid]
    assert rows[0].payload["chat_id"] == "oc_1"
    assert rows[0].kind == "dispatch"
    assert rows[0].attempts == 0


def test_mark_sent_removes_from_pending(conn):
    oid = _enqueue(conn)
    mark_sent(conn, oid, sent_msg_id="om_seed_1")
    rows = pending_due(conn, now="2026-11-14T10:00:00")
    assert rows == []
    row = conn.execute("SELECT status, sent_msg_id FROM outbox WHERE id=?", (oid,)).fetchone()
    assert row[0] == "sent"
    assert row[1] == "om_seed_1"


def test_failed_attempt_not_due_until_retry_time(conn):
    oid = _enqueue(conn)
    mark_failed_attempt(conn, oid, next_retry_at="2026-11-14T10:01:00")
    # 未到重试时间:不可见
    assert pending_due(conn, now="2026-11-14T10:00:30") == []
    # 到点:可见,attempts=1
    rows = pending_due(conn, now="2026-11-14T10:01:00")
    assert [r.id for r in rows] == [oid]
    assert rows[0].attempts == 1


def test_needs_reconcile_flag_persisted(conn):
    oid = _enqueue(conn)
    mark_failed_attempt(conn, oid, next_retry_at="2026-11-14T10:01:00", needs_reconcile=True)
    rows = pending_due(conn, now="2026-11-14T10:01:00")
    assert rows[0].needs_reconcile is True


def test_pending_due_ordered_by_id(conn):
    a = _enqueue(conn, kind="relay")
    b = _enqueue(conn, kind="decision")
    rows = pending_due(conn, now="2026-11-14T10:00:00")
    assert [r.id for r in rows] == [a, b]

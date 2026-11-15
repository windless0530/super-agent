"""HTTP /confirm:awaiting_go 是唯一可派单状态,重复确认不重复 dispatch。"""

import json
import urllib.error
import urllib.request

import pytest

from services.coordinator import db
from services.coordinator.cli import serve_in_thread, _handle_ingest
from services.coordinator.coordinator import Coordinator
from tests.test_coordinator import FakeSender


@pytest.fixture()
def server(tmp_path):
    conn = db.connect(str(tmp_path / "tasks.db"))
    db.init_schema(conn)
    db.seed_task_types(conn)
    conn.execute("UPDATE task_types SET group_chat_id='oc_g' WHERE type_code='RD'")
    conn.commit()
    co = Coordinator(conn, FakeSender())
    r = _handle_ingest(co, {"message_id": "om_m", "chat_id": "oc_router",
                            "root_msg_id": "om_root", "text": "调研支付重连"})
    holder = {}
    thread = serve_in_thread(co, port=0, ready=lambda p: holder.__setitem__("p", p))
    yield f"http://127.0.0.1:{holder['p']}", co, r["taskid"]
    thread.join(timeout=5)


def _post(url, path, body):
    req = urllib.request.Request(
        url + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _route(url, taskid, ambiguous=False):
    verdict = ({"ambiguous": True, "clarify_question": "验收标准?"} if ambiguous else
               {"ambiguous": False, "task_type": "RD", "title": "支付重连"})
    return _post(url, "/route", {"taskid": taskid, **verdict})


def test_confirm_awaiting_go_then_duplicate(server):
    url, co, taskid = server
    assert _route(url, taskid)[0] == 200
    status, first = _post(url, "/confirm", {"taskid": taskid})
    assert status == 200
    assert first == {"ok": True, "taskid": taskid, "state": "running", "duplicate": False}
    status, second = _post(url, "/confirm", {"taskid": taskid})
    assert status == 200 and second["duplicate"] is True
    rows = co.conn.execute(
        "SELECT kind FROM outbox WHERE taskid=? AND kind='dispatch'", (taskid,)
    ).fetchall()
    assert len(rows) == 1


def test_confirm_received_and_clarifying_rejected(server):
    url, co, taskid = server
    status, body = _post(url, "/confirm", {"taskid": taskid})
    assert status == 409 and body["reason"] == "not_awaiting_go"
    assert body["status"] == "received"

    _route(url, taskid, ambiguous=True)
    status, body = _post(url, "/confirm", {"taskid": taskid})
    assert status == 409 and body["status"] == "clarifying"
    assert co.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='dispatch'").fetchone()[0] == 0


def test_confirm_report_pending_rejected(server):
    url, co, taskid = server
    _route(url, taskid)
    _post(url, "/confirm", {"taskid": taskid})
    co.handle_report({
        "event_id": "ev_report", "taskid": taskid, "outcome": "completed",
        "phase": "designing", "source_msg_id": "om_w", "doc_version": "1",
    })
    status, body = _post(url, "/confirm", {"taskid": taskid})
    assert status == 409 and body["status"] == "report_pending"


def test_confirm_seed_uses_original_and_clarification_context(server):
    url, co, taskid = server
    _route(url, taskid)
    from services.coordinator.registry import cas_update, get_task
    task = get_task(co.conn, taskid)
    cas_update(co.conn, taskid, task.version,
               decisions=[{"text": "验收:重连成功率 99%"}])
    _post(url, "/confirm", {"taskid": taskid})
    co.process_due_outbox()
    seed = co.sender.top_level[0]["text"]
    assert "调研支付重连" in seed
    assert "澄清/修改 1: 验收:重连成功率 99%" in seed


def test_confirm_unknown_or_missing_task_422(server):
    url, _, _ = server
    assert _post(url, "/confirm", {"taskid": "20261115-999"})[0] == 422
    assert _post(url, "/confirm", {})[0] == 422

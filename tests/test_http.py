"""HTTP 服务测试:report 进线、健康检查、单写者锁。"""

import json
import urllib.request

import pytest

from services.coordinator import db
from services.coordinator.cli import serve_in_thread
from services.coordinator.coordinator import Coordinator
from tests.test_coordinator import FakeSender


@pytest.fixture()
def server(tmp_path):
    db_path = str(tmp_path / "tasks.db")
    conn = db.connect(db_path)
    db.init_schema(conn)
    db.seed_task_types(conn)
    conn.execute("UPDATE task_types SET group_chat_id='oc_g' WHERE type_code='RD'")
    conn.commit()
    co = Coordinator(conn, FakeSender())
    # 预置一个 running 任务供 report 使用
    r = co.ingest_task(event_id="ev_1", message_id="om_1", text="t",
                       task_type="RD", title="标题", thread_chat_id="oc_r")
    co.dispatch(r.taskid, seed_text="s", mentions=("design",))
    co.process_due_outbox()
    port_holder = {}

    def ready(port):
        port_holder["port"] = port

    thread = serve_in_thread(co, port=0, ready=ready)
    yield f"http://127.0.0.1:{port_holder['port']}", r.taskid
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


def _get(url, path):
    with urllib.request.urlopen(url + path, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def test_healthz(server):
    url, _ = server
    status, body = _get(url, "/healthz")
    assert status == 200 and body == {"ok": True}


def test_post_report_accepted(server):
    url, taskid = server
    status, body = _post(url, "/report", {
        "event_id": "ev_r1", "taskid": taskid, "outcome": "completed",
        "phase": "designing", "source_msg_id": "om_x", "doc_version": "1",
    })
    assert status == 200
    assert body["accepted"] is True and body["status"] == "report_pending"


def test_post_report_duplicate(server):
    url, taskid = server
    ev = {"event_id": "ev_r1", "taskid": taskid, "outcome": "completed",
          "phase": "designing", "source_msg_id": "om_x", "doc_version": "1"}
    _post(url, "/report", ev)
    status, body = _post(url, "/report", ev)
    assert status == 200
    assert body == {"accepted": False, "duplicate": True}


def test_post_report_invalid_422(server):
    url, _ = server
    status, body = _post(url, "/report", {"event_id": "ev_x"})  # 缺字段
    assert status == 422
    assert "error" in body


def test_post_report_unknown_task_422(server):
    url, _ = server
    status, _ = _post(url, "/report", {
        "event_id": "ev_r2", "taskid": "20261114-999", "outcome": "completed",
        "phase": "p", "source_msg_id": "m", "doc_version": "1",
    })
    assert status == 422


def test_get_tasks_filter(server):
    url, _ = server
    status, body = _get(url, "/tasks?status=running")
    assert status == 200
    assert any(t["status"] == "running" for t in body["tasks"])


def test_single_writer_lock(tmp_path):
    from services.coordinator.cli import acquire_lock, LockHeld
    lock = tmp_path / "coordinator.lock"
    assert acquire_lock(str(lock)) is not None
    with pytest.raises(LockHeld):
        acquire_lock(str(lock))

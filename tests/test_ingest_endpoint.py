"""HTTP /ingest 端点:Router 会话以用户消息接入,幂等去重(Review Focus #1)。

v1.2 固定动作 1:sa-coordinator 先按 message_id 写 inbox 并加 Get 表情、
生成 taskid;类型与标题此刻未知,以 PENDING 占位,/route 再回填。
"""

import json
import re
import urllib.error
import urllib.request

import pytest

from services.coordinator import db
from services.coordinator.cli import serve_in_thread
from services.coordinator.coordinator import Coordinator
from tests.test_coordinator import FakeSender


@pytest.fixture()
def server(tmp_path):
    conn = db.connect(str(tmp_path / "tasks.db"))
    db.init_schema(conn)
    db.seed_task_types(conn)
    sender = FakeSender()
    co = Coordinator(conn, sender)
    holder = {}
    thread = serve_in_thread(co, port=0, ready=lambda p: holder.__setitem__("p", p))
    yield f"http://127.0.0.1:{holder['p']}", co, sender
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


def test_ingest_creates_pending_task_and_reacts_get(server):
    url, co, sender = server
    status, body = _post(url, "/ingest", {
        "message_id": "om_a", "chat_id": "oc_router",
        "root_msg_id": "om_root", "text": "帮我调研支付网关重连方案",
        "sender_open_id": "ou_user",
    })
    assert status == 200
    assert body == {"ok": True, "taskid": body["taskid"], "duplicate": False}
    assert re.fullmatch(r"\d{8}-\d{3}", body["taskid"])
    # 占位类型 + received 状态,等 /route 回填
    task = co.conn.execute("SELECT * FROM tasks WHERE taskid=?", (body["taskid"],)).fetchone()
    assert task["type"] == "PENDING" and task["status"] == "received"
    assert task["title"] == "帮我调研支付网关重连方案"[:60]
    assert task["thread_a_msg_id"] == "om_root"
    # Get 表情加在 thread A 根消息上
    assert sender.reactions == [
        {"chat_id": "oc_router", "msg_id": "om_root", "emoji": "Get"}
    ]


def test_ingest_same_message_id_is_duplicate(server):
    url, _, _ = server
    payload = {"message_id": "om_dup", "chat_id": "oc_router",
               "root_msg_id": "om_root2", "text": "任务文本"}
    _, first = _post(url, "/ingest", payload)
    _, second = _post(url, "/ingest", payload)
    assert first["duplicate"] is False and second["duplicate"] is True
    assert first["taskid"] == second["taskid"]


def test_ingest_missing_message_id_422(server):
    url, _, _ = server
    status, body = _post(url, "/ingest", {"chat_id": "oc", "text": "t"})
    assert status == 422
    assert "error" in body


def test_ingest_empty_text_422(server):
    url, _, _ = server
    status, body = _post(url, "/ingest", {"message_id": "om_x", "chat_id": "oc", "text": "  "})
    assert status == 422
    assert "error" in body

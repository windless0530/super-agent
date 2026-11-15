"""HTTP /clarified 端点:澄清答复回到可路由态,或识别为新任务(Review Focus #3)。"""

import json
import urllib.error
import urllib.request

import pytest

from services.coordinator import db
from services.coordinator.cli import serve_in_thread, _handle_ingest, _handle_route
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
    _handle_route(co, {"taskid": r["taskid"], "ambiguous": True,
                       "clarify_question": "验收标准?"})
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


def test_clarified_returns_task_to_received(server):
    url, co, taskid = server
    status, body = _post(url, "/clarified", {
        "taskid": taskid, "answer": "验收:重连成功率 > 99%",
        "is_new_task": False, "message_id": "om_ans1",
    })
    assert status == 200
    assert body == {"ok": True, "taskid": taskid, "state": "received",
                    "duplicate": False}
    task = co.conn.execute("SELECT * FROM tasks WHERE taskid=?", (taskid,)).fetchone()
    assert task["status"] == "received"
    decisions = json.loads(task["decisions"])
    assert decisions[-1]["text"] == "验收:重连成功率 > 99%"
    # 回到 received 后可以再次 /route 派单
    status, body = _post(url, "/route", {
        "taskid": taskid, "ambiguous": False,
        "task_type": "RD", "title": "支付重连方案调研",
    })
    assert status == 200 and body["state"] == "awaiting_go"
    status, body = _post(url, "/confirm", {"taskid": taskid})
    assert status == 200 and body["state"] == "running"


def test_clarified_duplicate_message_id_idempotent(server):
    url, co, taskid = server
    payload = {"taskid": taskid, "answer": "验收:X", "is_new_task": False,
               "message_id": "om_ans1"}
    _post(url, "/clarified", payload)
    status, body = _post(url, "/clarified", payload)
    assert status == 200 and body["duplicate"] is True
    decisions = json.loads(co.conn.execute(
        "SELECT decisions FROM tasks WHERE taskid=?", (taskid,)).fetchone()["decisions"])
    assert len(decisions) == 1  # 不重复记录


def test_clarified_new_task_branch(server):
    url, co, taskid = server
    status, body = _post(url, "/clarified", {
        "taskid": taskid, "answer": "这是新任务:调研 Y",
        "is_new_task": True, "message_id": "om_ans2",
        "chat_id": "oc_router", "root_msg_id": "om_root",
    })
    assert status == 200
    assert body["new_taskid"] and body["new_taskid"] != taskid
    # 原任务保持 clarifying
    orig = co.conn.execute("SELECT status FROM tasks WHERE taskid=?",
                           (taskid,)).fetchone()
    assert orig["status"] == "clarifying"
    # 新任务 received、原文进简报
    new = co.conn.execute("SELECT * FROM tasks WHERE taskid=?",
                          (body["new_taskid"],)).fetchone()
    assert new["status"] == "received"


def test_clarified_unknown_task_422(server):
    url, _, _ = server
    status, _ = _post(url, "/clarified", {"taskid": "20261115-999",
                                          "answer": "a", "is_new_task": False})
    assert status == 422


def test_clarified_missing_answer_422(server):
    url, _, taskid = server
    status, _ = _post(url, "/clarified", {"taskid": taskid, "answer": " ",
                                          "is_new_task": False})
    assert status == 422


def co_row(co, taskid):
    return co.conn.execute(
        "SELECT * FROM tasks WHERE taskid=?", (taskid,)).fetchone()


class TestClarifyFixPass:
    """评审修复:先校验后占位、单事务、is_new 派生 message_id(不烧答复)。"""

    def test_second_answer_409_not_clarifying_no_burn(self, server):
        url, co, taskid = server
        # 第一条答复:合法,任务回 received
        s1, _ = _post(url, "/clarified", {"taskid": taskid, "answer": "验收:X",
                                          "is_new_task": False, "message_id": "om_a1"})
        assert s1 == 200
        # 第二条答复(任务已非 clarifying):确定性 409,不得 500、不得烧 message_id
        s2, body = _post(url, "/clarified", {"taskid": taskid, "answer": "补充:Y",
                                             "is_new_task": False, "message_id": "om_a2"})
        assert s2 == 409 and body["reason"] == "not_clarifying"
        burned = co.conn.execute(
            "SELECT 1 FROM inbox_events WHERE message_id='om_a2'").fetchone()
        assert burned is None  # message_id 未被占位烧掉

    def test_empty_answer_422_then_same_message_id_succeeds(self, server):
        url, co, taskid = server
        s1, _ = _post(url, "/clarified", {"taskid": taskid, "answer": "  ",
                                          "is_new_task": False, "message_id": "om_b1"})
        assert s1 == 422
        # 修正后用同一 message_id 重试:必须真实生效,不得 duplicate 假成功
        s2, body = _post(url, "/clarified", {"taskid": taskid, "answer": "验收:Z",
                                             "is_new_task": False, "message_id": "om_b1"})
        assert s2 == 200 and body["duplicate"] is False
        row = co_row(co, taskid)
        assert "验收:Z" in row["decisions"]

    def test_is_new_has_task_inbox_row(self, server):
        url, co, taskid = server
        s, body = _post(url, "/clarified", {
            "taskid": taskid, "answer": "新任务:调研 Y", "is_new_task": True,
            "message_id": "om_c1", "chat_id": "oc_router", "root_msg_id": "om_root"})
        assert s == 200
        row = co.conn.execute(
            "SELECT 1 FROM inbox_events WHERE taskid=? AND kind='task'",
            (body["new_taskid"],)).fetchone()
        assert row is not None  # 新任务必须有 kind='task' 的 inbox 行

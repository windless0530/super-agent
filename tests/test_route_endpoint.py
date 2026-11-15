"""HTTP /route 端点:Router verdict → 澄清或派单(Review Focus #2)。"""

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
    sender = FakeSender()
    co = Coordinator(conn, sender)
    r = _handle_ingest(co, {"message_id": "om_m", "chat_id": "oc_router",
                            "root_msg_id": "om_root", "text": "调研支付重连,验收:给出推荐方案"})
    holder = {}
    thread = serve_in_thread(co, port=0, ready=lambda p: holder.__setitem__("p", p))
    yield f"http://127.0.0.1:{holder['p']}", co, sender, r["taskid"]
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


def test_route_full_verdict_dispatches(server):
    url, co, sender, taskid = server
    status, body = _post(url, "/route", {
        "taskid": taskid, "ambiguous": False,
        "task_type": "RD", "title": "支付网关重连方案调研",
    })
    assert status == 200
    assert body == {"ok": True, "taskid": taskid, "state": "running"}
    task = co.conn.execute("SELECT * FROM tasks WHERE taskid=?", (taskid,)).fetchone()
    assert task["status"] == "running"
    assert task["type"] == "RD" and task["title"] == "支付网关重连方案调研"
    co.process_due_outbox()
    assert sender.top_level, "种子消息应已发出"
    seed = sender.top_level[0]
    assert seed["chat_id"] == "oc_g"
    assert f"[{taskid}] 支付网关重连方案调研" in seed["text"]
    assert "调研支付重连,验收:给出推荐方案" in seed["text"]  # 用户原文进简报
    assert seed["mentions"] == ("design", "review")  # role_members 未配置 → 角色名回退
    task = co.conn.execute("SELECT * FROM tasks WHERE taskid=?", (taskid,)).fetchone()
    assert task["topic_root_msg_id"] == seed["msg_id"]


def test_route_role_members_override_mentions(server):
    url, co, sender, taskid = server
    co.conn.execute("INSERT INTO role_members(role, mention) VALUES"
                    " ('design','ou_d1:sa-design'), ('review','ou_r1:sa-review')")
    co.conn.commit()
    _post(url, "/route", {"taskid": taskid, "ambiguous": False,
                          "task_type": "RD", "title": "T"})
    co.process_due_outbox()
    assert sender.top_level
    assert sender.top_level[0]["mentions"] == ("ou_d1:sa-design", "ou_r1:sa-review")


def test_route_ambiguous_sets_clarifying(server):
    url, co, sender, taskid = server
    status, body = _post(url, "/route", {
        "taskid": taskid, "ambiguous": True,
        "clarify_question": "验收标准是什么?",
    })
    assert status == 200
    assert body == {"ok": True, "taskid": taskid, "state": "clarifying"}
    task = co.conn.execute("SELECT status FROM tasks WHERE taskid=?", (taskid,)).fetchone()
    assert task["status"] == "clarifying"
    co.process_due_outbox()
    assert sender.replies and "验收标准是什么?" in sender.replies[0]["text"]


def test_route_invalid_422(server):
    url, _, _, taskid = server
    # 未知类型
    status, body = _post(url, "/route", {"taskid": taskid, "ambiguous": False,
                                         "task_type": "XX", "title": "T"})
    assert status == 422 and "error" in body
    # 空 title
    status, _ = _post(url, "/route", {"taskid": taskid, "ambiguous": False,
                                      "task_type": "RD", "title": " "})
    assert status == 422
    # ambiguous 且缺 clarify_question
    status, _ = _post(url, "/route", {"taskid": taskid, "ambiguous": True})
    assert status == 422
    # PENDING 占位不可作为派单类型
    status, _ = _post(url, "/route", {"taskid": taskid, "ambiguous": False,
                                      "task_type": "PENDING", "title": "T"})
    assert status == 422


def test_route_double_dispatch_rejected_409(server):
    url, co, _, taskid = server
    first = _post(url, "/route", {"taskid": taskid, "ambiguous": False,
                                  "task_type": "RD", "title": "T"})
    assert first[0] == 200
    second = _post(url, "/route", {"taskid": taskid, "ambiguous": False,
                                   "task_type": "RD", "title": "T"})
    assert second[0] == 409
    assert second[1]["reason"] == "not_routeable"


def test_route_group_not_configured_409(tmp_path):
    from services.coordinator.cli import _handle_ingest
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    db.seed_task_types(conn)  # RD 无 group_chat_id
    co = Coordinator(conn, FakeSender())
    r = _handle_ingest(co, {"message_id": "om_z", "chat_id": "oc", "text": "x"})
    holder = {}
    thread = serve_in_thread(co, port=0, ready=lambda p: holder.__setitem__("p", p))
    try:
        status, body = _post(f"http://127.0.0.1:{holder['p']}", "/route",
                             {"taskid": r["taskid"], "ambiguous": False,
                              "task_type": "RD", "title": "T"})
        assert status == 409 and body["reason"] == "group_not_configured"
    finally:
        thread.join(timeout=5)


def test_route_unknown_task_422(server):
    url, _, _, _ = server
    status, _ = _post(url, "/route", {"taskid": "20261115-999", "ambiguous": True,
                                      "clarify_question": "q"})
    assert status == 422


class TestRouteFixPass:
    """评审修复:422 不改任务行;CAS+dispatch 单事务(中途异常回滚)。"""

    def _row(self, co, taskid):
        return co.conn.execute(
            "SELECT * FROM tasks WHERE taskid=?", (taskid,)).fetchone()

    def test_422_leaves_task_row_unchanged(self, server):
        url, co, sender, taskid = server
        before = dict(self._row(co, taskid))
        for bad in ({"taskid": taskid, "ambiguous": False, "task_type": "XX",
                     "title": "T"},
                    {"taskid": taskid, "ambiguous": False, "task_type": "RD",
                     "title": " "},
                    {"taskid": taskid, "ambiguous": True},
                    {"taskid": taskid, "ambiguous": False, "task_type": "PENDING",
                     "title": "T"}):
            status, _ = _post(url, "/route", bad)
            assert status == 422
        after = dict(self._row(co, taskid))
        for k in ("status", "version", "type", "title"):
            assert after[k] == before[k], k
        assert co.conn.execute(
            "SELECT COUNT(*) c FROM outbox WHERE taskid=?", (taskid,)
        ).fetchone()["c"] == 0

    def test_cas_and_dispatch_atomic_rollback(self, server, monkeypatch):
        url, co, _, taskid = server
        from services.coordinator.coordinator import Coordinator

        def boom(self, taskid, *, seed_text, mentions):
            raise RuntimeError("kill -9 模拟:dispatch 前崩溃")

        monkeypatch.setattr(Coordinator, "dispatch", boom)
        status, body = _post(url, "/route", {"taskid": taskid, "ambiguous": False,
                                             "task_type": "RD", "title": "T"})
        assert status == 500
        row = self._row(co, taskid)
        assert row["status"] == "received"  # CAS 已回滚,不得停留在 running
        assert row["type"] == "PENDING" and row["title"] != "T"
        assert co.conn.execute(
            "SELECT COUNT(*) c FROM outbox WHERE taskid=?", (taskid,)
        ).fetchone()["c"] == 0

"""POST /inbox:单次 claim、Get 先行、锁外 verdict 与 thread 级状态分类。"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from services.coordinator import db
from services.coordinator.cli import serve_in_thread
from services.coordinator.coordinator import Coordinator
from services.coordinator.llm import VerdictError
from services.coordinator.registry import get_task
from tests.test_coordinator import FakeSender


CLEAR_RD = {"ambiguous": False, "task_type": "RD", "title": "支付重连调研"}


class FakeVerdict:
    def __init__(self, results=None, events=None):
        self.results = list(results or [CLEAR_RD])
        self.calls = []
        self.events = events

    def decide(self, *, mode, context):
        self.calls.append({"mode": mode, "context": context})
        if self.events is not None:
            self.events.append(("verdict", mode))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return dict(result)


@pytest.fixture()
def server(tmp_path):
    conn = db.connect(str(tmp_path / "tasks.db"))
    db.init_schema(conn)
    db.seed_task_types(conn)
    conn.execute("UPDATE task_types SET group_chat_id='oc_g' WHERE type_code='RD'")
    conn.commit()
    sender = FakeSender()
    co = Coordinator(conn, sender)
    verdict = FakeVerdict()
    wakes = []
    holder = {}
    thread = serve_in_thread(
        co, port=0, ready=lambda p: holder.__setitem__("p", p),
        verdict=verdict, wake_outbox=lambda: wakes.append("wake"),
    )
    yield f"http://127.0.0.1:{holder['p']}", co, sender, verdict, wakes
    thread.join(timeout=5)


def _post(url, path, body, timeout=5):
    req = urllib.request.Request(
        url + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url, path, timeout=5):
    with urllib.request.urlopen(url + path, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def _event(message_id="om_new", root=None, text="调研支付网关重连"):
    return {
        "message_id": message_id,
        "chat_id": "oc_router",
        "root_msg_id": root or message_id,
        "text": text,
        "sender_open_id": "ou_user",
    }


def _inbox(url, event):
    return _post(url, "/inbox", event)


def test_new_task_single_claim_get_before_verdict_and_wake(server):
    url, co, sender, verdict, wakes = server
    events = []
    verdict.events = events
    original_add = sender.add_reaction

    def reaction(chat_id, msg_id, emoji):
        events.append(("get", msg_id, co.conn.in_transaction,
                       co.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]))
        original_add(chat_id, msg_id, emoji)

    sender.add_reaction = reaction
    status, body = _inbox(url, _event())
    assert status == 200
    assert body["action"] == "ingest" and body["duplicate"] is False
    taskid = body["taskid"]
    assert get_task(co.conn, taskid).status == "awaiting_go"
    assert co.conn.execute("SELECT COUNT(*) FROM inbox_events").fetchone()[0] == 1
    assert co.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    assert events[0] == ("get", "om_new", False, 1)
    assert events[1] == ("verdict", "task")
    assert verdict.calls[0]["context"] == "用户原文: 调研支付网关重连"
    assert wakes == ["wake"]
    kinds = [r[0] for r in co.conn.execute("SELECT kind FROM outbox")]
    assert kinds == ["relay"]

    status, duplicate = _inbox(url, _event())
    assert status == 200 and duplicate["duplicate"] is True
    assert len(sender.reactions) == 1
    assert len(verdict.calls) == 1
    assert co.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_create_failure_rolls_back_claim_and_taskid_sequence(server):
    url, co, _, _, _ = server
    import services.coordinator.coordinator as coordinator_mod

    real_create = coordinator_mod.create_task

    def create_then_crash(*args, **kwargs):
        real_create(*args, **kwargs)
        raise RuntimeError("after allocate and insert")

    with monkeypatch_context(coordinator_mod, "create_task", create_then_crash):
        status, _ = _inbox(url, _event("om_crash"))
        assert status == 500
        assert co.conn.execute("SELECT COUNT(*) FROM inbox_events").fetchone()[0] == 0
        assert co.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert co.conn.execute("SELECT COUNT(*) FROM taskid_seq").fetchone()[0] == 0

    status, body = _inbox(url, _event("om_crash"))
    assert status == 200 and body["taskid"].endswith("-001")


class monkeypatch_context:
    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.old = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)

    def __exit__(self, *args):
        setattr(self.obj, self.name, self.old)


def test_llm_failure_503_then_same_message_resumes(server):
    url, co, sender, verdict, _ = server
    verdict.results = [VerdictError("down"), CLEAR_RD]
    status, _ = _inbox(url, _event("om_retry"))
    assert status == 503
    taskid = co.conn.execute("SELECT taskid FROM tasks").fetchone()[0]
    assert get_task(co.conn, taskid).status == "received"

    status, body = _inbox(url, _event("om_retry"))
    assert status == 200 and body["taskid"] == taskid
    assert get_task(co.conn, taskid).status == "awaiting_go"
    assert len(sender.reactions) == 1
    assert len(verdict.calls) == 2
    assert co.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_llm_does_not_hold_db_lock(server):
    url, co, _, _, _ = server
    entered = threading.Event()
    release = threading.Event()

    class BlockingVerdict:
        def decide(self, *, mode, context):
            entered.set()
            assert release.wait(3)
            return CLEAR_RD

    # 独立服务,替换默认 fake。
    holder = {}
    thread = serve_in_thread(co, port=0, ready=lambda p: holder.__setitem__("p", p),
                             verdict=BlockingVerdict())
    try:
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault(
            "response", _post(f"http://127.0.0.1:{holder['p']}", "/inbox", _event("om_block"))))
        worker.start()
        assert entered.wait(2)
        assert _get(f"http://127.0.0.1:{holder['p']}", "/tasks", timeout=1)[0] == 200
        release.set()
        worker.join(3)
        assert result["response"][0] == 200
    finally:
        release.set()
        thread.join(timeout=5)


def test_get_is_outside_lock_and_failure_does_not_block_verdict(server):
    url, co, sender, verdict, _ = server
    entered = threading.Event()
    release = threading.Event()

    def blocking_reaction(chat_id, msg_id, emoji):
        entered.set()
        assert release.wait(3)

    sender.add_reaction = blocking_reaction
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault(
        "response", _inbox(url, _event("om_get_lock"))))
    worker.start()
    assert entered.wait(2)
    assert _get(url, "/tasks", timeout=1)[0] == 200
    release.set()
    worker.join(3)
    assert result["response"][0] == 200

    def failed_reaction(chat_id, msg_id, emoji):
        raise RuntimeError("reaction unavailable")

    sender.add_reaction = failed_reaction
    status, _ = _inbox(url, _event("om_get_failed"))
    assert status == 200
    assert len(verdict.calls) == 2


def _two_awaiting_tasks(url):
    _, first = _inbox(url, _event("om_root_1", text="任务一"))
    _, second = _inbox(url, _event("om_root_2", text="任务二"))
    return first["taskid"], second["taskid"]


def test_confirm_is_exact_and_bound_to_its_thread(server):
    url, co, _, _, wakes = server
    first, second = _two_awaiting_tasks(url)
    status, body = _inbox(url, _event("om_go_1", "om_root_1", " 开工 "))
    assert status == 200 and body["action"] == "confirm" and body["taskid"] == first
    assert get_task(co.conn, first).status == "running"
    assert get_task(co.conn, second).status == "awaiting_go"
    assert co.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='dispatch'").fetchone()[0] == 1
    assert wakes[-1] == "wake"

    status, duplicate = _inbox(url, _event("om_go_1", "om_root_1", " 开工 "))
    assert status == 200 and duplicate["duplicate"] is True
    assert co.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='dispatch'").fetchone()[0] == 1


def test_negative_and_isolated_go_never_confirm(server):
    url, co, sender, verdict, _ = server
    first, second = _two_awaiting_tasks(url)
    status, body = _inbox(url, _event("om_no", "om_root_1", "先不开工"))
    assert status == 200 and body["action"] == "ingest"
    assert get_task(co.conn, first).status == "awaiting_go"
    assert get_task(co.conn, second).status == "awaiting_go"
    assert co.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='dispatch'").fetchone()[0] == 0

    status, body = _inbox(url, _event("om_orphan_go", text="开工"))
    assert status == 200 and body["action"] == "get"
    assert co.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    assert co.conn.execute("SELECT COUNT(*) FROM outbox WHERE kind='dispatch'").fetchone()[0] == 0
    assert "对应任务" in sender.replies[-1]["text"]
    assert "话题" in sender.replies[-1]["text"]


def test_clarification_answer_is_merged_and_rerouted(server):
    url, co, _, verdict, _ = server
    verdict.results = [
        {"ambiguous": True, "clarify_question": "验收标准?"},
        {**CLEAR_RD, "is_new_task": False},
    ]
    _, created = _inbox(url, _event("om_clarify", text="调研重连"))
    taskid = created["taskid"]
    assert get_task(co.conn, taskid).status == "clarifying"

    status, body = _inbox(url, _event("om_answer", "om_clarify", "成功率 99%"))
    assert status == 200 and body["action"] == "clarified" and body["taskid"] == taskid
    task = get_task(co.conn, taskid)
    assert task.status == "awaiting_go"
    assert task.decisions[-1]["text"] == "成功率 99%"
    assert verdict.calls[-1]["mode"] == "clarification"
    assert "用户原文: 调研重连" in verdict.calls[-1]["context"]
    assert "当前输入: 成功率 99%" in verdict.calls[-1]["context"]


def test_clarification_new_task_gets_independent_thread_root(server):
    url, co, _, verdict, _ = server
    verdict.results = [
        {"ambiguous": True, "clarify_question": "要做什么?"},
        {**CLEAR_RD, "is_new_task": True},
    ]
    _, created = _inbox(url, _event("om_old", text="我想开发一个"))
    old_taskid = created["taskid"]
    status, body = _inbox(url, _event("om_new_task", "om_old", "调研支付重连"))
    assert status == 200 and body["taskid"] != old_taskid
    assert get_task(co.conn, old_taskid).status == "clarifying"
    new_task = get_task(co.conn, body["taskid"])
    assert new_task.status == "awaiting_go"
    assert new_task.thread_a_msg_id == "om_new_task"
    assert new_task.title == "支付重连调研"


def test_awaiting_go_modification_uses_accumulated_context(server):
    url, co, _, verdict, _ = server
    _, created = _inbox(url, _event("om_modify_root", text="调研重连"))
    taskid = created["taskid"]
    verdict.results = [{**CLEAR_RD, "title": "支付重连可靠性调研"}]
    status, body = _inbox(url, _event("om_modify", "om_modify_root", "补充验收 99%"))
    assert status == 200 and body["action"] == "ingest"
    task = get_task(co.conn, taskid)
    assert task.status == "awaiting_go" and task.title == "支付重连可靠性调研"
    assert task.decisions[-1]["text"] == "补充验收 99%"
    assert verdict.calls[-1]["mode"] == "task"
    assert "澄清/修改 1: 补充验收 99%" in verdict.calls[-1]["context"]

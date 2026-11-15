"""sactl 测试:report 子命令的报文、重试幂等与退出码。"""

import io
import json
import sys
import urllib.error
import urllib.request

import pytest

# tools/ 不是包,sactl 是可执行脚本;用 importlib 按路径加载
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "sactl", "tools/sactl", loader=importlib.machinery.SourceFileLoader("sactl", "tools/sactl")
)
sactl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sactl)


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = json.dumps(body or {}).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class URLOk:
    """脚本化 urlopen:记录请求,按序返回预设响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append({
            "url": req.full_url,
            "method": req.get_method(),
            "body": json.loads(req.data.decode()),
        })
        if not self.responses:
            raise AssertionError("意外的一次额外请求")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _run(monkeypatch, responses, argv):
    fake = URLOk(responses)
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    monkeypatch.setattr(sactl.time, "sleep", lambda s: None)  # 不真等退避
    code = sactl.main(["report"] + argv)
    return code, fake


def test_report_ok_exit0(monkeypatch):
    code, fake = _run(
        monkeypatch,
        [FakeResponse(200, {"accepted": True})],
        ["--taskid", "20261114-001", "--outcome", "completed",
         "--phase", "designing", "--source-msg-id", "om_9", "--doc-version", "2"],
    )
    assert code == 0
    body = fake.requests[0]["body"]
    assert body["taskid"] == "20261114-001"
    assert body["outcome"] == "completed"
    assert body["event_id"]  # 自动生成
    assert fake.requests[0]["url"].endswith("/report")


def test_report_duplicate_exit0(monkeypatch):
    code, _ = _run(
        monkeypatch,
        [FakeResponse(200, {"accepted": False, "duplicate": True})],
        ["--taskid", "t-1", "--outcome", "completed", "--phase", "p",
         "--source-msg-id", "m", "--doc-version", "1"],
    )
    assert code == 0


def test_report_invalid_exit3(monkeypatch):
    code, _ = _run(
        monkeypatch,
        [FakeResponse(422, {"error": "缺少必填字段"})],
        ["--taskid", "t-1", "--outcome", "completed", "--phase", "p",
         "--source-msg-id", "m", "--doc-version", "1"],
    )
    assert code == 3


def test_report_retries_same_event_id(monkeypatch):
    # 500 → 500 → 200:重试复用同一 event_id,共 3 次请求
    code, fake = _run(
        monkeypatch,
        [
            urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b"{}")),
            urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b"{}")),
            FakeResponse(200, {"accepted": True}),
        ],
        ["--taskid", "t-1", "--outcome", "escalated", "--phase", "p",
         "--source-msg-id", "m", "--doc-version", "1"],
    )
    assert code == 0
    assert len(fake.requests) == 3
    ids = {r["body"]["event_id"] for r in fake.requests}
    assert len(ids) == 1  # event_id 生成一次复用


def test_report_unreachable_exit4(monkeypatch):
    code, _ = _run(
        monkeypatch,
        [urllib.error.URLError("conn refused")] * 4,
        ["--taskid", "t-1", "--outcome", "completed", "--phase", "p",
         "--source-msg-id", "m", "--doc-version", "1"],
    )
    assert code == 4


def test_report_payload_file(monkeypatch, tmp_path):
    p = tmp_path / "cards.json"
    p.write_text(json.dumps({
        "summary": "分歧未决",
        "doc_links": ["https://d/1"],
        "rounds": {"reviewing": 3},
        "disputes": [{"viewpoint_a": "A", "viewpoint_b": "B", "evidence": "e",
                      "impact": "i", "suggestion": "s"}],
    }, ensure_ascii=False), encoding="utf-8")
    code, fake = _run(
        monkeypatch,
        [FakeResponse(200, {"accepted": True})],
        ["--taskid", "t-1", "--outcome", "escalated", "--phase", "p",
         "--source-msg-id", "m", "--doc-version", "1", "--payload-file", str(p)],
    )
    assert code == 0
    payload = fake.requests[0]["body"]["payload"]
    assert payload["summary"] == "分歧未决"
    assert payload["disputes"][0]["viewpoint_a"] == "A"


def test_url_env_override(monkeypatch):
    monkeypatch.setenv("SA_COORDINATOR_URL", "http://10.0.0.9:9999")
    code, fake = _run(
        monkeypatch,
        [FakeResponse(200, {"accepted": True})],
        ["--taskid", "t-1", "--outcome", "completed", "--phase", "p",
         "--source-msg-id", "m", "--doc-version", "1"],
    )
    assert code == 0
    assert fake.requests[0]["url"] == "http://10.0.0.9:9999/report"


def test_report_409_no_retry_exit3(monkeypatch):
    # 409(任务非 running)是确定性拒绝:单次请求即返回,退出码 3
    code, fake = _run(
        monkeypatch,
        [FakeResponse(409, {"accepted": False, "reason": "not_running"})],
        ["--taskid", "t-1", "--outcome", "completed", "--phase", "p",
         "--source-msg-id", "m", "--doc-version", "1"],
    )
    assert code == 3
    assert len(fake.requests) == 1


class TestIngest:
    """ingest 子命令:/ingest 报文、退出码与幂等语义。"""

    def _run_ingest(self, monkeypatch, responses, argv):
        fake = URLOk(responses)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        monkeypatch.setattr(sactl.time, "sleep", lambda s: None)
        code = sactl.main(["ingest"] + argv)
        return code, fake

    def test_ingest_ok_exit0(self, monkeypatch, capsys):
        code, fake = self._run_ingest(
            monkeypatch,
            [FakeResponse(200, {"ok": True, "taskid": "20261115-001", "duplicate": False})],
            ["--message-id", "om_a", "--chat-id", "oc_r",
             "--root-msg-id", "om_root", "--text", "帮我调研X",
             "--sender-open-id", "ou_u"],
        )
        assert code == 0
        req = fake.requests[0]
        assert req["url"].endswith("/ingest")
        assert req["body"] == {"message_id": "om_a", "chat_id": "oc_r",
                               "root_msg_id": "om_root", "text": "帮我调研X",
                               "sender_open_id": "ou_u"}
        out = json.loads(capsys.readouterr().out)
        assert out["taskid"] == "20261115-001" and out["duplicate"] is False

    def test_ingest_invalid_exit3(self, monkeypatch):
        code, _ = self._run_ingest(
            monkeypatch,
            [FakeResponse(422, {"error": "缺少 text"})],
            ["--message-id", "om_a", "--chat-id", "oc",
             "--root-msg-id", "r", "--text", ""],
        )
        assert code == 3

    def test_ingest_unreachable_exit4(self, monkeypatch):
        code, _ = self._run_ingest(
            monkeypatch,
            [urllib.error.URLError("down")],
            ["--message-id", "om_a", "--chat-id", "oc",
             "--root-msg-id", "r", "--text", "t"],
        )
        assert code == 4


class TestRoute:
    """route 子命令:verdict 原样透传 /route。"""

    def _run_route(self, monkeypatch, responses, argv):
        fake = URLOk(responses)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        monkeypatch.setattr(sactl.time, "sleep", lambda s: None)
        code = sactl.main(["route"] + argv)
        return code, fake

    def test_route_ok_exit0(self, monkeypatch, capsys):
        code, fake = self._run_route(
            monkeypatch,
            [FakeResponse(200, {"ok": True, "taskid": "20261115-001", "state": "running"})],
            ["--taskid", "20261115-001",
             "--json", '{"ambiguous": false, "task_type": "RD", "title": "T"}'],
        )
        assert code == 0
        req = fake.requests[0]
        assert req["url"].endswith("/route")
        assert req["body"] == {"taskid": "20261115-001", "ambiguous": False,
                               "task_type": "RD", "title": "T"}
        out = json.loads(capsys.readouterr().out)
        assert out["state"] == "running"

    def test_route_stdin_json(self, monkeypatch):
        import io
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"ambiguous": true, "clarify_question": "Q"}'))
        code, fake = self._run_route(
            monkeypatch,
            [FakeResponse(200, {"ok": True, "state": "clarifying"})],
            ["--taskid", "t-1", "--json", "-"],
        )
        assert code == 0
        assert fake.requests[0]["body"]["clarify_question"] == "Q"

    def test_route_422_and_409_exit3(self, monkeypatch):
        code, _ = self._run_route(
            monkeypatch,
            [FakeResponse(422, {"error": "未知 task_type"})],
            ["--taskid", "t", "--json", '{"ambiguous": false}'],
        )
        assert code == 3
        code, _ = self._run_route(
            monkeypatch,
            [FakeResponse(409, {"ok": False, "reason": "not_routeable"})],
            ["--taskid", "t", "--json", '{"ambiguous": false}'],
        )
        assert code == 3

    def test_route_bad_json_exit2(self, monkeypatch):
        code = sactl.main(["route", "--taskid", "t", "--json", "not-json"])
        assert code == 2

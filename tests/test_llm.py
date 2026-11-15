"""DeepSeek verdict 客户端:最小上下文、严格 JSON 与异常脱敏。"""

import json
import urllib.error

import pytest


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def chat_response(verdict):
    return FakeResponse(json.dumps({
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek-flash",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps(verdict)},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }).encode())


def configured_client(monkeypatch, opener, **kwargs):
    from services.coordinator.llm import DeepSeekVerdict

    monkeypatch.setenv("SA_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("SA_LLM_KEY", "secret-test-key")
    monkeypatch.setenv("SA_LLM_MODEL", "deepseek-flash")
    return DeepSeekVerdict(opener=opener, **kwargs)


def test_task_verdict_sends_only_fixed_system_and_current_context(monkeypatch):
    captured = {}

    def opener(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data)
        return chat_response({
            "ambiguous": False,
            "task_type": "DEV",
            "title": "函数调用子树插件",
        })

    client = configured_client(monkeypatch, opener)
    result = client.decide(mode="task", context="用户原文:开发 VS Code 插件")

    assert result == {
        "ambiguous": False, "task_type": "DEV", "title": "函数调用子树插件"
    }
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["timeout"] == 8.0
    assert captured["headers"]["Authorization"] == "Bearer secret-test-key"
    assert captured["body"]["model"] == "deepseek-flash"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    messages = captured["body"]["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert all(term in messages[0]["content"] for term in ("RD", "RS", "DEV", "JSON"))
    assert messages[1] == {"role": "user", "content": "用户原文:开发 VS Code 插件"}


def test_ambiguous_verdict_requires_one_question(monkeypatch):
    client = configured_client(
        monkeypatch,
        lambda req, timeout: chat_response({
            "ambiguous": True, "clarify_question": "希望支持哪些语言?"
        }),
    )
    assert client.decide(mode="task", context="开发一个插件") == {
        "ambiguous": True, "clarify_question": "希望支持哪些语言?"
    }


@pytest.mark.parametrize("is_new", [False, True])
def test_clarification_verdict_has_typed_new_task_flag(monkeypatch, is_new):
    verdict = {
        "is_new_task": is_new,
        "ambiguous": False,
        "task_type": "RD",
        "title": "支付重连调研",
    }
    client = configured_client(
        monkeypatch, lambda req, timeout: chat_response(verdict)
    )
    assert client.decide(mode="clarification", context="原文\n答复") == verdict


@pytest.mark.parametrize("payload", [
    b"not-json",
    json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode(),
    json.dumps({"choices": [{"message": {"content": json.dumps({
        "ambiguous": False, "task_type": "RD", "title": "x", "is_new_task": "no"
    })}}]}).encode(),
])
def test_malformed_verdict_is_rejected(monkeypatch, payload):
    from services.coordinator.llm import VerdictError

    client = configured_client(monkeypatch, lambda req, timeout: FakeResponse(payload))
    mode = "clarification" if b"is_new_task" in payload else "task"
    with pytest.raises(VerdictError):
        client.decide(mode=mode, context="原文")


def test_http_and_timeout_errors_are_verdict_errors_without_key(monkeypatch):
    from services.coordinator.llm import VerdictError

    def http_error(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

    for failing_opener in (
        http_error,
        lambda req, timeout: (_ for _ in ()).throw(TimeoutError("timed out")),
    ):
        client = configured_client(monkeypatch, failing_opener)
        with pytest.raises(VerdictError) as exc:
            client.decide(mode="task", context="原文")
        assert "secret-test-key" not in str(exc.value)


def test_missing_endpoint_or_key_rejected_at_startup(monkeypatch):
    from services.coordinator.llm import DeepSeekVerdict, VerdictError

    monkeypatch.delenv("SA_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("SA_LLM_KEY", raising=False)
    monkeypatch.delenv("SA_LLM_MODEL", raising=False)
    with pytest.raises(VerdictError):
        DeepSeekVerdict(key="key")
    with pytest.raises(VerdictError):
        DeepSeekVerdict(base_url="https://api.deepseek.com")


def test_invalid_mode_rejected_before_external_call(monkeypatch):
    from services.coordinator.llm import VerdictError

    called = False

    def opener(req, timeout):
        nonlocal called
        called = True
        return chat_response({})

    client = configured_client(monkeypatch, opener)
    with pytest.raises(VerdictError):
        client.decide(mode="unknown", context="原文")
    assert called is False

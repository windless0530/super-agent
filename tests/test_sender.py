"""sender 模块测试:适配器命令拼装、失败语义、模板覆盖。"""

import subprocess

import pytest

from services.coordinator.sender import (
    AmbiguousSendError,
    LarkCliSender,
    SendError,
    parse_send_output,
)


class FakeCompleted:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture()
def sender():
    return LarkCliSender(sender_bot="router")


def _patch_run(monkeypatch, behavior):
    """behavior: callable(argv:list[str]) -> FakeCompleted | raise"""
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return behavior(argv)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_send_top_level_builds_command(monkeypatch, sender):
    calls = _patch_run(
        monkeypatch,
        lambda argv: FakeCompleted(stdout='{"message_id": "om_9"}'),
    )
    msg_id = sender.send_top_level("oc_g", "种子文本", mentions=("bot_design",))
    argv = calls[0]
    assert msg_id == "om_9"
    assert argv[:3] == ["botmux", "send", "--bot"]
    assert "router" in argv
    assert "--chat-id" in argv and "oc_g" in argv
    assert "--top-level" in argv
    assert "种子文本" in argv
    assert "--mention" in argv and "bot_design" in argv


def test_send_reply_uses_reply_to(monkeypatch, sender):
    calls = _patch_run(
        monkeypatch,
        lambda argv: FakeCompleted(stdout='{"message_id": "om_r"}'),
    )
    msg_id = sender.send_reply("oc_g", "om_root", "回投文本")
    assert msg_id == "om_r"
    argv = " ".join(calls[0])
    assert "--reply-to" in argv and "om_root" in argv
    # mentions 为空时不拼 --mention
    assert "--mention" not in argv


def test_parse_send_output_both_formats():
    assert parse_send_output('{"message_id": "om_1"}') == "om_1"
    assert parse_send_output('{"data": {"message_id": "om_2"}}') == "om_2"
    assert parse_send_output("not json") is None
    assert parse_send_output('{"foo": 1}') is None


def test_nonzero_rc_raises_send_error(monkeypatch, sender):
    _patch_run(monkeypatch, lambda argv: FakeCompleted(rc=1, stderr="boom"))
    with pytest.raises(SendError) as ei:
        sender.send_top_level("oc_g", "x")
    assert ei.value.rc == 1
    assert "boom" in str(ei.value)


def test_timeout_raises_ambiguous(monkeypatch, sender):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(AmbiguousSendError):
        sender.send_top_level("oc_g", "x")


def test_send_error_without_message_id(monkeypatch, sender):
    # rc=0 但拿不到 message_id:视为失败(非模糊)
    _patch_run(monkeypatch, lambda argv: FakeCompleted(stdout="{}"))
    with pytest.raises(SendError):
        sender.send_top_level("oc_g", "x")


def test_env_template_override(monkeypatch, sender):
    monkeypatch.setenv("SA_SEND_CMD", "my-send --token abc {bot} {chat_id} {text}")
    calls = _patch_run(
        monkeypatch,
        lambda argv: FakeCompleted(stdout='{"message_id": "om_3"}'),
    )
    msg_id = sender.send_top_level("oc_g", "hello")
    assert msg_id == "om_3"
    argv = calls[0]
    assert argv[:2] == ["my-send", "--token"]
    assert "abc" in argv and "hello" in argv


def test_add_reaction(monkeypatch, sender):
    calls = _patch_run(monkeypatch, lambda argv: FakeCompleted(stdout="{}"))
    sender.add_reaction("oc_g", "om_1", "Get")
    argv = calls[0]
    assert argv[0] == "lark-cli"
    joined = " ".join(argv)
    assert "add-reaction" in joined or "reaction" in joined
    assert "Get" in argv


def test_text_not_shell_interpreted(monkeypatch, sender):
    # 文本包含 shell 元字符也只作为单个 argv 元素传递(不经 shell)
    text = "hello; rm -rf / && $(echo pwned)"
    calls = _patch_run(
        monkeypatch,
        lambda argv: FakeCompleted(stdout='{"message_id": "om_z"}'),
    )
    sender.send_top_level("oc_g", text)
    assert text in calls[0]

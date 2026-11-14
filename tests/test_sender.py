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


def test_send_top_level_builds_p0_verified_command(monkeypatch, sender):
    calls = _patch_run(
        monkeypatch,
        lambda argv: FakeCompleted(stdout='{"messageId": "om_9"}'),
    )
    msg_id = sender.send_top_level("oc_g", "种子文本", mentions=("ou_design:设计",))
    argv = calls[0]
    assert msg_id == "om_9"
    assert argv[:2] == ["botmux", "send"]
    assert "--bot" not in argv  # botmux 3.59.10 send 不支持 --bot
    assert "--text" not in argv  # 正文是位置参数
    assert "--chat-id" in argv and "oc_g" in argv
    assert "--top-level" in argv
    assert "种子文本" in argv
    assert "--mention" in argv and "ou_design:设计" in argv
    assert "--no-mention" not in argv


def test_send_reply_uses_into_for_historical_thread(monkeypatch, sender):
    calls = _patch_run(
        monkeypatch,
        lambda argv: FakeCompleted(stdout='{"messageId": "om_r"}'),
    )
    msg_id = sender.send_reply("oc_g", "om_root", "回投文本")
    assert msg_id == "om_r"
    argv = calls[0]
    assert "--into" in argv and "om_root" in argv
    assert "--reply-to" not in argv
    assert "--chat-id" not in argv  # --into 不可与 --chat-id 混用
    assert "回投文本" in argv and "--text" not in argv
    assert "--no-mention" in argv


def test_send_uses_explicit_session_when_configured(monkeypatch, sender):
    monkeypatch.setenv("SA_BOTMUX_SESSION_ID", "session-full-uuid")
    calls = _patch_run(
        monkeypatch,
        lambda argv: FakeCompleted(stdout='{"messageId": "om_s"}'),
    )
    sender.send_top_level("oc_g", "种子文本")
    assert "--session-id" in calls[0]
    assert "session-full-uuid" in calls[0]


def test_parse_send_output_p0_formats():
    assert parse_send_output('{"message_id": "om_1"}') == "om_1"
    assert parse_send_output('{"data": {"message_id": "om_2"}}') == "om_2"
    assert parse_send_output('{"messageId": "om_3"}') == "om_3"
    assert parse_send_output(
        '✓ 已发送 om_4\n进度消息已发送\n{"success":true,"messageId":"om_4"}'
    ) == "om_4"
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


def test_add_reaction_uses_p0_verified_api(monkeypatch, sender):
    calls = _patch_run(monkeypatch, lambda argv: FakeCompleted(stdout="{}"))
    sender.add_reaction("oc_g", "om_1", "Get")
    argv = calls[0]
    assert argv[:4] == ["lark-cli", "im", "reactions", "create"]
    assert "--message-id" in argv and "om_1" in argv
    assert '--data' in argv
    assert '{"reaction_type": {"emoji_type": "Get"}}' in argv
    assert "--as" in argv and "bot" in argv


def test_text_not_shell_interpreted(monkeypatch, sender):
    # 文本包含 shell 元字符也只作为单个 argv 元素传递(不经 shell)
    text = "hello; rm -rf / && $(echo pwned)"
    calls = _patch_run(
        monkeypatch,
        lambda argv: FakeCompleted(stdout='{"message_id": "om_z"}'),
    )
    sender.send_top_level("oc_g", text)
    assert text in calls[0]


class TestReviewFixes:
    """I4/I5:模板 {text} 注入与 rc=0 解析失败的分类。"""

    def test_rc0_unparseable_output_is_ambiguous(self, monkeypatch, sender):
        # rc=0 但拿不到 message_id:消息大概率已发出 → 模糊,须对账而非重发(I5)
        _patch_run(monkeypatch, lambda argv: FakeCompleted(stdout="weird output"))
        with pytest.raises(AmbiguousSendError):
            sender.send_top_level("oc_g", "x")

    def test_env_template_text_quoted_as_single_arg(self, monkeypatch, sender):
        # 自定义模板含 {text}:空格/flag 注入必须被引号挡住,文本保持单元素(I4)
        monkeypatch.setenv(
            "SA_SEND_CMD", "botmux send --bot {bot} --chat-id {chat_id} --text {text}"
        )
        calls = _patch_run(
            monkeypatch,
            lambda argv: FakeCompleted(stdout='{"message_id": "om_q"}'),
        )
        msg = sender.send_top_level("oc_g", " hello world --mention evil_bot ")
        assert msg == "om_q"
        argv = calls[0]
        assert " hello world --mention evil_bot " in argv  # 原样单元素
        assert argv.count("--mention") == 0  # 注入的 flag 未成为独立参数

    def test_env_template_with_text_no_extra_text_flag(self, monkeypatch, sender):
        # 模板已含 {text} 时不再追加 --text(避免重复)
        monkeypatch.setenv("SA_SEND_CMD", "send {bot} {chat_id} --text {text}")
        calls = _patch_run(
            monkeypatch,
            lambda argv: FakeCompleted(stdout='{"message_id": "om_1"}'),
        )
        sender.send_top_level("oc_g", "hi there")
        argv = calls[0]
        assert argv.count("--text") == 1
        assert "hi there" in argv

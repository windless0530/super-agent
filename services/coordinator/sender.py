"""飞书发送适配器:所有外发副作用的唯一出口。

依据:落地方案 v1.2 sa-coordinator 职责 3(sender)与 D3/D4。
- 以指定 bot 身份(botmux 多 bot)向群/会话发消息、回复线程、加表情。
- 命令模板可用环境变量覆盖(SA_SEND_CMD / SA_REACTION_CMD),
  P0 实测校准参数时不改代码。默认参数对齐 botmux send 已实测形态:
  --chat-id / --top-level / --reply-to / --mention。
- 失败语义二分:SendError=确定未发出,可安全重试;
  AmbiguousSendError=不确定(超时/被杀),调用方必须先对账再重发。
"""

import json
import os
import shlex
import subprocess
from typing import Protocol, runtime_checkable

DEFAULT_SEND_CMD = "botmux send --bot {bot} --chat-id {chat_id}"
DEFAULT_REACTION_CMD = (
    "lark-cli im message add-reaction --chat-id {chat_id} --message-id {msg_id} --emoji {emoji}"
)

_TIMEOUT_SECONDS = 30


class SendError(Exception):
    """确定失败:消息未发出。"""

    def __init__(self, cmd: list[str], rc: int, stderr: str):
        self.cmd = cmd
        self.rc = rc
        self.stderr = stderr
        super().__init__(f"发送失败 rc={rc}: {' '.join(cmd[:6])}… stderr={stderr[:200]}")


class AmbiguousSendError(SendError):
    """模糊失败:超时或进程被杀,消息可能已发出;重发前必须对账。"""


@runtime_checkable
class Sender(Protocol):
    def send_top_level(
        self, chat_id: str, text: str, mentions: tuple[str, ...] = ()
    ) -> str: ...

    def send_reply(
        self, chat_id: str, root_msg_id: str, text: str, mentions: tuple[str, ...] = ()
    ) -> str: ...

    def add_reaction(self, chat_id: str, msg_id: str, emoji: str) -> None: ...


def parse_send_output(stdout: str) -> str | None:
    """从 botmux/lark-cli JSON 输出提取 message_id(兼容两级嵌套)。"""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict):
        if isinstance(data.get("message_id"), str):
            return data["message_id"]
        nested = data.get("data")
        if isinstance(nested, dict) and isinstance(nested.get("message_id"), str):
            return nested["message_id"]
    return None


class LarkCliSender:
    """经 botmux / lark-cli 子进程发送;文本作为单参数传递,不经 shell。"""

    def __init__(self, sender_bot: str = "router"):
        self.sender_bot = sender_bot

    def _template(self, env_key: str, default: str) -> str:
        return os.environ.get(env_key, default)

    def _run(self, argv: list[str]) -> str:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as e:
            raise AmbiguousSendError(argv, -1, f"timeout after {_TIMEOUT_SECONDS}s") from e
        except OSError as e:
            raise SendError(argv, -1, str(e)) from e
        if proc.returncode != 0:
            raise SendError(argv, proc.returncode, proc.stderr)
        return proc.stdout

    def _send_template(self) -> str:
        return self._template("SA_SEND_CMD", DEFAULT_SEND_CMD)

    def _base_argv(self, chat_id: str, text: str, template: str) -> list[str]:
        # format_map 容忍全部占位符;全部值先 shlex.quote,
        # 模板含 {text} 时空格/flag 注入被引号挡住,保持单元素
        class _Safe(dict):
            def __missing__(self, key):
                return ""

        rendered = template.format_map(
            _Safe(bot=shlex.quote(self.sender_bot),
                  chat_id=shlex.quote(chat_id),
                  text=shlex.quote(text))
        )
        return [a for a in shlex.split(rendered) if a]

    def send_top_level(
        self, chat_id: str, text: str, mentions: tuple[str, ...] = ()
    ) -> str:
        template = self._send_template()
        argv = self._base_argv(chat_id, text, template) + ["--top-level"]
        if "{text}" not in template:
            argv += ["--text", text]
        if mentions:
            argv += ["--mention", ",".join(mentions)]
        return self._extract(self._run(argv), argv)

    def send_reply(
        self, chat_id: str, root_msg_id: str, text: str, mentions: tuple[str, ...] = ()
    ) -> str:
        template = self._send_template()
        argv = self._base_argv(chat_id, text, template) + ["--reply-to", root_msg_id]
        if "{text}" not in template:
            argv += ["--text", text]
        if mentions:
            argv += ["--mention", ",".join(mentions)]
        return self._extract(self._run(argv), argv)

    def add_reaction(self, chat_id: str, msg_id: str, emoji: str) -> None:
        template = self._template("SA_REACTION_CMD", DEFAULT_REACTION_CMD)
        argv = shlex.split(
            template.format(
                chat_id=shlex.quote(chat_id),
                msg_id=shlex.quote(msg_id),
                emoji=shlex.quote(emoji),
            )
        )
        self._run(argv)

    def _extract(self, stdout: str, argv: list[str]) -> str:
        msg_id = parse_send_output(stdout)
        if msg_id is None:
            # rc=0 意味着消息大概率已发出,解析失败只是输出格式不符:
            # 归类为模糊失败(重发前必须对账),不能当确定失败盲目重发
            raise AmbiguousSendError(
                argv, 0, f"无法从输出提取 message_id: {stdout[:200]}"
            )
        return msg_id

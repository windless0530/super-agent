"""飞书发送适配器:所有外发副作用的唯一出口。

依据:落地方案 v1.2 sa-coordinator 职责 3(sender)与 D3/D4。
- 以指定 bot 身份(botmux 多 bot)向群/会话发消息、回复线程、加表情。
- 命令模板可用环境变量覆盖(SA_SEND_CMD / SA_REACTION_CMD),
  P0 实测校准参数时不改代码。默认参数对齐 botmux send 已实测形态:
  --chat-id / --top-level / --into / --mention。
- 失败语义二分:SendError=确定未发出,可安全重试;
  AmbiguousSendError=不确定(超时/被杀),调用方必须先对账再重发。
"""

import json
import os
import shlex
import subprocess
from typing import Protocol, runtime_checkable

DEFAULT_SEND_CMD = "botmux send --chat-id {chat_id} {text}"
DEFAULT_REPLY_CMD = "botmux send --into {root} {text}"
DEFAULT_REACTION_CMD = (
    "lark-cli im reactions create --message-id {msg_id} "
    "--data {reaction_body} --as bot"
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
    """提取 message_id;兼容 lark-cli 信封与 botmux 的混合文本/camelCase。"""
    candidates = [stdout]
    candidates.extend(reversed(stdout.splitlines()))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        for key in ("message_id", "messageId"):
            if isinstance(data.get(key), str):
                return data[key]
        nested = data.get("data")
        if isinstance(nested, dict):
            for key in ("message_id", "messageId"):
                if isinstance(nested.get(key), str):
                    return nested[key]
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

    def _reply_template(self) -> str:
        # 回复与顶层发送的互斥参数不同,不能继承含 --chat-id 的 SA_SEND_CMD。
        return os.environ.get("SA_REPLY_CMD", DEFAULT_REPLY_CMD)

    def _base_argv(self, chat_id: str, text: str, template: str, root: str = "") -> list[str]:
        # format_map 容忍全部占位符;全部值先 shlex.quote,
        # 模板含 {text} 时空格/flag 注入被引号挡住,保持单元素
        class _Safe(dict):
            def __missing__(self, key):
                return ""

        session_id = os.environ.get("SA_BOTMUX_SESSION_ID", "")
        rendered = template.format_map(
            _Safe(bot=shlex.quote(self.sender_bot),
                  chat_id=shlex.quote(chat_id),
                  text=shlex.quote(text),
                  root=shlex.quote(root),
                  session_id=shlex.quote(session_id))
        )
        argv = [a for a in shlex.split(rendered) if a]
        if session_id and "{session_id}" not in template:
            argv += ["--session-id", session_id]
        return argv

    @staticmethod
    def _finish_send_argv(argv: list[str], text: str, template: str,
                          mentions: tuple[str, ...]) -> list[str]:
        if "{text}" not in template:
            argv.append(text)  # botmux send 正文是位置参数,不支持 --text
        if mentions:
            for mention in mentions:
                argv += ["--mention", mention]
        else:
            argv.append("--no-mention")
        return argv

    def send_top_level(
        self, chat_id: str, text: str, mentions: tuple[str, ...] = ()
    ) -> str:
        template = self._send_template()
        argv = self._base_argv(chat_id, text, template) + ["--top-level"]
        argv = self._finish_send_argv(argv, text, template, mentions)
        return self._extract(self._run(argv), argv)

    def send_reply(
        self, chat_id: str, root_msg_id: str, text: str, mentions: tuple[str, ...] = ()
    ) -> str:
        template = self._reply_template()
        # P0 实测:--into 可回复任意历史 thread;--reply-to 仅限本会话保留的进线,
        # 且二者都不能与 --chat-id 混用。默认回复模板因此只带 --into。
        argv = self._base_argv(chat_id, text, template, root=root_msg_id)
        if "{root}" not in template:
            argv += ["--into", root_msg_id]
        argv = self._finish_send_argv(argv, text, template, mentions)
        return self._extract(self._run(argv), argv)

    def add_reaction(self, chat_id: str, msg_id: str, emoji: str) -> None:
        template = self._template("SA_REACTION_CMD", DEFAULT_REACTION_CMD)
        reaction_body = json.dumps(
            {"reaction_type": {"emoji_type": emoji}}, ensure_ascii=False
        )
        argv = shlex.split(
            template.format(
                chat_id=shlex.quote(chat_id),
                msg_id=shlex.quote(msg_id),
                emoji=shlex.quote(emoji),
                reaction_body=shlex.quote(reaction_body),
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

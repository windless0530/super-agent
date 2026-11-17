#!/usr/bin/env python3
"""控制会话 IM watcher:2s 轮询、原子游标、HTTP inbox 幂等接入。"""

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("im_watcher")

DEFAULT_LIST_CMD = (
    "lark-cli --profile sa-router im +chat-messages-list --chat-id {chat_id} "
    "--start {start} --order asc --page-size 50 --no-reactions --as bot "
    "{page_token_arg}"
)
DEFAULT_THREAD_CMD = (
    "lark-cli --profile sa-router im +threads-messages-list --thread {thread_id} "
    "--order asc --page-size 50 --no-reactions --as bot {page_token_arg}"
)


def _parse_time(value) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000
            return datetime.fromtimestamp(stamp, timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # lark-cli 的 create_time 可能是本地 naive 时间;先按主机时区定位,再统一 UTC。
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


def _sender_open_id(raw):
    sender = raw.get("sender") or {}
    nested = sender.get("sender_id") or {}
    return str(nested.get("open_id") or sender.get("open_id") or sender.get("id") or "")


def normalize_message(raw: dict, bot_ids: tuple[str, ...]) -> dict | None:
    """将 lark-cli 消息归一为 /inbox 输入;bot/撤回/非文本不消费。"""
    if not isinstance(raw, dict) or raw.get("deleted") or raw.get("msg_type") != "text":
        return None
    sender = raw.get("sender") or {}
    sender_type = str(sender.get("sender_type") or "")
    open_id = _sender_open_id(raw)
    if sender_type in ("bot", "app") or open_id in bot_ids:
        return None
    content = raw.get("content")
    text = ""
    if isinstance(content, dict):
        text = str(content.get("text") or "").strip()
    elif isinstance(content, str):
        try:
            decoded = json.loads(content)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict) and "text" in decoded:
            text = str(decoded.get("text") or "").strip()
        else:
            # lark-cli +chat-messages-list 对 text 消息直接返回用户正文字符串。
            text = content.strip()
    created = _parse_time(raw.get("create_time"))
    if not text or created is None:
        return None
    message_id = str(raw.get("message_id") or "")
    if not message_id:
        return None
    return {
        "message_id": message_id,
        "chat_id": "",
        "root_msg_id": str(raw.get("root_id") or message_id),
        "text": text,
        "sender_open_id": open_id,
        "_create_time": created.isoformat(),
    }


class LarkCliMessageReader:
    @staticmethod
    def _page_token_arg(page_token):
        return f"--page-token {shlex.quote(page_token)}" if page_token else ""

    def _run(self, template, **kwargs):
        argv = [a for a in shlex.split(template.format(**kwargs)) if a]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"IM 读取失败: {proc.stderr[:200]}")
        try:
            raw = json.loads(proc.stdout)
        except ValueError as e:
            raise RuntimeError("IM 读取返回非 JSON") from e
        if raw.get("ok") is not True:
            message = str((raw.get("error") or {}).get("message") or "未知错误")
            raise RuntimeError(f"IM 读取失败: {message}")
        return raw.get("data") or {}

    def list_messages(self, chat_id, start, page_token=None) -> dict:
        return self._run(
            os.environ.get("SA_IM_LIST_CMD", DEFAULT_LIST_CMD),
            chat_id=shlex.quote(chat_id), start=shlex.quote(start),
            page_token_arg=self._page_token_arg(page_token),
        )

    def list_thread_messages(self, thread_id, page_token=None) -> dict:
        return self._run(
            os.environ.get("SA_IM_THREAD_CMD", DEFAULT_THREAD_CMD),
            thread_id=shlex.quote(thread_id),
            page_token_arg=self._page_token_arg(page_token),
        )


class CursorStore:
    """JSON 游标;同目录临时文件 + os.replace 保证原子替换。"""

    def __init__(self, path):
        self.path = Path(path)

    def load(self, now: str) -> str:
        if not self.path.exists():
            return now
        data = json.loads(self.path.read_text(encoding="utf-8"))
        cursor = data.get("cursor")
        if not isinstance(cursor, str) or not cursor:
            raise ValueError("IM cursor journal 缺少 cursor")
        return cursor

    def load_threads(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        threads = data.get("threads", {})
        return dict(threads) if isinstance(threads, dict) else {}

    def save(self, cursor: str, threads: dict[str, str] | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        payload = {"cursor": cursor}
        if threads is not None:
            payload["threads"] = threads
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


class HttpInbox:
    def __init__(self, base_url):
        self.url = base_url.rstrip("/") + "/inbox"

    def __call__(self, event: dict) -> dict:
        req = urllib.request.Request(
            self.url, data=json.dumps(event, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"inbox HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise RuntimeError(f"inbox 不可达: {type(e).__name__}") from e
        if body.get("ok") is not True:
            raise RuntimeError("inbox 未受理")
        return body


def sync_once(reader, post, *, chat_id: str, start: str, bot_ids: tuple[str, ...],
              round_started: str, threads: dict[str, str] | None = None) -> str:
    """读取 chat 新根消息并展开已知 thread;失败时不推进游标。"""
    threads = threads if threads is not None else {}
    start_dt = _parse_time(start)
    round_dt = _parse_time(round_started)
    if start_dt is None or round_dt is None:
        raise ValueError("非法 IM 游标时间")
    latest = start_dt
    saw_current = False

    def consume(raw):
        nonlocal latest, saw_current
        created = _parse_time(raw.get("create_time"))
        if created is None or created < start_dt:
            return
        saw_current = True
        latest = max(latest, created)
        event = normalize_message(raw, bot_ids)
        if event is None:
            return
        event = dict(event)
        event.pop("_create_time")
        event["chat_id"] = chat_id
        post(event)

    page_token = None
    while True:
        page = reader.list_messages(chat_id, start, page_token)
        for raw in page.get("messages", page.get("items", [])) or []:
            if raw.get("thread_id") and raw.get("message_id"):
                threads[raw["thread_id"]] = raw["message_id"]
            consume(raw)
        if not page.get("has_more"):
            break
        page_token = page.get("page_token")
        if not page_token:
            break

    for thread_id, root_msg_id in sorted(threads.items()):
        page_token = None
        while True:
            page = reader.list_thread_messages(thread_id, page_token)
            for raw in page.get("messages", page.get("items", [])) or []:
                raw = dict(raw)
                if not raw.get("root_id"):
                    raw["root_id"] = root_msg_id
                consume(raw)
            if not page.get("has_more"):
                break
            page_token = page.get("page_token")
            if not page_token:
                break

    if saw_current:
        overlap = max(start_dt, latest - timedelta(seconds=2))
        return overlap.isoformat()
    return max(start_dt, round_dt - timedelta(seconds=2)).isoformat()


def watch_loop(reader, post, *, chat_id, bot_ids, cursor_store,
               now=lambda: datetime.now(timezone.utc).isoformat(),
               interval=2.0, stop=None, max_rounds=None) -> None:
    stop = stop or threading.Event()
    cursor = cursor_store.load(now())
    threads = cursor_store.load_threads()
    backoff = 1.0
    while not stop.is_set():
        try:
            next_cursor = sync_once(
                reader, post, chat_id=chat_id, start=cursor,
                bot_ids=bot_ids, round_started=now(), threads=threads,
            )
            cursor_store.save(next_cursor, threads)
            cursor = next_cursor
            wait = interval
            backoff = 1.0
        except Exception:  # noqa: BLE001
            logger.exception("IM 轮询失败;保留游标,%.1fs 后重试", backoff)
            wait = backoff
            backoff = min(backoff * 2, 300.0)
        if max_rounds is not None and max_rounds() <= 0:
            break
        stop.wait(wait)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-id", default=os.environ.get("SA_CONTROL_CHAT_ID"))
    parser.add_argument("--bot-id", action="append", default=[])
    parser.add_argument("--url", default=os.environ.get(
        "SA_COORDINATOR_URL", "http://127.0.0.1:8787"))
    parser.add_argument("--cursor-path", default=os.environ.get(
        "SA_IM_CURSOR_PATH", "registry/im_watcher_cursor.json"))
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args(argv)
    if not args.chat_id or not args.bot_id:
        print("错误:必须提供 --chat-id 与至少一个 --bot-id", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO)
    watch_loop(
        LarkCliMessageReader(), HttpInbox(args.url), chat_id=args.chat_id,
        bot_ids=tuple(args.bot_id), cursor_store=CursorStore(args.cursor_path),
        interval=args.interval,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

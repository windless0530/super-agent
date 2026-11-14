#!/usr/bin/env python3
"""评论 watcher:文档评论事件不可用时的轮询降级(P0 探针失败才启用)。

依据:落地方案 v1.2「文档评论回路」(D5)与 P0 第 8 项:
- 完整处理评论分页与 reply_list 分页,读取 reaction 与 is_solved;
- 以 (reply_id, 内容+update_time hash) 去重,重复评论不重复唤醒;
- 过滤责任 bot 自己的回复,防止自回复死循环;
- 活动/空闲分频 + 失败退避,单轮异常不退出。
"""

import hashlib
import json
import logging
import os
import shlex
import subprocess
import threading
from typing import Callable, Protocol, runtime_checkable

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.coordinator import outbox  # noqa: E402
from services.coordinator.registry import get_task  # noqa: E402

logger = logging.getLogger("comment_watcher")

DEFAULT_LIST_CMD = (
    "lark-cli drive +list-comments --token {doc_token} --type docx "
    "--solved-status all --need-reaction --as bot {page_token_arg}"
)
DEFAULT_GET_CMD = (
    "lark-cli drive +list-replies --token {doc_token} --type docx "
    "--comment-id {comment_id} --need-reaction --as bot {page_token_arg}"
)


def dedup_key(reply_id: str, content: str, update_time: str) -> tuple[str, str]:
    """(reply_id, sha256(content+update_time) 前 16 位)。"""
    h = hashlib.sha256((content + update_time).encode("utf-8")).hexdigest()[:16]
    return reply_id, h


@runtime_checkable
class CommentReader(Protocol):
    def list_comments(self, doc_token: str, page_token: str | None = None) -> dict: ...

    def get_comment(self, doc_token: str, comment_id: str,
                    page_token: str | None = None) -> dict: ...


def _page_token_arg(page_token: str | None) -> str:
    """渲染为 `--page-token xxx` 或空串(空串元素稍后过滤)。"""
    return f"--page-token {shlex.quote(page_token)}" if page_token else ""


def _content_text(content) -> str:
    """Drive 实际返回 content.elements;统一成通知用的纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""
    texts = []
    for element in content.get("elements", []):
        if not isinstance(element, dict):
            continue
        text_run = element.get("text_run")
        if isinstance(text_run, dict):
            texts.append(str(text_run.get("text", "")))
        elif element.get("type") == "text":
            texts.append(str(element.get("text", "")))
    return "".join(texts)


def _normalize_reactions(reply: dict) -> list[dict]:
    """聚合 reaction 展开成用户级记录;count=0 是删除残留,忽略。"""
    out = []
    for reaction in reply.get("reactions", []) or []:
        if int(reaction.get("count", 0)) <= 0:
            continue
        for user_id in reaction.get("ahead_users", []) or []:
            out.append({
                "emoji_type": reaction.get("reaction_key", reaction.get("emoji_type")),
                "user_id": user_id,
                "reply_id": reply.get("reply_id"),
            })
    return out


def _normalize_reply(reply: dict) -> dict:
    out = dict(reply)
    out["content"] = _content_text(reply.get("content"))
    out["update_time"] = str(reply.get("update_time", ""))
    return out


def _normalize_comment(item: dict) -> dict:
    """根回复即评论正文;提到 item 层,避免同步时根评论为空。"""
    out = dict(item)
    replies = (item.get("reply_list") or {}).get("replies", []) or []
    root = replies[0] if replies else {}
    out["content"] = _content_text(root.get("content", item.get("content", "")))
    out["user_id"] = root.get("user_id", item.get("user_id", ""))
    out["update_time"] = str(root.get("update_time", item.get("update_time", "")))
    out["reactions"] = _normalize_reactions(root)
    return out


class LarkCliCommentReader:
    """经 lark-cli 读取评论;命令模板可用环境变量覆盖(P0 现场校准)。

    分页:list/get 均带 {page_token_arg} 占位,渲染为 --page-token <t>
    或空;调用方按返回的 has_more/page_token 逐页展开。
    """

    def _run(self, template_env: str, default: str, **kw) -> dict:
        template = os.environ.get(template_env, default)
        argv = [a for a in shlex.split(template.format(**kw)) if a]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"评论读取失败: {proc.stderr[:200]}")
        return json.loads(proc.stdout)

    def list_comments(self, doc_token: str, page_token: str | None = None) -> dict:
        raw = self._run(
            "SA_COMMENT_LIST_CMD", DEFAULT_LIST_CMD,
            doc_token=shlex.quote(doc_token),
            page_token_arg=_page_token_arg(page_token),
        )
        data = raw.get("data", raw)
        return {**raw, "data": {
            **data, "items": [_normalize_comment(i) for i in data.get("items", [])]
        }}

    def get_comment(self, doc_token: str, comment_id: str,
                    page_token: str | None = None) -> dict:
        raw = self._run(
            "SA_COMMENT_GET_CMD", DEFAULT_GET_CMD,
            doc_token=shlex.quote(doc_token),
            comment_id=shlex.quote(comment_id),
            page_token_arg=_page_token_arg(page_token),
        )
        data = raw.get("data", raw)
        items = data.get("items", [])
        # 只有第一页 items[0] 是根回复;根评论已由 list_comments 处理。
        replies = items[1:] if page_token is None else items
        reactions = []
        for reply in items:
            reactions.extend(_normalize_reactions(reply))
        return {
            "replies": [_normalize_reply(r) for r in replies],
            "reactions": reactions,
            "has_more": data.get("has_more", False),
            "page_token": data.get("page_token"),
        }


def _seen(conn, key: tuple[str, str, str]) -> bool:
    row = conn.execute(
        "SELECT 1 FROM comment_inbox WHERE reply_id=? AND content_hash=? AND kind=?",
        key,
    ).fetchone()
    return row is not None


def _record(conn, key: tuple[str, str, str], doc_token: str, taskid: str, payload: dict):
    conn.execute(
        "INSERT OR IGNORE INTO comment_inbox"
        " (reply_id, content_hash, kind, taskid, doc_token, payload, first_seen_at)"
        " VALUES (?,?,?,?,?,?,datetime('now'))",
        (*key, taskid, doc_token, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()


def _emit_notify(conn, taskid: str, text: str) -> None:
    task = get_task(conn, taskid)
    if task is None or not task.group_chat_id or not task.topic_root_msg_id:
        logger.warning("任务 %s 无话题锚点,notify 未入队", taskid)
        return
    outbox.enqueue(
        conn, taskid=taskid, kind="notify",
        payload={
            "chat_id": task.group_chat_id,
            "root": task.topic_root_msg_id,
            "text": text,
            "mentions": [task.active_role] if task.active_role else [],
        },
    )


def _walk_comments(reader: CommentReader, doc_token: str):
    """展开评论分页与每条评论的 reply 分页;返回 [(comment, replies, meta)]。"""
    results = []
    page_token = None
    while True:
        page = reader.list_comments(doc_token, page_token)
        data = page.get("data", page)
        for item in data.get("items", []):
            replies, meta = _all_replies(reader, doc_token, item)
            results.append((item, replies, meta))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    return results


def _all_replies(reader: CommentReader, doc_token: str, item: dict):
    """展开评论的 reply_list 分页;合并卡片与回复上的 reaction/is_solved。"""
    comment_id = item["comment_id"]
    replies = []
    meta = {
        "is_solved": item.get("is_solved"),
        "reactions": list(item.get("reactions", []) or []),
    }
    page_token = None
    while True:
        full = reader.get_comment(doc_token, comment_id, page_token)
        replies.extend(full.get("replies", []))
        if meta["is_solved"] is None:
            meta["is_solved"] = full.get("is_solved")
        meta["reactions"].extend(full.get("reactions", []) or [])
        if not full.get("has_more"):
            break
        page_token = full.get("page_token")
        if not page_token:
            break
    return replies, meta


def sync_doc(
    conn,
    reader: CommentReader,
    *,
    doc_token: str,
    taskid: str,
    bot_ids: tuple[str, ...],
) -> int:
    """同步一篇文档的评论;返回新增 notify 条数(去重后用户侧新内容)。"""
    task = get_task(conn, taskid)
    if task is None or not task.group_chat_id or not task.topic_root_msg_id:
        # 不消费评论:锚点补齐后下轮仍能通知,避免 _record 后永久去重吞掉。
        logger.warning("任务 %s 无话题锚点,本轮评论暂不同步", taskid)
        return 0
    notified = 0
    for item, replies, meta in _walk_comments(reader, doc_token):
        comment_id = item["comment_id"]
        author = item.get("user_id", "")
        content = item.get("content", "")
        update_time = item.get("update_time", "")
        # 评论本体(用户发出的才算新输入)
        if author not in bot_ids:
            key = (*dedup_key(comment_id, content, update_time), "comment")
            if not _seen(conn, key):
                _record(conn, key, doc_token, taskid,
                        {"user_id": author, "content": content})
                _emit_notify(conn, taskid, f"文档评论({author}): {content}")
                notified += 1
        # 回复(完整分页展开;过滤 bot 自回复,防死循环)
        for reply in replies:
            if reply.get("user_id", "") in bot_ids:
                continue
            key = (*dedup_key(reply["reply_id"], reply.get("content", ""),
                              reply.get("update_time", "")), "reply")
            if not _seen(conn, key):
                _record(conn, key, doc_token, taskid,
                        {"user_id": reply.get("user_id"), "content": reply.get("content")})
                _emit_notify(conn, taskid,
                             f"评论回复({reply.get('user_id')}): {reply.get('content')}")
                notified += 1
        # reaction 单独记录,不产生 notify
        for reaction in meta.get("reactions", []):
            if str(reaction.get("user_id", "")) in bot_ids:
                continue
            rkey = (*dedup_key(
                reaction.get("reply_id") or comment_id,
                f"{reaction.get('user_id')}:{reaction.get('emoji_type')}",
                str(reaction.get("update_time", update_time))), "reaction")
            if not _seen(conn, rkey):
                _record(conn, rkey, doc_token, taskid, reaction)
        # is_solved 单独记录
        solved = meta.get("is_solved")
        if solved is not None:
            skey = (*dedup_key(comment_id, str(solved), update_time), "solved")
            if not _seen(conn, skey):
                _record(conn, skey, doc_token, taskid, {"is_solved": solved})
    return notified


def watch_loop(
    conn,
    reader: CommentReader,
    docs: Callable[[], list[dict]],
    *,
    active_interval: float = 3.0,
    idle_interval: float = 60.0,
    stop: threading.Event | None = None,
    max_rounds: Callable[[], int] | None = None,
) -> None:
    """活动/空闲分频轮询骨架:有非 done 任务用 active_interval,否则 idle_interval。

    docs() 返回 [{"doc_token","taskid","bot_ids"}];单轮异常退避后继续。
    """
    stop = stop or threading.Event()
    backoff = 1.0
    while not stop.is_set():
        try:
            targets = docs()
            for d in targets:
                sync_doc(conn, reader, **d)
            backoff = 1.0
            interval = active_interval if targets else idle_interval
        except Exception:  # noqa: BLE001
            logger.exception("评论轮询单轮异常(退避 %.1fs)", backoff)
            interval = backoff
            backoff = min(backoff * 2, 300.0)
        if max_rounds is not None and max_rounds() <= 0:
            break
        stop.wait(interval)


def build_docs_fn(conn, bot_ids: tuple[str, ...]):
    """构造 docs() 目标列表:非 done 且带文档链接的任务 × 其文档。"""

    def docs():
        rows = conn.execute(
            "SELECT t.taskid, t.doc_links FROM tasks t"
            " WHERE t.status != 'done' AND t.doc_links != '[]'"
        ).fetchall()
        out = []
        for r in rows:
            for link in json.loads(r[1]):
                out.append({"doc_token": link.rsplit("/", 1)[-1],
                            "taskid": r[0], "bot_ids": bot_ids})
        return out

    return docs


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="registry/tasks.db")
    parser.add_argument("--bot-id", action="append", default=[],
                        help="责任 bot 的 open_id/user_id,可重复;"
                             "缺失则自回复过滤失效,程序拒绝启动")
    parser.add_argument("--active-interval", type=float, default=3.0)
    parser.add_argument("--idle-interval", type=float, default=60.0)
    args = parser.parse_args(argv)
    if not args.bot_id:
        print("错误: 至少一个 --bot-id(否则 bot 会被自己的回复唤醒,死循环)",
              file=sys.stderr)
        return 2

    from services.coordinator import db as db_mod

    conn = db_mod.connect(args.db)
    db_mod.init_schema(conn)
    docs = build_docs_fn(conn, tuple(args.bot_id))
    logging.basicConfig(level=logging.INFO)
    watch_loop(conn, LarkCliCommentReader(), docs,
               active_interval=args.active_interval,
               idle_interval=args.idle_interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

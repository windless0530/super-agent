"""comment_watcher 测试:分页展开、去重、自回复过滤、reaction/is_solved 记录。"""

import json
from pathlib import Path

import pytest

from services.coordinator import db
from services.coordinator.coordinator import Coordinator
from tools.comment_watcher import dedup_key, sync_doc
from tests.test_coordinator import FakeSender

FIXTURES = Path(__file__).parent / "fixtures"
BOT_IDS = ("ou_bot_design",)


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeReader:
    """按 fixture 提供 list_comments / get_comment。"""

    def __init__(self):
        self.pages = {"": _load("comments_page1.json"), "cp1": _load("comments_page2.json")}
        self.reply_pages = {"com_1": {"rp1": _load("comments_reply_page.json")}}

    def list_comments(self, doc_token, page_token=None):
        return self.pages.get(page_token or "", {"data": {"items": [], "has_more": False}})

    def get_comment(self, doc_token, comment_id, page_token=None):
        if comment_id == "com_1":
            return {
                "replies": [
                    {"reply_id": "rep_1", "user_id": "ou_bot_design",
                     "content": "收到,已补充", "update_time": "2026-11-14T01:00:00"},
                    {"reply_id": "rep_2", "user_id": "ou_elio",
                     "content": "重连次数上限?", "update_time": "2026-11-14T02:00:00"},
                    {"reply_id": "rep_3", "user_id": "ou_bot_design",
                     "content": "上限 5 次", "update_time": "2026-11-14T03:00:00"},
                ],
                "is_solved": False,
                "reactions": [],
            }
        return {"replies": [], "is_solved": True,
                "reactions": [{"emoji_type": "OK", "user_id": "ou_elio"}]}


@pytest.fixture()
def env(tmp_path):
    conn = db.connect(str(tmp_path / "tasks.db"))
    db.init_schema(conn)
    db.seed_task_types(conn)
    conn.execute("UPDATE task_types SET group_chat_id='oc_g' WHERE type_code='RD'")
    conn.commit()
    co = Coordinator(conn, FakeSender())
    r = co.ingest_task(event_id="ev_1", message_id="om_1", text="t",
                       task_type="RD", title="标题")
    co.dispatch(r.taskid, seed_text="s", mentions=("design",))
    co.process_due_outbox()
    return conn, r.taskid


def _notify_texts(conn):
    rows = conn.execute(
        "SELECT payload FROM outbox WHERE kind='notify'"
    ).fetchall()
    return [json.loads(r[0])["text"] for r in rows]


def test_dedup_key_shape():
    k1 = dedup_key("rep_1", "内容", "2026-11-14T00:00:00")
    k2 = dedup_key("rep_1", "内容", "2026-11-14T00:00:01")
    k3 = dedup_key("rep_1", "内容", "2026-11-14T00:00:00")
    assert k1[0] == "rep_1"
    assert k1[1] != k2[1]  # update_time 变 → hash 变
    assert k1 == k3


def test_sync_expands_all_pages_and_filters_bot_replies(env):
    conn, taskid = env
    n = sync_doc(conn, FakeReader(), doc_token="docx_1", taskid=taskid, bot_ids=BOT_IDS)
    # 用户新内容:com_1 评论本体 + rep_2 用户二次回复 + com_2 评论本体 = 3 条 notify
    assert n == 3
    texts = _notify_texts(conn)
    assert any("这里要补充超时分析" in t for t in texts)
    assert any("重连次数上限?" in t for t in texts)
    assert any("总体方向 Get" in t for t in texts)
    # bot 自回复不进 notify
    assert not any("收到,已补充" in t for t in texts)
    assert not any("上限 5 次" in t for t in texts)


def test_second_sync_no_new(env):
    conn, taskid = env
    reader = FakeReader()
    sync_doc(conn, reader, doc_token="docx_1", taskid=taskid, bot_ids=BOT_IDS)
    assert sync_doc(conn, reader, doc_token="docx_1", taskid=taskid, bot_ids=BOT_IDS) == 0
    assert len(_notify_texts(conn)) == 3


def test_content_update_generates_new_record(env):
    conn, taskid = env
    reader = FakeReader()
    sync_doc(conn, reader, doc_token="docx_1", taskid=taskid, bot_ids=BOT_IDS)
    # 用户编辑了二次回复(update_time 与内容都变)
    reader.get_comment = lambda doc, cid, page_token=None: {
        "replies": [
            {"reply_id": "rep_1", "user_id": "ou_bot_design",
             "content": "收到,已补充", "update_time": "2026-11-14T01:00:00"},
            {"reply_id": "rep_2", "user_id": "ou_elio",
             "content": "重连次数上限?上限可配?", "update_time": "2026-11-14T05:00:00"},
            {"reply_id": "rep_3", "user_id": "ou_bot_design",
             "content": "上限 5 次", "update_time": "2026-11-14T03:00:00"},
        ],
        "is_solved": False, "reactions": [],
    } if cid == "com_1" else {"replies": [], "is_solved": True,
                              "reactions": [{"emoji_type": "OK", "user_id": "ou_elio"}]}
    assert sync_doc(conn, reader, doc_token="docx_1", taskid=taskid, bot_ids=BOT_IDS) == 1


def test_reaction_and_solved_recorded_without_notify(env):
    conn, taskid = env
    sync_doc(conn, FakeReader(), doc_token="docx_1", taskid=taskid, bot_ids=BOT_IDS)
    kinds = {r[0] for r in conn.execute(
        "SELECT DISTINCT kind FROM comment_inbox").fetchall()}
    assert "reaction" in kinds
    assert "solved" in kinds
    # reaction/solved 不产生额外 notify(仍为 3)
    assert len(_notify_texts(conn)) == 3


def test_notify_payload_targets_topic(env):
    conn, taskid = env
    sync_doc(conn, FakeReader(), doc_token="docx_1", taskid=taskid, bot_ids=BOT_IDS)
    row = conn.execute(
        "SELECT payload FROM outbox WHERE kind='notify' LIMIT 1"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["chat_id"] == "oc_g"      # worker 群
    assert payload["root"]                   # 话题种子消息 id


def test_watch_loop_single_round_smoke(env):
    from tools.comment_watcher import watch_loop
    conn, taskid = env
    reader = FakeReader()
    rounds = iter([1])  # 只跑一轮
    import threading
    stop = threading.Event()

    def docs():
        return [{"doc_token": "docx_1", "taskid": taskid, "bot_ids": BOT_IDS}]

    watch_loop(conn, reader, docs, active_interval=0, idle_interval=0,
               stop=stop, max_rounds=lambda: next(rounds, 0))
    assert len(_notify_texts(conn)) == 3


class TestPagination:
    """I2:list 分页游标必须传给 reader;reply_list 分页必须展开。"""

    def test_list_pagination_cursor_forwarded(self, env):
        conn, taskid = env
        seen_tokens = []

        class PagedReader(FakeReader):
            def list_comments(self, doc_token, page_token=None):
                seen_tokens.append(page_token)
                return self.pages.get(page_token or "",
                                      {"data": {"items": [], "has_more": False}})

        reader = PagedReader()
        sync_doc(conn, reader, doc_token="docx_1", taskid=taskid, bot_ids=BOT_IDS)
        # 第一页(page_token=None)→ 第二页(cp1)→ 终止,不死循环
        assert seen_tokens == [None, "cp1"]
        # 两页的评论都进 notify
        texts = _notify_texts(conn)
        assert any("这里要补充超时分析" in t for t in texts)   # page1
        assert any("总体方向 Get" in t for t in texts)          # page2

    def test_reply_pagination_expanded(self, env):
        conn, taskid = env
        calls = []

        class ReplyPagedReader(FakeReader):
            def get_comment(self, doc_token, comment_id, page_token=None):
                calls.append((comment_id, page_token))
                if comment_id == "com_1":
                    if page_token is None:
                        return {"replies": [
                            {"reply_id": "rep_1", "user_id": "ou_bot_design",
                             "content": "收到", "update_time": "t1"},
                            {"reply_id": "rep_2", "user_id": "ou_elio",
                             "content": "第一页用户回复", "update_time": "t2"},
                        ], "has_more": True, "page_token": "rp1"}
                    return {"replies": [
                        {"reply_id": "rep_3", "user_id": "ou_elio",
                         "content": "第二页用户回复", "update_time": "t3"},
                    ], "has_more": False}
                return {"replies": [], "is_solved": True, "reactions": []}

        sync_doc(conn, ReplyPagedReader(), doc_token="docx_1",
                 taskid=taskid, bot_ids=BOT_IDS)
        # com_1 的 reply 分页被展开(两页都取)
        assert ("com_1", None) in calls and ("com_1", "rp1") in calls
        texts = _notify_texts(conn)
        assert any("第一页用户回复" in t for t in texts)
        assert any("第二页用户回复" in t for t in texts)  # 第 2 页不再漏检

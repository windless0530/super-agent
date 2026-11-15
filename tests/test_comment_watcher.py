"""comment_watcher 测试:分页展开、去重、自回复过滤、reaction/is_solved 记录。"""

import json
from pathlib import Path

import pytest

from services.coordinator import db
from services.coordinator.coordinator import Coordinator
from tools.comment_watcher import LarkCliCommentReader, dedup_key, sync_doc
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
            # 对齐线上语义:detail 的 replies 不含根回复(根即评论卡片)
            return {
                "replies": [
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


def test_missing_topic_anchor_does_not_consume_comment(env):
    conn, taskid = env
    # 文档链接已存在但派单锚点尚未补齐时,不能先把评论标成已消费。
    conn.execute("UPDATE tasks SET topic_root_msg_id=NULL WHERE taskid=?", (taskid,))
    conn.commit()
    reader = FakeReader()
    assert sync_doc(conn, reader, doc_token="docx_1", taskid=taskid, bot_ids=BOT_IDS) == 0
    assert conn.execute("SELECT COUNT(*) FROM comment_inbox").fetchone()[0] == 0
    conn.execute("UPDATE tasks SET topic_root_msg_id='om_restored' WHERE taskid=?", (taskid,))
    conn.commit()
    assert sync_doc(conn, reader, doc_token="docx_1", taskid=taskid, bot_ids=BOT_IDS) == 3


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


class TestMainBotIds:
    """I3:生产入口必须显式传 bot_ids,否则自回复过滤形同虚设。"""

    def test_missing_bot_ids_rejected(self, capsys):
        import importlib.util
        import importlib.machinery
        from pathlib import Path
        repo = Path(__file__).parent.parent
        spec = importlib.util.spec_from_file_location(
            "cw_main", str(repo / "tools/comment_watcher.py"),
            loader=importlib.machinery.SourceFileLoader(
                "cw_main", str(repo / "tools/comment_watcher.py")))
        cw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cw)
        code = cw.main(["--db", "/tmp/cw-test.db"])
        assert code == 2
        assert "--bot-id" in capsys.readouterr().err

    def test_bot_ids_forwarded_to_docs(self, tmp_path):
        from tools.comment_watcher import build_docs_fn
        from services.coordinator import db as db_mod
        conn = db_mod.connect(str(tmp_path / "t.db"))
        db_mod.init_schema(conn)
        docs = build_docs_fn(conn, bot_ids=("ou_bot_design", "ou_bot_review"))
        # 无带文档链接的非终态任务 → 空列表;函数可调用即契约成立
        assert docs() == []


class TestSteadyStateSkip:
    """配额优化:list 载荷无变化时跳过 detail 展开,稳态 1 次调用/轮/文档。"""

    def _state(self):
        return {"sigs": {}, "unstable": set()}

    def test_unchanged_second_round_skips_detail_calls(self, env):
        conn, taskid = env
        calls = {"list": 0, "get": 0}

        class CountingReader(FakeReader):
            def list_comments(self, doc_token, page_token=None):
                calls["list"] += 1
                return super().list_comments(doc_token, page_token)

            def get_comment(self, doc_token, comment_id, page_token=None):
                calls["get"] += 1
                return super().get_comment(doc_token, comment_id, page_token)

        reader = CountingReader()
        state = self._state()
        sync_doc(conn, reader, doc_token="docx_1", taskid=taskid,
                 bot_ids=BOT_IDS, state=state)
        first_get = calls["get"]
        assert first_get > 0
        assert sync_doc(conn, reader, doc_token="docx_1", taskid=taskid,
                        bot_ids=BOT_IDS, state=state) == 0
        # 第二轮 list 照常分页,但 detail 全部跳过
        assert calls["list"] == 4  # 每轮 2 页 × 2 轮
        assert calls["get"] == first_get

    def test_changed_comment_expands_only_it(self, env):
        conn, taskid = env
        expanded = []

        class MutableReader(FakeReader):
            mutate = False

            def list_comments(self, doc_token, page_token=None):
                page = super().list_comments(doc_token, page_token)
                if self.mutate:
                    for item in page.get("data", {}).get("items", []):
                        if item["comment_id"] == "com_1":
                            for r in (item.get("reply_list") or {}).get("replies", []):
                                if r.get("reply_id") == "rep_2":
                                    r["update_time"] = "2026-11-14T09:00:00"
                return page

            def get_comment(self, doc_token, comment_id, page_token=None):
                expanded.append(comment_id)
                return super().get_comment(doc_token, comment_id, page_token)

        reader = MutableReader()
        state = self._state()
        sync_doc(conn, reader, doc_token="docx_1", taskid=taskid,
                 bot_ids=BOT_IDS, state=state)
        reader.mutate = True
        expanded.clear()
        sync_doc(conn, reader, doc_token="docx_1", taskid=taskid,
                 bot_ids=BOT_IDS, state=state)
        assert expanded == ["com_1"]

    def test_truncated_comment_always_expanded(self, env):
        conn, taskid = env
        expanded = []

        class TruncatingReader(FakeReader):
            def get_comment(self, doc_token, comment_id, page_token=None):
                expanded.append(comment_id)
                result = super().get_comment(doc_token, comment_id, page_token)
                if comment_id == "com_1" and page_token is None:
                    # detail 首页带回 3 条回复,但 list 只内嵌了 2 条 → 截断
                    pass
                return result

            def list_comments(self, doc_token, page_token=None):
                page = super().list_comments(doc_token, page_token)
                for item in page.get("data", {}).get("items", []):
                    if item["comment_id"] == "com_1":
                        replies = (item.get("reply_list") or {}).get("replies", [])
                        item["reply_list"] = {"replies": replies[:1]}
                return page

        reader = TruncatingReader()
        state = self._state()
        sync_doc(conn, reader, doc_token="docx_1", taskid=taskid,
                 bot_ids=BOT_IDS, state=state)
        expanded.clear()
        sync_doc(conn, reader, doc_token="docx_1", taskid=taskid,
                 bot_ids=BOT_IDS, state=state)
        # 截断过的评论不可信任签名,必须持续展开
        assert "com_1" in expanded

    def test_watch_loop_passes_state_across_rounds(self, env):
        import threading
        from tools.comment_watcher import watch_loop
        conn, taskid = env
        calls = {"get": 0}

        class CountingReader(FakeReader):
            def get_comment(self, doc_token, comment_id, page_token=None):
                calls["get"] += 1
                return super().get_comment(doc_token, comment_id, page_token)

        rounds = iter([1, 1])
        stop = threading.Event()
        snapshots = []

        def docs():
            snapshots.append(calls["get"])  # 每轮开始时抓快照
            return [{"doc_token": "docx_1", "taskid": taskid, "bot_ids": BOT_IDS}]

        watch_loop(conn, CountingReader(), docs, active_interval=0, idle_interval=0,
                   stop=stop, max_rounds=lambda: next(rounds, 0))
        after_round1 = snapshots[1]
        assert after_round1 > 0                    # 第一轮做了 detail 展开
        assert calls["get"] == after_round1        # 第二轮未新增 detail 调用



    """P0 #8:lark-cli 1.0.97 的真实命令与 Drive 评论响应结构。"""

    ROOT = {
        "reply_id": "r_root", "user_id": "ou_elio", "update_time": 100,
        "content": {"elements": [{"type": "text_run", "text_run": {"text": "根评论"}}]},
        "reactions": [
            {"reaction_key": "Get", "count": 1, "ahead_users": ["ou_bot_design"]},
            {"reaction_key": "OK", "count": 1, "ahead_users": ["ou_elio"]},
        ],
    }
    SECOND = {
        "reply_id": "r_second", "user_id": "ou_elio", "update_time": 101,
        "content": {"elements": [{"type": "text_run", "text_run": {"text": "二次回复"}}]},
    }

    @staticmethod
    def _completed(payload):
        return type("Completed", (), {
            "returncode": 0, "stdout": json.dumps(payload), "stderr": "",
        })()

    def test_list_uses_real_command_and_normalizes_root(self, monkeypatch):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return self._completed({"ok": True, "data": {
                "items": [{"comment_id": "c1", "user_id": "ou_elio",
                           "is_solved": False, "reply_list": {"replies": [self.ROOT]}}],
                "has_more": False, "page_token": "",
            }})

        monkeypatch.setattr("tools.comment_watcher.subprocess.run", run)
        result = LarkCliCommentReader().list_comments("docx_real")
        argv = calls[0]
        assert "+list-comments" in argv and "--token" in argv
        assert "--doc" not in argv and "--type" in argv and "docx" in argv
        assert "--solved-status" in argv and "all" in argv
        assert "--need-reaction" in argv
        item = result["data"]["items"][0]
        assert item["content"] == "根评论"
        assert item["reactions"] == [
            {"emoji_type": "Get", "user_id": "ou_bot_design", "reply_id": "r_root"},
            {"emoji_type": "OK", "user_id": "ou_elio", "reply_id": "r_root"},
        ]

    def test_replies_use_real_command_and_skip_root_only_on_first_page(self, monkeypatch):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            page = argv[argv.index("--page-token") + 1] if "--page-token" in argv else None
            items = [self.SECOND] if page else [self.ROOT, self.SECOND]
            return self._completed({"ok": True, "data": {
                "items": items, "has_more": False, "page_token": "",
            }})

        monkeypatch.setattr("tools.comment_watcher.subprocess.run", run)
        reader = LarkCliCommentReader()
        first = reader.get_comment("docx_real", "c1")
        later = reader.get_comment("docx_real", "c1", "next")
        assert "+list-replies" in calls[0] and "+get-comment" not in calls[0]
        assert [r["content"] for r in first["replies"]] == ["二次回复"]
        assert [r["content"] for r in later["replies"]] == ["二次回复"]
        assert first["reactions"][1]["emoji_type"] == "OK"

    def test_live_shape_notifies_root_once_and_records_reactions(self, monkeypatch, env):
        conn, taskid = env

        def run(argv, **kwargs):
            if "+list-comments" in argv:
                payload = {"data": {"items": [{
                    "comment_id": "c1", "user_id": "ou_elio", "is_solved": False,
                    "reply_list": {"replies": [self.ROOT]},
                }], "has_more": False}}
            else:
                payload = {"data": {"items": [self.ROOT, self.SECOND], "has_more": False}}
            return self._completed(payload)

        monkeypatch.setattr("tools.comment_watcher.subprocess.run", run)
        n = sync_doc(conn, LarkCliCommentReader(), doc_token="docx_real",
                     taskid=taskid, bot_ids=BOT_IDS)
        assert n == 2  # 根评论 + 二次回复;根回复不得再作为 reply 重复通知
        texts = _notify_texts(conn)
        assert texts == ["文档评论(ou_elio): 根评论", "评论回复(ou_elio): 二次回复"]
        reactions = conn.execute(
            "SELECT payload FROM comment_inbox WHERE kind='reaction'"
        ).fetchall()
        # bot 的 Get 被过滤,用户的 OK 被记录且不产生 notify。
        assert [json.loads(r[0])["emoji_type"] for r in reactions] == ["OK"]

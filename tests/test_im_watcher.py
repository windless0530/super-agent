"""im_watcher:消息归一化、分页、持久游标、失败重投与退避。"""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


def load_module():
    import tools.im_watcher as iw
    return iw


def iso_second(offset=0):
    return datetime.fromtimestamp(1_800_000_000 + offset, timezone.utc).isoformat()


def raw_message(message_id="om_1", *, text="调研 X", sender="ou_user",
                create_time=None, root_id=None):
    return {
        "message_id": message_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
        "create_time": create_time or iso_second(10),
        "sender": {"sender_id": {"open_id": sender}, "sender_type": "user"},
        "root_id": root_id,
        "deleted": False,
    }


def test_normalize_text_dict_and_nested_sender():
    iw = load_module()
    raw = raw_message(root_id="om_root")
    raw["content"] = {"text": "开发插件"}
    event = iw.normalize_message(raw, ("ou_router",))
    assert event == {
        "message_id": "om_1", "chat_id": "", "root_msg_id": "om_root",
        "text": "开发插件", "sender_open_id": "ou_user",
        "_create_time": iso_second(10),
    }


def test_normalize_flat_sender_and_root_fallback():
    iw = load_module()
    raw = raw_message()
    raw["sender"] = {"id": "ou_flat", "sender_type": "user"}
    event = iw.normalize_message(raw, ())
    assert event["sender_open_id"] == "ou_flat"
    assert event["root_msg_id"] == "om_1"


@pytest.mark.parametrize("mutate", [
    lambda m: m["sender"].update(sender_type="bot"),
    lambda m: m["sender"]["sender_id"].update(open_id="ou_router"),
    lambda m: m.update(deleted=True),
    lambda m: m.update(msg_type="image"),
    lambda m: m.update(content="not-json"),
])
def test_filtered_messages_never_reach_inbox(mutate):
    iw = load_module()
    raw = raw_message()
    mutate(raw)
    assert iw.normalize_message(raw, ("ou_router",)) is None


def test_lark_reader_command_and_success_envelope(monkeypatch):
    iw = load_module()
    captured = {}

    def run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "ok": True,
            "data": {"messages": [raw_message()], "has_more": False},
        }), stderr="")

    monkeypatch.setattr(iw.subprocess, "run", run)
    page = iw.LarkCliMessageReader().list_messages(
        "oc_control", iso_second(), page_token="next page",
    )
    assert page["messages"][0]["message_id"] == "om_1"
    argv = captured["argv"]
    assert argv[:5] == ["lark-cli", "--profile", "sa-router", "im", "+chat-messages-list"]
    assert "--order" in argv and argv[argv.index("--order") + 1] == "asc"
    assert "--no-reactions" in argv
    assert argv[argv.index("--chat-id") + 1] == "oc_control"
    assert argv[argv.index("--start") + 1] == iso_second()
    assert argv[argv.index("--page-token") + 1] == "next page"
    assert captured["kwargs"]["timeout"] == 30


def test_lark_reader_rejects_error_envelope(monkeypatch):
    iw = load_module()
    monkeypatch.setattr(iw.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps({"ok": False, "error": {"message": "bad"}}), stderr="",
    ))
    with pytest.raises(RuntimeError, match="bad"):
        iw.LarkCliMessageReader().list_messages("oc", iso_second())


def test_cursor_store_first_start_and_atomic_restart(tmp_path):
    iw = load_module()
    path = tmp_path / "nested" / "cursor.json"
    store = iw.CursorStore(path)
    assert store.load(iso_second(100)) == iso_second(100)
    assert not path.exists()
    store.save(iso_second(200))
    assert json.loads(path.read_text())["cursor"] == iso_second(200)
    assert list(path.parent.iterdir()) == [path]
    assert store.load(iso_second(300)) == iso_second(200)


class PagedReader:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list_messages(self, chat_id, start, page_token=None):
        self.calls.append((chat_id, start, page_token))
        return self.pages[page_token]


def test_sync_expands_pages_posts_in_order_and_overlaps_two_seconds():
    iw = load_module()
    reader = PagedReader({
        None: {"messages": [raw_message("om_1", create_time=iso_second(10))],
               "has_more": True, "page_token": "p2"},
        "p2": {"messages": [raw_message("om_2", create_time=iso_second(20))],
               "has_more": False},
    })
    posted = []
    cursor = iw.sync_once(
        reader, posted.append, chat_id="oc_control", start=iso_second(),
        bot_ids=("ou_router",), round_started=iso_second(30),
    )
    assert [e["message_id"] for e in posted] == ["om_1", "om_2"]
    assert posted[0]["chat_id"] == "oc_control"
    assert "_create_time" not in posted[0]
    assert reader.calls == [("oc_control", iso_second(), None),
                            ("oc_control", iso_second(), "p2")]
    assert cursor == iso_second(18)


def test_sync_skips_pre_cursor_and_empty_round_advances():
    iw = load_module()
    reader = PagedReader({None: {
        "messages": [raw_message("om_old", create_time=iso_second(-1))],
        "has_more": False,
    }})
    posted = []
    cursor = iw.sync_once(
        reader, posted.append, chat_id="oc", start=iso_second(),
        bot_ids=(), round_started=iso_second(30),
    )
    assert posted == []
    assert cursor == iso_second(28)  # 空轮也保留 2s 重叠,容忍主机/服务端时钟偏差


def test_failed_post_does_not_advance_cursor_and_next_round_retries(tmp_path):
    iw = load_module()
    reader = PagedReader({None: {"messages": [
        raw_message("om_1", create_time=iso_second(10)),
        raw_message("om_2", create_time=iso_second(20)),
    ], "has_more": False}})
    store = iw.CursorStore(tmp_path / "cursor.json")
    store.save(iso_second())
    calls = []

    def fail_second(event):
        calls.append(event["message_id"])
        if event["message_id"] == "om_2":
            raise RuntimeError("503")

    iw.watch_loop(
        reader, fail_second, chat_id="oc", bot_ids=(), cursor_store=store,
        now=lambda: iso_second(30), interval=0,
        stop=threading.Event(), max_rounds=lambda: 0,
    )
    assert store.load(iso_second(999)) == iso_second()

    iw.watch_loop(
        reader, lambda e: calls.append(e["message_id"]), chat_id="oc", bot_ids=(),
        cursor_store=store, now=lambda: iso_second(40), interval=0,
        stop=threading.Event(), max_rounds=lambda: 0,
    )
    assert calls == ["om_1", "om_2", "om_1", "om_2"]


def test_watch_loop_backs_off_and_recovers(tmp_path):
    iw = load_module()

    class FlakyReader:
        def __init__(self):
            self.calls = 0

        def list_messages(self, chat_id, start, page_token=None):
            self.calls += 1
            if self.calls <= 2:
                raise RuntimeError("temporary")
            return {"messages": [], "has_more": False}

    class RecordingStop:
        def __init__(self):
            self.waits = []

        def is_set(self):
            return False

        def wait(self, seconds):
            self.waits.append(seconds)

    stop = RecordingStop()
    store = iw.CursorStore(tmp_path / "cursor.json")
    rounds = iter([2, 1, 0])
    iw.watch_loop(
        FlakyReader(), lambda e: None, chat_id="oc", bot_ids=(), cursor_store=store,
        now=lambda: iso_second(50), interval=2, stop=stop,
        max_rounds=lambda: next(rounds, 0),
    )
    assert stop.waits == [1.0, 2.0]
    assert store.load(iso_second()) == iso_second(50)


def test_http_inbox_non_2xx_is_failure(monkeypatch):
    import io
    iw = load_module()
    error = urllib.error.HTTPError(
        "http://x/inbox", 503, "down", {}, io.BytesIO(b'{"error":"down"}'),
    )
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(error),
    )
    with pytest.raises(RuntimeError, match="503"):
        iw.HttpInbox("http://x")({"message_id": "om_1"})


def test_main_requires_chat_and_bot_ids(capsys):
    iw = load_module()
    assert iw.main([]) == 2
    assert "--chat-id" in capsys.readouterr().err

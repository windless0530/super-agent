"""sa-coordinator CLI 与 HTTP 服务。

用法:
  python3 -m services.coordinator init-db          # 建库 + 类型种子
  python3 -m services.coordinator serve [--port N] # 常驻 HTTP + 后台推进 outbox
  python3 -m services.coordinator once             # 单次对账 + 推进(systemd timer 形态)
"""

import argparse
import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db
from .coordinator import (
    ClarifyRejected,
    Coordinator,
    InvalidClarified,
    ReportRejected,
    utcnow_iso,
)
from .registry import StaleVersion, TaskNotFound
from .state import InvalidTransition
from .reconciler import reconcile, scan_stuck
from .report import InvalidReport
from .sender import LarkCliSender

DEFAULT_DB_PATH = "registry/tasks.db"
POLL_INTERVAL_SECONDS = 5

logger = logging.getLogger("coordinator")


class LockHeld(Exception):
    """单写者锁已被存活进程持有。"""


def acquire_lock(lock_path: str) -> int:
    """O_CREAT|O_EXCL 创建锁文件;已存在且 pid 存活则拒绝启动。"""
    if os.path.exists(lock_path):
        try:
            with open(lock_path) as f:
                old_pid = int(f.read().strip() or 0)
            os.kill(old_pid, 0)  # 进程存活检查
            raise LockHeld(f"coordinator 已在运行(pid={old_pid})")
        except (ProcessLookupError, ValueError, PermissionError):
            pass  # 残留锁:占用进程已死,接管
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o644)
    os.write(fd, str(os.getpid()).encode())
    return fd


def make_handler(co: Coordinator, lock: threading.Lock | None = None):
    lock = lock or threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, body: dict):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._json(200, {"ok": True})
            elif parsed.path == "/tasks":
                status = (parse_qs(parsed.query).get("status") or [None])[0]
                with lock:
                    tasks = [
                        {"taskid": t.taskid, "status": t.status, "title": t.title,
                         "phase": t.phase, "updated_at": t.updated_at}
                        for t in _list_tasks(co, status)
                    ]
                self._json(200, {"tasks": tasks})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            if path not in ("/report", "/ingest", "/route", "/clarified"):
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                event = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as e:
                self._json(422, {"error": f"非法 JSON: {e}"})
                return
            try:
                with lock:
                    if path == "/ingest":
                        body = _handle_ingest(co, event)
                    elif path == "/route":
                        body = _handle_route(co, event)
                    elif path == "/clarified":
                        body = co.handle_clarified(event)
                    else:
                        body = co.handle_report(event)
                self._json(200, body)
            except (InvalidIngest, InvalidRoute, InvalidClarified, InvalidReport) as e:
                self._json(422, {"error": str(e)})
            except TaskNotFound as e:
                self._json(422, {"error": f"任务不存在: {e}"})
            except (ReportRejected, RouteRejected, ClarifyRejected) as e:
                self._json(409, e.body)
            except (InvalidTransition, StaleVersion) as e:
                self._json(409, {"ok": False, "reason": "state_conflict",
                                 "error": str(e)})
            except Exception as e:  # noqa: BLE001 — 兜底:服务端 bug 不得伪装成断连
                import logging
                logging.getLogger("coordinator").exception("do_POST 内部错误")
                self._json(500, {"error": f"内部错误: {e}"})

        def log_message(self, fmt, *args):  # 安静模式:交给 logging
            pass

    return Handler


class InvalidIngest(ValueError):
    """ingest 请求字段缺失或非法。"""


class InvalidRoute(ValueError):
    """route verdict 字段缺失或非法(Review Focus #2:拒绝半派单)。"""


class RouteRejected(Exception):
    """确定性拒绝(状态不可路由/群未配置);body 随异常携带。"""

    def __init__(self, body: dict):
        self.body = body
        super().__init__(str(body))


def _handle_route(co: Coordinator, event: dict) -> dict:
    """v1.2 固定动作 2/3:Router verdict → 澄清提问 或 类型回填 + 派单。

    - ambiguous → set_clarifying(thread A 提问,outbox 承担发送)
    - 完整 verdict → 校验类型注册表(显式拒绝 PENDING 占位)→
      cas 回填 type/title 并置 running → 渲染种子 → dispatch 入队
      (与 /report 同构:状态先行,outbox 兜投)
    - mentions:role_members 已配置则用 mention 令牌,否则回退角色名。
    """
    taskid = str(event.get("taskid") or "").strip()
    if not taskid:
        raise InvalidRoute("缺少 taskid")
    task = co.conn.execute("SELECT * FROM tasks WHERE taskid=?", (taskid,)).fetchone()
    if task is None:
        raise TaskNotFound(taskid)
    ambiguous = event.get("ambiguous")
    if not isinstance(ambiguous, bool):
        raise InvalidRoute("ambiguous 必须为布尔值")
    if task["status"] != "received":
        raise RouteRejected({"ok": False, "reason": "not_routeable",
                             "status": task["status"]})

    if ambiguous:
        question = str(event.get("clarify_question") or "").strip()
        if not question:
            raise InvalidRoute("ambiguous=true 时必须携带 clarify_question")
        co.set_clarifying(taskid, question)
        return {"ok": True, "taskid": taskid, "state": "clarifying"}

    task_type = str(event.get("task_type") or "").strip()
    title = str(event.get("title") or "").strip()
    if not title:
        raise InvalidRoute("缺少 title")
    if task_type == "PENDING":
        raise InvalidRoute("task_type 不能为占位符 PENDING")
    trow = co.conn.execute(
        "SELECT name, group_chat_id, roles, rounds_limit FROM task_types"
        " WHERE type_code=? AND enabled=1", (task_type,),
    ).fetchone()
    if trow is None:
        raise InvalidRoute(f"未知或未启用的 task_type: {task_type}")
    if not trow["group_chat_id"]:
        raise RouteRejected({"ok": False, "reason": "group_not_configured",
                             "type": task_type})

    import json as _json
    try:
        roles = _json.loads(trow["roles"])
    except ValueError as e:
        raise InvalidRoute(f"类型注册表 roles 损坏: {task_type} ({e})") from e
    mention_rows = {
        r["role"]: r["mention"]
        for r in co.conn.execute("SELECT role, mention FROM role_members")
    }
    mentions = tuple(mention_rows.get(role, role) for role in roles)

    from .registry import cas_update
    from .seed import render_seed
    brief_row = co.conn.execute(
        "SELECT payload FROM inbox_events WHERE taskid=? AND kind='task'"
        " ORDER BY received_at LIMIT 1", (taskid,),
    ).fetchone()
    brief = ""
    if brief_row is not None:
        try:
            brief = str((_json.loads(brief_row["payload"]) or {}).get("text") or "")
        except (ValueError, TypeError):
            brief = ""
    # CAS 与 dispatch 入队同一事务:dispatch 前崩溃不留下 running 无种子的任务
    try:
        cas_update(co.conn, taskid, task["version"],
                   type=task_type, title=title, status="running", commit=False)
        co.dispatch(taskid, seed_text=render_seed(
            taskid=taskid, title=title, type_name=trow["name"],
            roles=roles, rounds_limit=trow["rounds_limit"], brief=brief,
        ), mentions=mentions, commit=False)
        co.conn.commit()
    except Exception:
        co.conn.rollback()
        raise
    return {"ok": True, "taskid": taskid, "state": "running"}


def _handle_ingest(co: Coordinator, event: dict) -> dict:
    """v1.2 固定动作 1:消息接入(幂等)。

    类型/标题此刻未知 → PENDING 占位、received 状态;由 /route 回填并派单。
    event_id 由 message_id 确定性派生:重试同一消息只算一次。
    """
    message_id = str(event.get("message_id") or "").strip()
    text = str(event.get("text") or "").strip()
    if not message_id:
        raise InvalidIngest("缺少 message_id")
    if not text:
        raise InvalidIngest("缺少 text")
    r = co.ingest_task(
        event_id=f"msg:{message_id}",
        message_id=message_id,
        text=text,
        task_type="PENDING",
        title=text[:60],
        thread_chat_id=event.get("chat_id") or None,
        thread_root_msg_id=event.get("root_msg_id") or None,
        allow_placeholder_type=True,
    )
    return {"ok": True, "taskid": r.taskid, "duplicate": not r.new}


def _list_tasks(co: Coordinator, status: str | None):
    if status:
        rows = co.conn.execute(
            "SELECT * FROM tasks WHERE status=? ORDER BY taskid", (status,)
        ).fetchall()
        from .registry import Task
        return [Task.from_row(r) for r in rows]
    from .registry import list_non_terminal
    return list_non_terminal(co.conn)


def serve_in_thread(co: Coordinator, port: int = 8787, ready=None,
                    startup_now: str | None = None, with_watchdog: bool = True):
    """测试友好:启动对账 + 线程内起服务,ready(port) 回调通知端口。"""
    if with_watchdog:
        _startup_reconcile_and_log(co, startup_now or utcnow_iso())
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(co))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if ready:
        ready(server.server_address[1])
    return _Joinable(thread, server)


def _startup_reconcile_and_log(co: Coordinator, now: str):
    report = reconcile(co.conn, co.sender, now=now)
    for s in report.stuck:
        logger.error("启动对账:任务卡死 %s %s(%s)", s["taskid"], s["status"], s["reason"])


def _watchdog_loop(co: Coordinator, stop: threading.Event,
                   stuck_every_rounds: int = 12):
    """周期卡死巡检(每 N 轮 5s 即约 1 分钟一次),只记日志/告警。"""
    rounds = 0
    while not stop.is_set():
        stop.wait(POLL_INTERVAL_SECONDS)
        rounds += 1
        if rounds % stuck_every_rounds:
            continue
        try:
            for s in scan_stuck(co.conn, utcnow_iso()):
                logger.error("巡检:任务卡死 %s %s(%s)",
                             s["taskid"], s["status"], s["reason"])
        except Exception:  # noqa: BLE001
            logger.exception("卡死巡检异常(继续)")


class _Joinable:
    def __init__(self, thread, server):
        self._thread = thread
        self._server = server

    def join(self, timeout=None):
        self._server.shutdown()
        self._thread.join(timeout=timeout)


def serve(host: str, port: int, db_path: str) -> None:
    lock_fd = acquire_lock(os.path.join(os.path.dirname(db_path) or ".",
                                        "coordinator.lock"))
    conn = db.connect(db_path)
    db.init_schema(conn)
    db.seed_task_types(conn)
    co = Coordinator(conn, LarkCliSender())

    def poll():
        import time
        while True:
            try:
                with poll_lock:
                    co.process_due_outbox()
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger("coordinator").exception("outbox 推进异常(继续)")
            time.sleep(POLL_INTERVAL_SECONDS)

    poll_lock = threading.Lock()
    threading.Thread(target=poll, daemon=True).start()
    # 启动对账 + 周期卡死巡检(I6:serve 主运行模式与 once 同等服务面)
    _startup_reconcile_and_log(co, utcnow_iso())
    watchdog_stop = threading.Event()
    threading.Thread(
        target=_watchdog_loop, args=(co, watchdog_stop), daemon=True
    ).start()
    print(f"sa-coordinator serving on {host}:{port} (db={db_path})", flush=True)
    try:
        ThreadingHTTPServer((host, port), make_handler(co, poll_lock)).serve_forever()
    finally:
        os.close(lock_fd)
        os.unlink(os.path.join(os.path.dirname(db_path) or ".", "coordinator.lock"))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="services.coordinator")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8787)
    sub.add_parser("once")
    args = parser.parse_args(argv)

    if args.cmd == "init-db":
        conn = db.connect(args.db)
        db.init_schema(conn)
        db.seed_task_types(conn)
        print(f"initialized {args.db}")
    elif args.cmd == "serve":
        serve(args.host, args.port, args.db)
    elif args.cmd == "once":
        conn = db.connect(args.db)
        db.init_schema(conn)
        db.seed_task_types(conn)
        report = reconcile(conn, LarkCliSender(), now=__import__("services.coordinator.coordinator", fromlist=["utcnow_iso"]).utcnow_iso())
        print(json.dumps({
            "non_terminal": report.non_terminal,
            "resent": report.resent,
            "stuck": report.stuck,
        }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

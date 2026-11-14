"""P0 #6:真实进程在发送结果已产生、coordinator 尚未落库时被 kill -9。"""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from services.coordinator import db
from services.coordinator.coordinator import Coordinator
from tests.test_coordinator import FakeSender


_FAKE_SENDER = r'''import json, os, signal, sys
from pathlib import Path
log = Path(os.environ["FAKE_SEND_LOG"])
with log.open("a") as f:
    f.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
marker = Path(os.environ["FAKE_SEND_MARKER"])
if os.environ.get("FAKE_SEND_KILL") == "1" and not marker.exists():
    # 模拟外部消息已经产生,但 coordinator 在读取响应/写 sent 前崩溃。
    marker.write_text("1")
    os.kill(os.getppid(), signal.SIGKILL)
count = sum(1 for _ in log.open())
print(json.dumps({"success": True, "messageId": f"om_fake_{count}"}))
'''


def _send_count(path: Path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


def _start_once(db_path: Path, fake: Path, log: Path, marker: Path, kill: bool):
    env = os.environ.copy()
    env.update({
        "SA_SEND_CMD": f"{sys.executable} {fake} {{text}}",
        "FAKE_SEND_LOG": str(log),
        "FAKE_SEND_MARKER": str(marker),
        "FAKE_SEND_KILL": "1" if kill else "0",
    })
    return subprocess.Popen(
        [sys.executable, "-m", "services.coordinator", "--db", str(db_path), "once"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@pytest.mark.parametrize("boundary", ["dispatch", "relay"])
def test_kill_after_send_before_ack_preserves_row_without_duplicate(
    tmp_path, boundary,
):
    db_path = tmp_path / "tasks.db"
    log = tmp_path / "send.jsonl"
    marker = tmp_path / "killed.once"
    fake = tmp_path / "fake_sender.py"
    fake.write_text(_FAKE_SENDER)

    conn = db.connect(str(db_path))
    db.init_schema(conn)
    db.seed_task_types(conn)
    conn.execute("UPDATE task_types SET group_chat_id='oc_p0' WHERE type_code='RD'")
    conn.commit()
    co = Coordinator(conn, FakeSender())
    result = co.ingest_task(
        event_id="ev_crash", message_id="om_crash", text="t",
        task_type="RD", title="崩溃边界", thread_chat_id="oc_router",
    )
    co.dispatch(result.taskid, seed_text=f"[{result.taskid}] seed", mentions=())
    if boundary == "relay":
        co.process_due_outbox()
        co.handle_report({
            "event_id": "ev_report_crash", "taskid": result.taskid,
            "outcome": "completed", "phase": "designing",
            "source_msg_id": "om_worker", "doc_version": "1",
        })
    conn.close()

    first = _start_once(db_path, fake, log, marker, kill=True)
    first.communicate(timeout=15)
    assert first.returncode == -signal.SIGKILL
    assert _send_count(log) == 1

    second = _start_once(db_path, fake, log, marker, kill=False)
    _, stderr = second.communicate(timeout=15)
    assert second.returncode == 0, stderr

    conn = db.connect(str(db_path))
    row = conn.execute(
        "SELECT status, attempts, needs_reconcile FROM outbox "
        "WHERE kind=? ORDER BY id DESC LIMIT 1", (boundary,)
    ).fetchone()
    task = conn.execute("SELECT status FROM tasks WHERE taskid=?", (result.taskid,)).fetchone()
    conn.close()
    assert _send_count(log) == 1, "崩溃前的外部消息不得在重启后重复发送"
    assert row is not None and row["status"] == "pending"
    assert row["needs_reconcile"] == 1, "记录必须安全滞留待对账,不得标记为已发送"
    assert task["status"] == ("received" if boundary == "dispatch" else "report_pending")

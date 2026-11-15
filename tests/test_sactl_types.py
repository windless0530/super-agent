"""sactl types 子命令:类型注册表 group_chat_id / role_members 回填(Task 6)。"""

import importlib.machinery
import importlib.util
import json
import sqlite3

import pytest

from services.coordinator import db

_spec = importlib.util.spec_from_file_location(
    "sactl", "tools/sactl",
    loader=importlib.machinery.SourceFileLoader("sactl", "tools/sactl"),
)
sactl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sactl)


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "tasks.db")
    conn = db.connect(path)
    db.init_schema(conn)
    db.seed_task_types(conn)
    conn.close()
    return path


def test_types_set_group_and_show(db_path, capsys):
    code = sactl.main(["types", "set-group", "RD", "oc_research",
                       "--db", db_path])
    assert code == 0
    capsys.readouterr()  # 丢弃 "ok:" 行
    code = sactl.main(["types", "show", "--db", db_path])
    assert code == 0
    rows = json.loads(capsys.readouterr().out)
    by_code = {r["type_code"]: r for r in rows}
    assert by_code["RD"]["group_chat_id"] == "oc_research"
    assert by_code["DEV"]["group_chat_id"] is None  # 未动


def test_types_set_group_unknown_type_exit3(db_path):
    code = sactl.main(["types", "set-group", "XX", "oc_x", "--db", db_path])
    assert code == 3


def test_types_set_member_roundtrip(db_path):
    code = sactl.main(["types", "set-member", "design",
                       "ou_d1:sa-design-planner", "--db", db_path])
    assert code == 0
    code = sactl.main(["types", "set-member", "review",
                       "ou_r1:sa-design-reviewer", "--db", db_path])
    assert code == 0
    # 重复设置 = 更新(UPSERT),不是报错
    code = sactl.main(["types", "set-member", "design",
                       "ou_d1v2:sa-design-planner", "--db", db_path])
    assert code == 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = {r["role"]: r["mention"] for r in
            conn.execute("SELECT role, mention FROM role_members")}
    conn.close()
    assert rows == {"design": "ou_d1v2:sa-design-planner",
                    "review": "ou_r1:sa-design-reviewer"}


def test_types_show_includes_members(db_path, capsys):
    sactl.main(["types", "set-member", "design", "ou_d1:sa-design-planner",
                "--db", db_path])
    capsys.readouterr()
    sactl.main(["types", "show", "--db", db_path])
    rows = json.loads(capsys.readouterr().out)
    rd = next(r for r in rows if r["type_code"] == "RD")
    assert rd["roles"] == ["design", "review"]
    assert rd["members"] == {"design": "ou_d1:sa-design-planner"}

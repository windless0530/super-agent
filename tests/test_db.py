"""db 模块测试:连接 PRAGMA、建表幂等、默认类型种子。"""

import sqlite3

import pytest

from services.coordinator import db

ALL_TABLES = {
    "tasks",
    "inbox_events",
    "outbox",
    "comment_inbox",
    "bot_ownership",
    "task_types",
}


@pytest.fixture()
def conn(tmp_path):
    """临时文件库(WAL 需要真实文件,:memory: 不支持)。"""
    c = db.connect(str(tmp_path / "tasks.db"))
    db.init_schema(c)
    yield c
    c.close()


def test_init_schema_idempotent(conn):
    # 第二次 init_schema 不报错,6 张表齐全
    db.init_schema(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert ALL_TABLES <= names


def test_wal_and_fk_pragmas(conn):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1


def test_seed_task_types(conn):
    db.seed_task_types(conn)
    rows = conn.execute(
        "SELECT type_code, can_dev, rounds_limit, enabled FROM task_types"
    ).fetchall()
    by_code = {r[0]: r for r in rows}
    assert set(by_code) == {"RD", "RS", "DEV"}
    assert by_code["RD"][1] == 1  # RD 可转开发
    assert by_code["RS"][1] == 0  # RS 无下游
    for code in by_code:
        assert by_code[code][2] == 3  # 三轮规则默认
        assert by_code[code][3] == 1


def test_seed_task_types_idempotent(conn):
    db.seed_task_types(conn)
    db.seed_task_types(conn)  # 重复 seed 不报错(INSERT OR IGNORE)
    n = conn.execute("SELECT COUNT(*) FROM task_types").fetchone()[0]
    assert n == 3


def test_schema_version_exposed():
    assert db.SCHEMA_VERSION == 1

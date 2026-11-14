"""state 模块测试:状态机迁移合法性。"""

import pytest

from services.coordinator.state import (
    TERMINAL,
    TRANSITIONS,
    InvalidTransition,
    is_terminal,
    validate_transition,
)


def test_all_defined_transitions_valid():
    # 穷举 TRANSITIONS 全部边:每条都应放行
    for status_from, targets in TRANSITIONS.items():
        for status_to in targets:
            validate_transition(status_from, status_to)  # 不抛即合法


def test_illegal_transitions_rejected():
    for pair in [
        ("received", "done"),          # 未运行不能直接完成
        ("done", "running"),           # 终态不可迁出
        ("running", "awaiting_decision"),  # 升级必须先 report_pending
        ("clarifying", "done"),
        ("received", "report_pending"),
        ("report_pending", "clarifying"),
    ]:
        with pytest.raises(InvalidTransition):
            validate_transition(*pair)


def test_same_status_rejected():
    with pytest.raises(InvalidTransition):
        validate_transition("running", "running")


def test_unknown_status_rejected():
    with pytest.raises(InvalidTransition):
        validate_transition("running", "bogus")
    with pytest.raises(InvalidTransition):
        validate_transition("bogus", "running")


def test_is_terminal():
    assert is_terminal("done") is True
    assert TERMINAL == {"done"}
    for s in ("received", "clarifying", "running", "report_pending", "awaiting_decision"):
        assert is_terminal(s) is False


def test_done_has_no_outgoing():
    assert TRANSITIONS["done"] == set()

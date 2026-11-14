"""report 校验测试:必填字段、outcome 合法性。"""

import pytest

from services.coordinator.report import OUTCOMES, REPORT_FIELDS, InvalidReport, validate_report


def _valid():
    return {
        "event_id": "ev_1",
        "taskid": "20261114-001",
        "outcome": "completed",
        "phase": "designing",
        "source_msg_id": "om_9",
        "doc_version": "3",
        "payload": {"summary": "完成"},
    }


def test_valid_report_passes():
    validate_report(_valid())  # 不抛即通过


def test_payload_optional():
    ev = _valid()
    del ev["payload"]
    validate_report(ev)  # payload 可缺省


def test_missing_event_id_rejected():
    ev = _valid()
    del ev["event_id"]
    with pytest.raises(InvalidReport):
        validate_report(ev)


def test_missing_taskid_rejected():
    ev = _valid()
    del ev["taskid"]
    with pytest.raises(InvalidReport):
        validate_report(ev)


def test_empty_taskid_rejected():
    ev = _valid()
    ev["taskid"] = ""
    with pytest.raises(InvalidReport):
        validate_report(ev)


def test_invalid_outcome_rejected():
    ev = _valid()
    ev["outcome"] = "finished"
    with pytest.raises(InvalidReport):
        validate_report(ev)


def test_outcomes_and_fields_constants():
    assert OUTCOMES == ("completed", "escalated")
    assert REPORT_FIELDS == (
        "event_id", "taskid", "outcome", "phase", "source_msg_id", "doc_version", "payload",
    )

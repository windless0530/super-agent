"""sactl report 报文的纯校验(单一契约源,verify_skills 交叉引用)。"""

REPORT_FIELDS = (
    "event_id",
    "taskid",
    "outcome",
    "phase",
    "source_msg_id",
    "doc_version",
    "payload",
)

OUTCOMES = ("completed", "escalated")


class InvalidReport(Exception):
    """报文不合法。"""


def validate_report(event: dict) -> None:
    """必填字段齐全、outcome 合法、taskid 非空;payload 可缺省为 {}。"""
    if not isinstance(event, dict):
        raise InvalidReport("report 必须是 JSON 对象")
    for field in ("event_id", "taskid", "outcome", "phase", "source_msg_id", "doc_version"):
        if field not in event or event[field] in (None, ""):
            raise InvalidReport(f"缺少必填字段: {field}")
    if event["outcome"] not in OUTCOMES:
        raise InvalidReport(f"outcome 非法: {event['outcome']}(合法: {OUTCOMES})")
    payload = event.get("payload", {})
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise InvalidReport("payload 必须是 JSON 对象")

"""种子消息模板渲染:spec v1.2 §模板要点「种子消息」。"""

from services.coordinator.seed import render_seed


def test_seed_contains_title_line_roles_protocol_rounds():
    text = render_seed(
        taskid="20261115-001", title="支付网关重连方案调研",
        type_name="研发调研", roles=("design", "review"), rounds_limit=3,
    )
    # 标题行 [taskid] 标题
    assert "[20261115-001] 支付网关重连方案调研" in text
    assert "研发调研" in text
    # 参与角色
    assert "design" in text and "review" in text
    # 完成协议:@Router + sactl report + 文档链接
    assert "@Router" in text and "sactl report" in text and "文档链接" in text
    # 三轮规则提示
    assert "3" in text and "轮" in text


def test_verdict_card_contains_context_plan_and_confirmation_instruction():
    from services.coordinator.seed import render_verdict

    text = render_verdict(
        taskid="20261115-002", title="支付重连", type_name="研发调研",
        roles=("design", "review"), rounds_limit=3,
        context="用户原文:调研重连\n澄清答复:验收成功率 99%",
    )
    assert "[20261115-002] 研发调研 · 支付重连" in text
    assert "用户原文:调研重连" in text and "验收成功率 99%" in text
    assert "design → review" in text and "3" in text
    assert "在本话题回**开工**派单;要改直接说" in text


def test_seed_brief_inline_or_placeholder():
    with_brief = render_seed(taskid="t-1", title="T", type_name="RD",
                             roles=("design",), rounds_limit=3,
                             brief="调研目标:X;边界:不含 Y")
    assert "调研目标:X;边界:不含 Y" in with_brief
    placeholder = render_seed(taskid="t-1", title="T", type_name="RD",
                              roles=("design",), rounds_limit=3)
    assert "thread A" in placeholder  # 无简报时指向原文


def test_task_context_orders_original_decisions_and_current(tmp_path):
    from services.coordinator import db
    from services.coordinator.context import task_context
    from services.coordinator.coordinator import Coordinator
    from services.coordinator.registry import cas_update, get_task
    from tests.test_coordinator import FakeSender

    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    db.seed_task_types(conn)
    co = Coordinator(conn, FakeSender())
    task = co.ingest_task(event_id="ev_1", message_id="om_1", text="调研 X",
                          task_type="RD", title="X")
    current = get_task(conn, task.taskid)
    cas_update(conn, task.taskid, current.version,
               decisions=[{"text": "验收 Y"}])
    assert task_context(conn, task.taskid, "改成 Z") == (
        "用户原文: 调研 X\n澄清/修改 1: 验收 Y\n当前输入: 改成 Z"
    )

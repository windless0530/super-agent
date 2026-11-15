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


def test_seed_brief_inline_or_placeholder():
    with_brief = render_seed(taskid="t-1", title="T", type_name="RD",
                             roles=("design",), rounds_limit=3,
                             brief="调研目标:X;边界:不含 Y")
    assert "调研目标:X;边界:不含 Y" in with_brief
    placeholder = render_seed(taskid="t-1", title="T", type_name="RD",
                              roles=("design",), rounds_limit=3)
    assert "thread A" in placeholder  # 无简报时指向原文

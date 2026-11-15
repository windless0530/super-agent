"""种子消息模板:worker 群新话题的首条消息(spec v1.2 §模板要点)。

纯函数,无副作用;由 /route 渲染、经 dispatch outbox 发送。
"""

PLACEHOLDER_BRIEF = "任务简报:见 thread A 原文(本次路由未携带简报,以用户原话为准)。"


def render_seed(
    taskid: str,
    title: str,
    type_name: str,
    roles: tuple[str, ...] | list[str],
    rounds_limit: int,
    brief: str = "",
) -> str:
    """渲染种子消息。

    结构(spec §模板要点「种子消息」):标题行 / 任务简报 / 参与角色 /
    完成协议 / 三轮规则提示。
    """
    roles_text = "、".join(roles)
    brief_text = f"任务简报:{brief}" if brief.strip() else PLACEHOLDER_BRIEF
    return (
        f"[{taskid}] {title}\n"
        f"类型:{type_name} | 参与角色:{roles_text} | 评审轮数上限:{rounds_limit}\n"
        f"{brief_text}\n"
        "完成协议:完成或分歧升级时 @Router,并经 sactl report 写登记表,"
        "附产物文档链接。\n"
        f"三轮规则:评审意见与执行方回应各计一轮;第 {rounds_limit} 轮仍未一致的"
        "分歧点必须升级(分歧卡片 + sactl report escalated),禁止进入下一轮。"
    )

# P0 验证清单 Runbook

P0 八项验证(方案 v1.2「P0 验证清单」)。探针 #1 必须先做:其结论决定评论回路走事件还是轮询降级(D5)。每项产出写入 `docs/p0/`,通过标准与降级路径以方案为准。

| # | 验证项 | 执行方式 | 产出 |
|-|-|-|-|
| 1 | 评论事件接口探针 `drive.notice.comment_add_v1` | `python3 tools/probe/probe_comment_event.py`(先 `--dry-run` 校对命令,再 live;命令模板在 `probe_config.json`,现场校准) | `docs/p0/2026-11-14-probe1-评论事件.md`(命令、原始输出、available/unavailable 结论) |
| 2 | 话题群顶层消息成话题 + @ 唤醒 bot | 人工 + `botmux send --chat-id <群> --top-level --mention <bot>`;确认话题生成、bot 会话被唤醒 | `docs/p0/probe2-话题群派单.md` |
| 3 | bot 间同话题互 @ 唤醒 | 群内设计 bot 发消息 @评审 bot,确认评审会话收到并同话题 | `docs/p0/probe3-bot互唤.md` |
| 4 | bot 作为目录协作者建文档/权限继承 | 把测试 bot 加为测试目录协作者,bot 身份建文档、user 读评;验证继承 | `docs/p0/probe4-目录协作者.md` |
| 5 | Router 精确回复历史 thread A | 对历史消息用 reply(root) 发送,确认落在原线程;DEV 派生定位 active_task | `docs/p0/probe5-threadA回复.md` |
| 6 | 控制层故障注入 | 重复 message_id/event_id 打 `POST /report` 与 ingest;`kill -9` sa-coordinator 于派单/回投边界后重启;断言无重复任务/话题、无丢回投(跑 `python3 -m services.coordinator once` 对账) | `docs/p0/probe6-故障注入.md` |
| 7 | systemd unit + 容器重启演练 | 写 `systemd/sa-coordinator.service` 等单元(本容器 PID 1 已实测为 systemd),`systemctl start` 后重启容器,断言自动恢复、非终态任务续跑 | `docs/p0/probe7-systemd.md` + `systemd/*.service` |
| 8 | 评论降级与配额 | 仅当 #1 unavailable:跑 `tools/comment_watcher.py`,验证分页/二次回复/Get-OK reaction/is_solved/自回复过滤;记录轮询配额消耗,必要时调 `--active-interval/--idle-interval` | `docs/p0/probe8-轮询降级.md` |

## sender/reader 模板校准点

P0 #2/#3/#5 结论直接决定以下模板默认值(均可用环境变量覆盖,不需改代码):

| 环境变量 | 默认值 | 校准来源 |
|-|-|-|
| `SA_SEND_CMD` | `botmux send --bot {bot} --chat-id {chat_id}` | #2/#3/#5 实测 |
| `SA_REACTION_CMD` | `lark-cli im message add-reaction ...` | #5(Get 表情) |
| `SA_COMMENT_LIST_CMD` | `lark-cli drive +list-comments --doc {doc_token}` | #8 |
| `SA_COMMENT_GET_CMD` | `lark-cli drive +get-comment ...` | #8 |

校准后把最终值回填到 `services/coordinator/sender.py` / `tools/comment_watcher.py` 的默认常量并 commit。

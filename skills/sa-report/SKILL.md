---
name: sa-report
description: 完成协议与升级协议:所有角色 bot 收尾时用 sactl report 提交结构化事件,幂等可靠
---

# sa-report:完成协议 / 升级协议

所有角色 bot(设计、评审、Planner、Executor、开发评审)的唯一收尾通道。**讨论结束后必须上报;分歧满 3 轮必须升级,禁止进入第 4 轮。**

## 何时上报

| 场景 | outcome | 时机 |
|-|-|-|
| **completed** | 本阶段工作完成,产物文档已就绪 | 认领的工作做完、讨论达成一致(或轮满但无分歧) |
| **escalated** | 存在无法自行解决的分歧,需要用户在 thread A 拍板 | 三轮讨论结束仍未一致的每个分歧点 |

## 上报命令

```bash
sactl report \
  --taskid 20261114-001 \
  --outcome completed \
  --phase designing \
  --source-msg-id om_xxx \
  --doc-version 3 \
  --payload-file report-payload.json
```

## 事件字段(与 sa-coordinator 契约一致,不得增删改名)

| 字段 | 必填 | 说明 |
|-|-|-|
| event_id | 是 | sactl 自动生成(uuid4),**每次调用生成一次并跨重试复用**——这是幂等的锚点 |
| taskid | 是 | 种子消息标题里的任务号 |
| outcome | 是 | `completed` 或 `escalated` |
| phase | 是 | 当前阶段名(designing / reviewing / planning / implementing / dev_reviewing 等,与话题内进展一致) |
| source_msg_id | 是 | 触发本次收尾的消息 id(通常是最后一条讨论消息) |
| doc_version | 是 | 产物文档当前版本号 |
| payload | 否 | JSON 对象:summary(一句话结论)、doc_links(产物文档链接)、rounds(各阶段轮数,如 `{"reviewing": 3}`)、disputes(escalated 时必填,分歧卡片) |

## 分歧卡片格式(escalated 必填)

每张卡片五个键,缺一不可:

```json
{
  "viewpoint_a": "设计 bot 的观点",
  "viewpoint_b": "评审 bot 的观点",
  "evidence": "双方依据(数据/文档引用)",
  "impact": "该分歧对任务的影响",
  "suggestion": "执行方的建议倾向"
}
```

## 幂等与重试

- event_id 生成一次后跨重试复用;sa-coordinator 按 event_id 去重,**重复上报不会造成重复回投**。
- 网络失败自动重试 3 次(1/2/4 秒退避);退出码 0=成功或重复,3=被拒绝(检查字段),4=不可达(先确认 sa-coordinator 存活,再重试)。
- 上报成功前不得宣告任务结束;上报失败且重试耗尽时,在话题里 @Router 说明,等待 Watchdog 兜底。

## 三轮规则回顾

一轮 = 评审方一次完整意见 + 执行方一次完整回应。第 3 轮结束仍未一致的**每个**分歧点:写入文档「分歧记录」节 → 打包分歧卡片 → `--outcome escalated` 上报 → 停止讨论。你的拍板会经 Router 转回话题并写入「决策记录」节,届时视为一致,继续执行。

# P0 探针 #6:控制层故障注入

- 日期:2026-11-14
- 结论:**发现并已修复真实崩溃重复发送;重复事件与故障回归通过**

## 覆盖

```bash
python3 -m pytest tests/test_inbox.py tests/test_http.py \
  tests/test_fault_injection.py tests/test_smoke_e2e.py -q
# 23 passed
```

- inbox:同 `event_id`、同 `message_id` 重投不重复建任务。
- HTTP `/report`:同 event 重发返回 `accepted=false, duplicate=true`,只保留一条 relay。
- 真实子进程:fake sender 先记录外部消息已产生,再对 coordinator 发 `SIGKILL`;分别覆盖 dispatch 与 relay 边界;随后用真实 `python3 -m services.coordinator once` 重启对账。

## RED 证据

修复前两条进程测试均失败:第一次发送 1 次,重启后累计 **2 次**。根因是发送前未持久化进行中标记,重启把崩溃行误判为从未尝试的 pending 行。

## 修复与 GREEN

`outbox.mark_inflight()` 在外部调用前原子认领并置 `needs_reconcile=1`;成功才清除。无 probe 时重启安全滞留,不重复外发、不丢记录,任务分别保持 `received` / `report_pending`。

```text
tests/test_fault_injection.py: 2 passed
全量套件:141 passed
```

## 独立评审后补强

- reply 不再继承含 `--chat-id` 的 `SA_SEND_CMD`;仅 `SA_REPLY_CMD` 控制历史线程回投。
- reconciler 对「relay 已 sent、任务仍 report_pending」按最新 completed/escalated 补 CAS,不重发。
- 确定未发出的 SendError 清除对账标记,无 probe 的生产路径也可安全重试。
- 评论话题锚点缺失时不写入去重记录,锚点恢复后仍可通知。

以上均有先失败后通过的回归测试;最终全量 **141 passed**。

## 降级边界

安全滞留不等于自动确认外发结果。生产要自动解除该状态,仍需给常驻/once 路径注入真实 `MsgProbe`;在此之前由人工核查后处理,禁止盲目重发。

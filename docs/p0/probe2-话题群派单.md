# P0 探针 #2:话题群顶层派单与 @ 唤醒

- 日期:2026-11-14
- 结论:**核心链路通过;模型端到端回复被凭据问题阻断**
- 测试群:`oc_589050c3a89aa64f60c95a549e08ea9d`(`P0探针-话题群-可删除`)
- 目标 bot:`cli_aa561825aab8dbb5`;另一发送 bot:`cli_aa41c9f9c5789cbc`

## 实测

1. `botmux create-group --bot cli_aa561825aab8dbb5 --chat-mode topic ...` 建群成功;`chats.get` 回读 `chat_mode=topic`。
2. 另一 bot 在群内 @ 目标 bot,目标 bot 新建会话 `10b58a97-be9f-4cce-87f9-25217e1e9d14`;daemon 日志明确收到 `[来自 Bot 的 @mention]`。**@ 唤醒通过。**
3. 目标会话因 `provider_authentication_failed` 未能生成语义回复。这属于模型配置问题,不能据此宣称完整 worker 闭环通过。
4. 顶层发送:

```bash
botmux send --session-id 10b58a97-be9f-4cce-87f9-25217e1e9d14 \
  --chat-id oc_589050c3a89aa64f60c95a549e08ea9d \
  --top-level --no-mention 'P0 #2 顶层种子消息'
# {"success":true,"messageId":"om_x100b60b2a3d534a4b0e3e3aa7ababd5",...}
```

回读该消息:`thread_id=omt_199de618ad8f5ca2`,`thread_message_position=-1`,与已有线程均不同。**顶层消息成新话题通过。**

## 对 sender 的校准

- `botmux send` 不支持 `--bot`,也不支持 `--text`;正文使用位置参数。
- 宿主/常驻进程必须传**完整** `--session-id`;短 ID 会 `session_lookup_miss`。
- 无 mention 时必须显式 `--no-mention`;mention 使用逐个 `--mention <open_id:name>`。
- 输出含非 JSON 提示行,JSON 字段为 `messageId`。
- 修复后 `LarkCliSender` 实测顶层发送成功:`om_x100b60b399cbd0a4cea67102e1a2a41`。

## 后续

恢复目标 bot 的模型凭据后,在同一测试群重发一条只要求固定文本的 @ 消息,确认语义回复落在同一线程。

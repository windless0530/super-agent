# P0 探针 #3:bot 间同话题互 @ 唤醒

- 日期:2026-11-14
- 结论:**跨 bot @ → 目标 botmux 会话唤醒通过;双向互唤及语义回复部分通过**

## 已验证

发送 bot `cli_aa41c9f9c5789cbc` 在测试群顶层发送:

```bash
lark-cli im +messages-send \
  --chat-id oc_589050c3a89aa64f60c95a549e08ea9d \
  --text '<at user_id="ou_baa4e976b84975fa081823a9e7d13941">Elio The Archangel</at> P0 #2/#3:请只回复固定文本' \
  --as bot
# message_id=om_x100b60b2a6c174a0cf2ccdc9445e720
```

目标 bot `cli_aa561825aab8dbb5` 收到事件并创建同线程会话;回读根消息与目标回包均为 `thread_id=omt_199de649e98f1b5e`。因此「另一个 bot 的 @ 能唤醒目标 bot,且落到同话题」成立。

## 未通过/未覆盖

- 目标 bot 的模型调用返回 `provider_authentication_failed`,没有完成预期文本回复。
- 当前只有目标 bot 接入 botmux;发送 bot 仅通过 lark-cli 发消息,未验证两个 botmux worker 的双向往返。

P1 配置第二个 botmux worker、修复模型凭据后需补跑反向 @ 和双向各一轮固定文本回复。

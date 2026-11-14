# P0 探针 #5:精确回复历史 thread A

- 日期:2026-11-14
- 结论:**通过;必须使用 `--into`,现有 `--reply-to` 方案不适用**

## 证伪旧参数

```text
--reply-to ... --chat-id ... → --reply-to 不能与 --chat-id 混用
--reply-to <历史消息>          → requires a retained incoming message from this session
```

因此 `--reply-to` 只适合当前会话仍保留的进线消息,不适合 coordinator 重启后回复任意历史 thread A。

## 通过路径

```bash
botmux send --session-id 10b58a97-be9f-4cce-87f9-25217e1e9d14 \
  --into om_x100b60b299cc10a0b05a51186728f78 \
  --no-mention 'P0 #5:精确回复历史 thread A'
# messageId=om_x100b60b2bb66b8a4cfffa3cb00cc32f
```

回读根消息与新回复:`thread_id` 均为 `omt_199de5b93fcf1ca9`;新回复 `thread_message_position=0`。修复后的 `LarkCliSender.send_reply()` 也在另一新线程实测成功。

## Get 表情校准

真实可用命令为:

```bash
lark-cli im reactions create --message-id <om_xxx> \
  --data '{"reaction_type":{"emoji_type":"Get"}}' --as bot
```

原默认 `im message add-reaction` 不存在。修复后已实测 Get reaction 写入并回读 `count=1`。

# P0 探针 #8:评论轮询降级与配额

- 日期:2026-11-14
- 结论:**功能链路通过;已实测每轮 API 放大,生产启用前需按配额选间隔**
- 文档:`TcSsddqsso4OaFxMLizcBoaZnyh`(`P0探针-评论轮询-可删除`)

## 修复前实测失败

- `+list-comments --doc` → `unknown flag --doc`。
- `+get-comment` 不存在;真实接口是 `+list-replies`。
- 正文位于 `content.elements`;reaction 是回复上的聚合结构(`reaction_key/count/ahead_users`),不是旧 fixture 的平铺字段。

修复后默认命令使用 `--token <token> --type docx --solved-status all --need-reaction --as bot`,回复使用 `+list-replies`,并把内容/reaction 规范化。

## 真实场景结果

1. 全文首评 + Get(bot) + OK(user):读取成功;bot Get 被过滤,user OK 单独写入 `comment_inbox`,不产生 notify。
2. 局部评论 `7708053896748354491` + 用户二次回复 + bot 自回复:用户二次回复正常通知,bot 自回复被过滤。
3. 一轮真实同步:**首轮 3 条 notify**(全文首评、局部首评、用户二次回复),**第二轮 0**;通知文本不含 bot 自回复。
4. 全文评论 `7708052200416611331` resolve 后,watcher 回读 `is_solved=true`;另一未解决评论仍为 false。
5. 局部评论可二次回复。#1 中的 1069302 根因是 `is_whole=true` 的全文评论限制,不是「API 建文档默认禁回复」。

## 分页

- 评论卡片与回复均按 `has_more + page_token` 展开。
- 仅回复第一页的 `items[0]` 是根回复;后续页首项不能跳过。
- 回归测试覆盖两页评论、两页回复、更新去重、自回复过滤、Get/OK 与 solved 记录。

## 配额实测口径

当前文档有 2 张评论卡片,每轮调用:

- 1 次 `list_comments`
- 2 次 `list_replies`

即 **3 次/轮**。一般公式为 `1 + 评论卡片数`。在 3 秒活动间隔下,该文档约 60 次/分钟、**86,400 次/天**;15 秒约 17,280 次/天;60 秒空闲间隔约 4,320 次/天。多文档按活动目标数累加。

本轮未获得租户限额或 rate-limit 响应头,不能宣称配额充足。生产启用前应确认限额;必要时用 `--active-interval 15`(或更大)并保留指数退避。

## 验证

```text
tests/test_comment_watcher.py:14 passed
全量套件:141 passed
```

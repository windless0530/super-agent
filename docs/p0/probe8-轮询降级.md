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

~~当前文档有 2 张评论卡片,每轮调用:1 次 list_comments + 2 次 list_replies,即 3 次/轮。~~ **已于 2026-11-15 优化为稳态 1 次/轮/文档**,见下节。

### 频控实测(2026-11-15)

用本文档对 lark-cli 所在应用(cli_aa41c9f9c5789cbc)做了两组突发:

- 串行 40 连发(list-comments/list-replies 交替):40 ok / 0 err,约 0.8 调/s(lark-cli 进程启动约 1.3s/次,是瓶颈而非服务端)。
- 20 并发 × 40 连发:40 ok / 0 err,峰值约 6.1 调/s,无 429。

结论:1 QPS 量级的轮询远未触服务端频控;官方文档站为 SPA 抓不到正文,租户级每日总量上限仍未拿到书面数字,不宣称"配额充足",但已无硬限流证据。

### 稳态优化:list 签名跳过(2026-11-15)

实测确认 `list-comments` 内嵌 `reply_list.replies` 与 `list-replies` 详情**完全一致**(1=1、3=3、无截断,`--need-reaction` 下含 reactions)。据此实现:

- 每轮固定 1 次 list(含分页);对每条评论计算签名(comment update_time / is_solved / solver / 作者 / 卡片 reaction 数 / 内嵌回复的 reply_id+update_time+reaction 数)。
- 签名与上轮一致 → 跳过该评论的全部 detail 调用;任一变化才展开对应评论。
- detail 展开发现比内嵌更多的回复 → 标记 unstable,此后每轮必展开(防御截断)。
- state 为进程内存;重启后首轮全量展开一次,notify 去重仍由 comment_inbox 兜底。

调用口径:**首/变化轮 `1 + 变化评论数`,稳态 1 次/轮/文档**。3 秒间隔单文档 28,800 次/天(原 86,400);15 秒 5,760 次/天。反应性不降:任何编辑/新回复/解决/点赞都会改变签名并在下一轮(≤3s)被展开。

## 验证

```text
tests/test_comment_watcher.py:19 passed(新增稳态跳过 4 例)
全量套件:145 passed(修复 5 处跨 UTC 日硬编码日期断言后)
```

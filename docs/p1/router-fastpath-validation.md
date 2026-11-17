# Router fastpath 生产验证记录

- 日期:2026-11-17
- 范围:`/inbox`、DeepSeek verdict、awaiting_go 开工门、`/confirm`、im_watcher、原生 text 种子、systemd 常驻
- 结论:**功能链路与本次延迟探针通过**;新任务 `20261117-002` 仍停在 awaiting_go,未自行开工

## 部署状态

- `sa-coordinator`、`sa-comment-watcher`、`sa-im-watcher` 均为 `active`,watcher 已 enable。
- `sa-im-watcher` 先加载 `sa.env`(PATH)再加载 `im-watcher.env`;cursor 与 thread 映射写入 `registry/im_watcher_cursor.json`。
- DeepSeek 使用 `https://api.deepseek.com/chat/completions` + `deepseek-flash`;key 仅写现场 600 env,未出现在日志或仓库。

## 真机发现与修复

| 问题 | 根因 | 修复与回归 |
|---|---|---|
| watcher 找不到 `lark-cli` | 新 unit 未加载 `sa.env` 的 PATH | unit 增加共享 env;`test_im_watcher_unit_syntax_and_entrypoint` |
| 游标落到未来 8 小时,API 报 `end_time earlier than start_time` | lark-cli 本地 naive `create_time` 被误当 UTC | naive 时间先按主机时区定位再归一 UTC;`test_naive_lark_create_time_uses_host_timezone` |
| 用户正文被过滤 | text 消息的 `content` 是纯字符串,不总是 JSON | 兼容纯字符串与 JSON/dict;`test_normalize_plain_lark_text_content` |
| thread 中的「开工」不可见 | chat 查询只返回话题根;未文档化 `only_thread_root_messages=false` 返回不完整且曾报 2200 | 不依赖该参数;chat 发现 thread + `+threads-messages-list` 展开回复,journal 持久化 `thread_id → root_msg_id`;21 个 watcher 测试 |

前两次测试消息未受理,均按上述根因修复后重放恢复;没有伪造延迟数据。

## 功能证据

### 任务 `20261117-001`(支付网关重连)

1. 恢复后落库为 RD,状态 `awaiting_go`;结论卡 `om_x100b608980e90caccfcae406b7926fb` 发出时无 dispatch。
2. 用户在对应 thread 回「开工」后,状态为 `running`,dispatch 仅 1 条。
3. 种子 `om_x100b60f58dbf08a8ce8c67bf0bb2e54`:
   - `msg_type=text`,发送者 `SA-先知`;
   - 首行 `[20261117-001] 调研支付网关重连方案并给出推荐`;
   - thread `omt_199994fe0e4f9b44`,包含原生 @SA-方案-导师/@SA-方案-圣裁。
4. 对原「开工」message_id 重投 `/inbox`,返回:
   `{"ok":true,"action":"confirm","taskid":"20261117-001","duplicate":true,"state":"running"}`;dispatch 仍为 1。
5. 后续 worker 已完成该调研任务并回投,说明种子确实唤醒了工场。

### 任务 `20261117-002`(消息队列重试,未开工)

- 用户消息:`om_x100b60f60ead28a8b07b88fd2a6400e`
- 登记时间:`2026-11-17T09:53:15.303496+00:00`
- Get:`2026-11-17T09:53:16+00:00`,距登记 **0.696s**
- 结论卡:`om_x100b60f60fc7b4a0cf5c381fc323a17`,落库时间 `09:53:18.258591`,距登记 **2.955s**
- 加上最多 2s 轮询等待,本次探针仍满足 Get ≤5s、结论卡 ≤10s 的目标。
- 状态保持 `awaiting_go`,只有结论卡 relay,**没有 dispatch**。

## 自动化验证

- `python3 -m pytest tests/ -q` → **240 passed**
- `python3 tools/verify_skills.py` → 7 个 Skill OK
- 独立整分支评审后修复:重复 confirm 恢复、outbox 锁外发送、同 thread 串行化、空轮 2s 重叠。
- 生产服务最近日志无 watcher ERROR;coordinator `/healthz` 返回 `{"ok":true}`。

## 边界与遗留

- 本次只证明单次延迟探针,不宣称所有网络/配额条件下都满足 SLO。
- 首次启动不回放历史;已发现 thread 会持久化,重启后继续读取其回复。
- 旧 `/route` 的门禁由 HTTP 回归测试覆盖;生产未另造测试任务验证。
- deferred minors 见执行台账:M-2 损坏 journal 恢复、M-3 默认 reaction profile、M-4 received 提示、M-5 wake 竞态、M-6 编辑/非文本输入。
- 回滚必须先停 watcher,drain 或迁移 awaiting_go,步骤见 `docs/p1/ops-runbook.md`。

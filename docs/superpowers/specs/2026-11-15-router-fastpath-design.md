# Router 快路径改造设计(收单秒回 + 精简判定 + 开工门禁)

- 日期:2026-11-15
- 状态:已批准(用户要求继续执行);builder check 已复核并吸收
- 背景:首次真机任务暴露三个产品问题——①Get 收单确认要等 router LLM 会话跑完(分钟级,应秒级、零 LLM);②意图判定上下文过重(botmux 全量注入),应只剩固定 system prompt + 用户原文;③任务未经用户确认就自行开工。

## 目标

1. 用户消息发出后 **≤5 秒**看到 Get 表情(确定性层,无 LLM)。
2. 意图判定正常路径 **≤10 秒**出结论卡(deepseek-flash 直连,固定 prompt)。
3. 派单前有**用户确认门**:router 出「理解 + 执行计划」卡,用户在对应 thread A 回「开工」才 dispatch。
4. 种子话题在话题列表可见(用飞书原生 text 消息,不用 botmux 卡片)。

## 非目标(本期不做)

- botmux 插件级事件钩子(<1s 延迟的进一步优化,留 P2)。
- 工场内部 Spec/方案后的第二道门(用户已选只做派单前一道)。
- 在途任务 002 的流程迁移(旧流程跑完,P2 收口状态相位问题)。

## 架构(改后)

```
用户消息(控制会话;回复必须落在对应 thread A)
 → im_watcher(新 daemon,2s 轮询,sa-router bot 身份)
 → POST /inbox(coordinator)
     ├─ 单次 message_id 幂等占位 + Get 表情       ← 秒回,零 LLM
     ├─ 绑定 awaiting_go + 精确「开工」→ /confirm 内部流转
     ├─ 绑定 clarifying               → 答复/新任务判定
     ├─ 绑定 awaiting_go + 其他文字   → 修改原任务,重新 verdict
     └─ 无绑定新消息                  → 新任务 verdict
 → verdict(coordinator 内直连 deepseek-flash,固定 system prompt + 规范上下文)
     ├─ 歧义 → 澄清问题 relay(thread A)
     └─ 明确 → 结论卡 relay + 状态 awaiting_go
 → 用户在对应 thread A 回「开工」→ dispatch(现有 outbox 种子路径)→ 工场话题
```

router 的 botmux Claude 会话退出 happy path,仅保留日常寒暄与异常恢复兜底;无论经新 `/inbox` 还是旧 `/route`,明确 verdict 都只能到 `awaiting_go`,不能绕过确认门。

## 组件与接口

1. **`POST /inbox`**(新):body `{message_id, chat_id, root_msg_id, text, sender_open_id}`。全入口只以 `msg:<message_id>` claim 一次,不再与 `/ingest` 双重占位;按 `thread_a_chat_id + thread_a_msg_id` 绑定控制回复。响应 `{ok, action: get|confirm|clarified|ingest, taskid?, duplicate?}`;孤立「开工」返回 `action=get` 并提示到对应话题。重复投递若任务仍 `received`,必须续跑 verdict,不得被 duplicate 假成功吞掉。
2. **任务绑定**:仅回复对应 thread A 的消息可 confirm/澄清/修改;不按「最新一单」猜测。顶层孤立「开工」不新建任务,回复提示用户到对应话题操作。多个并行任务因此不会串单。
3. **verdict 调用**(新 `llm.py`,stdlib urllib):OpenAI 兼容 `POST {SA_LLM_BASE_URL}/chat/completions`;env `SA_LLM_BASE_URL=https://api.deepseek.com`、`SA_LLM_KEY`、`SA_LLM_MODEL=deepseek-flash`,超时 8s。2026-11-15 已实测该三元组返回 200,64-token 探针约 1.01s 返回 OK;key 从既有 DeepSeek 配置注入,不入库/日志。固定 system prompt 包含 RD/RS/DEV 定义、歧义判据、标题规则与 JSON schema。新任务/修改输出 `{"ambiguous":bool,"task_type"?,"title"?,"clarify_question"?}`;澄清阶段额外判定 `is_new_task`。
4. **规范上下文**:原始 prompt + 已记录的澄清答复/修改 + 当前输入,按时间顺序拼接;verdict、结论卡和种子 brief 使用同一上下文,不再只取最初原文。
5. **状态机**:`received → awaiting_go|clarifying`;`awaiting_go → received`(修改)或 `running`(confirm);其余不变。另保留内部 `received → running` 仅供旧 dispatch 已发出后的崩溃恢复,不作为任何 HTTP 新派单路径。awaiting_go 的 Watchdog 阈值为 24h。`/route` 非歧义分支改为写类型/标题、入队结论卡并进入 awaiting_go;`/confirm` 独享 dispatch。
6. **`POST /confirm`**(新):body `{taskid}`;仅 awaiting_go 可 CAS 到 running 并入队 dispatch,复用 `/route` 原同事务逻辑。重复 confirm 在已 running 时幂等返回;其他状态 409。
7. **并发与外发**:DB claim/CAS/outbox 入队持短锁;Get 与 LLM 均在全局 DB 锁外执行。结论卡入队后立即唤醒/推进 outbox,不等待固定 5s 轮询。LLM 失败返回 503,任务滞留 received;watcher 不推进游标,下一轮以同一 message_id 重试。
8. **im_watcher**(`tools/im_watcher.py` + unit `sa-im-watcher`):经 `+chat-messages-list` 发现新根消息,并以 `+threads-messages-list` 展开已知 thread 回复(chat 查询只给话题根);journal 持久 `thread_id → 原始 root_msg_id` 映射、ISO UTC 时间游标与 2s 重叠。兼容 lark-cli 纯文本正文与本地 naive 时间(转主机时区后归一到 UTC)。2s 一轮、升序完整分页;过滤 router 自己、撤回和非文本消息。首次启动从当前时间开始、不回放历史;整轮消息成功受理后才原子推进,失败指数退避。
9. **结论卡模板**:`[taskid] 类型 · 标题` / 我的理解(规范上下文摘要)/ 执行计划(角色链+轮数)/ 「在本话题回**开工**派单;要改直接说」。
10. **种子可见性**:dispatch 使用 `lark-cli --profile sa-router im +messages-send --msg-type text`,首行 `[taskid] 标题`;mentions 转为原生 `<at user_id="ou_xxx">name</at>`。其余 relay 仍走 botmux。
11. **sa-router SKILL.md**:改为兜底角色;保留红线与 sactl 手册。旧 `/ingest`、`/route` 保留,旧 `/route` 同样只到 awaiting_go;运维可显式 `sactl confirm` 兜底。

## 环境与部署

- sa.env 新增:`SA_LLM_BASE_URL`、`SA_LLM_KEY`、`SA_LLM_MODEL`;im-watcher.env 新增 `SA_CONTROL_CHAT_ID`、`SA_IM_CURSOR_PATH=registry/im_watcher_cursor.json`。
- 新 unit `sa-im-watcher.service`;coordinator 部署后先 `/healthz`,再启动 watcher。
- 回滚:停新 unit;先 drain 或显式把 awaiting_go 迁回 received,再切旧代码并重启,避免旧二进制遗留不可路由状态。

## 测试(TDD)

- `/inbox` 分类矩阵(新任务/开工/澄清/修改/孤立开工/重复消息)× 单次 claim × Get
- verdict mock HTTP:正常/歧义/超时/畸形 JSON/澄清新任务;真实探针记录延迟
- `/route` 与 `/confirm` 均不能绕过门禁;confirm 状态迁移、幂等、非 awaiting_go 拒绝
- 重复投递在 received 状态续跑 verdict;LLM 失败不推进 watcher 游标
- 多任务同私聊时只按 thread A 绑定,不串单
- awaiting_go 的 Watchdog 24h 阈值
- 规范上下文贯穿 verdict、结论卡、种子 brief
- 结论卡与原生 text 种子模板、mention 渲染
- im_watcher 首轮不回放、chat/thread 升序分页、自过滤、纯文本与本地时间归一、thread 根映射持久化、游标原子推进、重启重叠去重、失败退避

## 方案取舍(已考虑)

- **事件推送 vs 轮询**:botmux 插件进程内钩子延迟最优但引入插件开发/打包风险;lark-cli event consume 与 botmux 争用同 app 长连接(未验证)。选 2s 轮询,<1s 优化留 P2。
- **判定留在会话内瘦身 vs 直连**:会话内无法剥离 botmux 注入的运行时上下文;直连已实测约 1s、数十 token。
- **门禁位置**:只做派单前一道;工场内两道门方案已否决。
- **顶层开工快捷匹配**:拒绝按最新任务猜测;必须在对应 thread A 回复,牺牲一点便捷换取多任务不串单。

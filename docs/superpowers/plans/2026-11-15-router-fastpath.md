# Router Fastpath Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Router 收单、语义判定和派单拆成确定性快路径,实现 Get 秒回、DeepSeek 精简判定、thread 级开工门禁和可见的原生文本种子。

**Architecture:** 新 `im_watcher` 轮询控制会话并调用 `/inbox`;coordinator 以单次 inbox claim 建任务/绑定 thread,锁外执行 Get 与 DeepSeek verdict,所有明确任务先到 `awaiting_go`。`/confirm` 是唯一派单入口,复用 SQLite CAS + outbox;种子改走 lark-cli 原生 text。

**Tech Stack:** Python 3 stdlib(urllib/http.server/sqlite3/threading/subprocess)、pytest、lark-cli、botmux、systemd user units。

**Spec:** `docs/superpowers/specs/2026-11-15-router-fastpath-design.md`

## Global Constraints

- Get 目标 ≤5 秒、零 LLM;正常 verdict + 结论卡目标 ≤10 秒;DeepSeek 超时 8 秒。
- `SA_LLM_BASE_URL=https://api.deepseek.com`,`SA_LLM_MODEL=deepseek-flash`;key 只经 env 注入,禁止写库、日志或仓库。
- 控制回复只按 `thread_a_chat_id + thread_a_msg_id` 绑定;禁止按“最新任务”猜测;开工词为去首尾空白后的精确 `开工`。
- 所有路径(包括旧 `/route`)必须先进入 `awaiting_go`;只有 `/confirm` 可 enqueue dispatch。
- SQLite 单写者仍是 coordinator;watcher 只经 HTTP 写入;DB 短事务与外部 Get/LLM 调用分离。
- 不迁移在途任务 `20261115-002`;旧 `/ingest`、`/route` 保留作兼容入口。
- 仅 Python stdlib,不新增第三方运行依赖;每个任务结束跑全量测试。基线:`182 passed`。
- 实现位于独立 worktree;合并 main、重启生产 unit、真机发消息前必须停下取得用户确认。

## Review Focus

1. **多任务并行时孤立/跨 thread 的“开工”** → 不得确认最新或其他任务;测试 Task 3。
2. **LLM 超时后 watcher 重投同一 message_id** → received 任务必须续跑 verdict,不得 duplicate 假成功或重复建单;测试 Task 3、4。
3. **LLM/Get 阻塞期间 report 或 confirm 进线** → 外部调用不得持全局 DB 锁;测试 Task 3。
4. **旧 Claude 兜底调用 `/route`** → 只能发结论卡并进入 awaiting_go,不得直接派单;测试 Task 2。
5. **watcher 首轮/重启/bot 自发卡片** → 不回放历史、不自唤醒,重叠游标由 message_id 幂等兜底;测试 Task 4。

---

### Task 1: DeepSeek verdict 客户端

**Files:**
- Create: `services/coordinator/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `SA_LLM_BASE_URL`、`SA_LLM_KEY`、`SA_LLM_MODEL`、既有 DeepSeek key 注入。
- Produces: `VerdictError(RuntimeError)`;`DeepSeekVerdict(base_url=None, key=None, model=None, timeout=8.0, opener=None)`;`decide(*, mode: str, context: str) -> dict`,mode 为 `task|clarification`,task 返回 `ambiguous` 及对应字段,clarification 额外返回布尔 `is_new_task`。

- [ ] **Step 1: 写失败测试**:mock `opener`,覆盖明确 DEV、歧义 RD、澄清答复 `is_new_task=false`、澄清新任务 true;断言 URL 为 `https://api.deepseek.com/chat/completions`、model=deepseek-flash、Authorization 使用 env key 且请求只有固定 system prompt + 当前规范 context。
- [ ] **Step 2: 写异常测试**:非 JSON、缺 `ambiguous`、非布尔 `is_new_task`、HTTP/超时均抛 `VerdictError`;异常文本不包含 key。缺 key/base_url 时启动失败。
- [ ] **Step 3: 跑测试确认失败**:Run `python3 -m pytest tests/test_llm.py -q` Expected: FAIL(`ModuleNotFoundError`)。
- [ ] **Step 4: 实现 `llm.py`**:stdlib urllib;`response_format={"type":"json_object"}`;固定 prompt 写清 RD/RS/DEV、一次一个澄清问题、标题 ≤60 字、严格 JSON;解析 `choices[0].message.content` 并做字段校验。
- [ ] **Step 5: 跑测试确认通过**:Run 同上 Expected: 全部 PASS。
- [ ] **Step 6: 全量回归并提交**:Run `python3 -m pytest tests/ -q` Expected: 182 + 新增全绿;`git commit -m "feat: add direct DeepSeek verdict client"`。

### Task 2: awaiting_go 门禁、结论卡与 `/confirm`

**Files:**
- Create: `services/coordinator/context.py`
- Modify: `services/coordinator/state.py`
- Modify: `services/coordinator/coordinator.py`
- Modify: `services/coordinator/cli.py`
- Modify: `services/coordinator/seed.py`
- Modify: `services/coordinator/reconciler.py`
- Modify: `tools/sactl`
- Test: `tests/test_route_endpoint.py`、`tests/test_confirm_endpoint.py`、`tests/test_seed_template.py`、`tests/test_reconciler.py`、受影响旧测试

**Interfaces:**
- Consumes: Task 1 的 verdict 数据形状;现有 `Coordinator.dispatch()`、`cas_update()`、outbox。
- Produces: `task_context(conn, taskid: str, current: str = "") -> str`;`render_verdict(taskid, title, type_name, roles, rounds_limit, context) -> str`;`Coordinator.confirm_task(taskid: str) -> dict`;`POST /confirm {taskid}`;`sactl confirm --taskid`;状态 `awaiting_go`。

- [ ] **Step 1: 写状态与模板失败测试**:允许 `received→awaiting_go`、`awaiting_go→received|running`;保留内部 `received→running` 供旧 dispatch 崩溃恢复;拒绝 `awaiting_go→awaiting_go`;`render_verdict` 含 `[taskid] 类型 · 标题`、规范上下文、角色链、轮数及“在本话题回开工;要改直接说”。
- [ ] **Step 2: 写 route/confirm 失败测试**:非歧义 `/route` 后状态 awaiting_go、只有 relay 结论卡、没有 dispatch;`/confirm` 后才 CAS running + 一条 dispatch;重复 confirm 返回 `duplicate=true` 且不新增 outbox;received/clarifying/report_pending confirm 返回 409。旧 `/route` 不得绕门。
- [ ] **Step 3: 写上下文与 Watchdog 测试**:`task_context` 按“原文→decisions→current”顺序且供 seed brief 使用;awaiting_go 23h 不卡死、25h 报告卡死。
- [ ] **Step 4: 跑测试确认失败**:Run `python3 -m pytest tests/test_route_endpoint.py tests/test_confirm_endpoint.py tests/test_seed_template.py tests/test_reconciler.py -q` Expected: FAIL(无 awaiting_go/confirm)。
- [ ] **Step 5: 实现状态、模板与上下文**:更新 `STATUS/TRANSITIONS`;`context.py` 从首条绑定该 taskid 的 inbox payload 取原文(不限 kind,兼容快路径控制事件),追加 `tasks.decisions` 与可选 current;`seed.py` 增加纯函数 `render_verdict`。
- [ ] **Step 6: 改造 `/route`**:歧义分支不变;明确分支校验注册表后,以单事务 CAS `received→awaiting_go`、写 type/title、enqueue thread A relay 结论卡;返回 `{ok,taskid,state:"awaiting_go"}`,不得调 dispatch。
- [ ] **Step 7: 实现确认派单**:将原 `/route` 的 mentions、render_seed、CAS + dispatch 事务迁入 `Coordinator.confirm_task`;仅 awaiting_go 可执行,running 且已有 dispatch 时幂等;新增 `ConfirmRejected` 并映射 409;接入 HTTP 与 sactl。
- [ ] **Step 8: 更新受影响旧测试**:凡 fixture 需要 running,在 `/route` 后显式 `/confirm`;保留断言旧入口兼容但不再自动派单。
- [ ] **Step 9: 跑测试确认通过**:Run Step 4 命令 Expected: 全部 PASS。
- [ ] **Step 10: 全量回归并提交**:Run `python3 -m pytest tests/ -q` Expected: 全绿;`git commit -m "feat: gate dispatch behind explicit confirmation"`。

### Task 3: 单次 claim 的 `/inbox` 快路径

**Files:**
- Create: `services/coordinator/fastpath.py`
- Modify: `services/coordinator/registry.py`
- Modify: `services/coordinator/cli.py`
- Test: `tests/test_fastpath_inbox.py`

**Interfaces:**
- Consumes: `DeepSeekVerdict.decide()`、`task_context()`、`Coordinator.confirm_task()`、Task 2 的 `/route` 内部处理;现有 `ingest_task()` 的孤儿 claim 恢复。
- Produces: `Fastpath(co, verdict, db_lock, wake_outbox=None).handle(event: dict) -> dict`;`POST /inbox`;`allocate_taskid(..., commit: bool = True)` 与 `create_task(..., commit=False)` 的真实事务边界。

- [ ] **Step 1: 写新任务/幂等失败测试**:新消息只产生一条 `msg:<id>` inbox 行和一个任务;事件顺序为 DB 提交→Get→verdict;明确 verdict 到 awaiting_go;同消息重复不多 Get、不建单;模拟建任务异常时整体回滚,不烧 message_id 或 taskid 序号。
- [ ] **Step 2: 写恢复与锁失败测试**:首次 verdict 抛 `VerdictError` 返回 503、任务 received;同 message_id 重试成功续跑;用阻塞 fake verdict + 并发 `GET /tasks` 证明 LLM 不持 DB 锁。Get 也在锁外,失败仅记录不影响 verdict。
- [ ] **Step 3: 写绑定分类矩阵**:构造两个同 chat、不同 thread 的任务;awaiting_go + 精确开工只 confirm 对应任务;`先不开工`/孤立开工不 confirm;clarifying 答复合并 decisions 后重新 verdict;`is_new_task=true` 新建独立 thread 根,原任务保持 clarifying;awaiting_go 修改回 received 并把修改写入下一次 context。
- [ ] **Step 4: 写唤醒测试**:结论卡或 dispatch 入队后调用 `wake_outbox`,无需等 5 秒轮询。
- [ ] **Step 5: 跑测试确认失败**:Run `python3 -m pytest tests/test_fastpath_inbox.py -q` Expected: FAIL(404/无模块)。
- [ ] **Step 6: 实现单次 claim 与任务创建**:`Fastpath` 在短锁内 claim `msg:<id>`;新任务直接复用 `ingest_task(commit=False)` 的孤儿占位恢复来建单并 bind taskid(只保留一行 inbox);回复绑定后 bind 对应 taskid;`allocate_taskid`/`create_task` 将 commit 标志下传,异常统一 rollback;提交后释放锁,再对新事件当前 message_id 加 Get。
- [ ] **Step 7: 实现分类与锁外 verdict**:confirm 精确匹配;clarification 先调 `mode='clarification'`;修改先原子 `awaiting_go→received` 并追加 decision;规范上下文统一传入 verdict 与 `/route`;LLM 失败抛 `VerdictError`,HTTP 映射 503。
- [ ] **Step 8: 接入 HTTP 与即时 outbox 唤醒**:`make_handler(co, lock, verdict=None, wake_outbox=None)` 对 `/inbox` 使用 Fastpath,不套外层全局锁;`serve()` 用 `threading.Event` 让 outbox loop 可被立即唤醒;其他端点保持原锁语义。
- [ ] **Step 9: 跑测试确认通过**:Run Step 5 命令 Expected: 全部 PASS。
- [ ] **Step 10: 全量回归并提交**:Run `python3 -m pytest tests/ -q` Expected: 全绿;`git commit -m "feat: add idempotent IM inbox fastpath"`。

### Task 4: `im_watcher` 轮询、游标与退避

**Files:**
- Create: `tools/im_watcher.py`
- Test: `tests/test_im_watcher.py`

**Interfaces:**
- Consumes: `POST /inbox`;`lark-cli --profile sa-router im +chat-messages-list --as bot --no-reactions`。
- Produces: `LarkCliMessageReader.list_messages(chat_id, start, page_token=None) -> dict` 与 `list_thread_messages(thread_id, page_token=None) -> dict`;`CursorStore(path).load(now) / load_threads() / save(cursor, threads)`;`sync_once(..., threads) -> str`;`watch_loop(...)`;CLI 必填 `--chat-id`、至少一个 `--bot-id`。

- [ ] **Step 1: 写消息归一化失败测试**:text JSON/dict 提取正文;sender open_id 兼容嵌套/平铺;root_msg_id 优先 `root_id`,否则 message_id;过滤 bot、deleted、非 text。
- [ ] **Step 2: 写分页与游标失败测试**:chat/thread 均升序完整分页并传 page_token;无 journal 首次从传入 now 开始;整轮成功后原子保存;2s 重叠(含空轮);重启加载 cursor 与 `thread_id → root_msg_id` 映射。
- [ ] **Step 3: 写可靠投递测试**:逐条 POST `/inbox`;任一非 2xx 抛错且不保存 cursor,下轮重投;成功响应含 taskid;单轮 reader 异常由 watch_loop 指数退避且继续。
- [ ] **Step 4: 跑测试确认失败**:Run `python3 -m pytest tests/test_im_watcher.py -q` Expected: FAIL(`ModuleNotFoundError`)。
- [ ] **Step 5: 实现 reader 与归一化**:`SA_IM_LIST_CMD` 可覆盖,默认显式 sa-router profile、`--order asc --page-size 50 --no-reactions`;subprocess 30s 超时;按 lark-cli `ok=true` 信封读取 data.messages。
- [ ] **Step 6: 实现 CursorStore 与 sync/watch**:journal 写同目录临时文件后 `os.replace`;首轮不回放;chat 根消息发现 thread,逐个展开回复并补原始 root_msg_id;成功整轮才推进 cursor 与 thread 映射;HTTP 用 urllib,非 2xx 不消费;watch 默认 2s、退避 1→300s。
- [ ] **Step 7: 跑测试确认通过**:Run Step 4 命令 Expected: 全部 PASS。
- [ ] **Step 8: 全量回归并提交**:Run `python3 -m pytest tests/ -q` Expected: 全绿;`git commit -m "feat: add durable control-chat IM watcher"`。

### Task 5: 原生 text 种子与 mention

**Files:**
- Modify: `services/coordinator/sender.py`
- Modify: `services/coordinator/coordinator.py`
- Test: `tests/test_sender.py`、`tests/test_coordinator.py` 的 FakeSender/断言

**Interfaces:**
- Consumes: dispatch outbox payload `{chat_id,seed_text,mentions}`;role_members 的 `ou_xxx:name` mention。
- Produces: `Sender.send_seed(chat_id: str, text: str, mentions: tuple[str,...]=()) -> str`;`LarkCliSender` 默认经 sa-router profile 发原生 text;`Coordinator._send('dispatch')` 调用它。

- [ ] **Step 1: 写失败测试**:dispatch 调 `send_seed`;默认 argv 含 `lark-cli --profile sa-router im +messages-send --as bot`,不含 `--session-id/--top-level/--no-mention`;首行标题不变;`ou_1:研发导师` 转为 `<at user_id="ou_1">研发导师</at>`;含空格/flag 的正文仍是单 argv。
- [ ] **Step 2: 跑测试确认失败**:Run `python3 -m pytest tests/test_sender.py tests/test_coordinator.py -q` Expected: FAIL(无 send_seed/参数不符)。
- [ ] **Step 3: 实现 `send_seed`**:新增 `DEFAULT_SEED_SEND_CMD` 与 `SA_SEED_SEND_CMD`;独立构造 argv,不复用会自动追加 botmux session/mention flags 的路径;复用 `_run/_extract` 保留模糊失败语义。
- [ ] **Step 4: 跑测试确认通过**:Run Step 2 命令 Expected: 全部 PASS。
- [ ] **Step 5: 全量回归并提交**:Run `python3 -m pytest tests/ -q` Expected: 全绿;`git commit -m "fix: publish factory seeds as native text"`。

### Task 6: 兜底 Skill、unit 模板与运维文档

**Files:**
- Modify: `skills/sa-router/SKILL.md`
- Modify: `systemd/sa.env.example`
- Create: `systemd/im-watcher.env.example`
- Create: `systemd/sa-im-watcher.service`
- Modify: `docs/p1/ops-runbook.md`
- Test: `tools/verify_skills.py`、`tests/test_fastpath_deployment.py`

**Interfaces:**
- Consumes: Tasks 1–5 的 HTTP、env、cursor 与发送命令。
- Produces: 可安装的 `sa-im-watcher.service`;完整部署、停止、drain/回滚文档;router 仅兜底且不能绕过 awaiting_go。

- [ ] **Step 1: 写/更新契约检查**:断言 router Skill 含 `awaiting_go`、`sactl confirm`、禁止直接派单;unit 使用 `/usr/bin/python3 tools/im_watcher.py`、`$SA_IM_WATCHER_ARGS`、Restart=always;env example 含全部必需变量且无真实 key/chat id。
- [ ] **Step 2: 跑检查确认失败**:Run `python3 tools/verify_skills.py` 及新增静态测试 Expected: FAIL(旧 Skill/缺 unit)。
- [ ] **Step 3: 更新 Skill 与配置模板**:happy path 明确由 im_watcher/coordinator 执行;Claude 仅在 fastpath 故障时做语义兜底,`/route` 后提示用户在 thread A 回开工,运维兜底用 `sactl confirm`;保留 Get/红线/结构化契约。
- [ ] **Step 4: 写部署与回滚手册**:配置 LLM env、control chat、cursor;先重启 coordinator 并检查 healthz,再 enable/start watcher;回滚前停 watcher、drain 或在 coordinator 停止后显式将 awaiting_go 迁回 received,再切旧代码;禁止带 awaiting_go 直接回滚。
- [ ] **Step 5: 验证并提交**:Run `python3 tools/verify_skills.py && python3 -m pytest tests/ -q` Expected: 7 个 Skill OK、全量全绿;`git commit -m "docs: deploy router fastpath with safe rollback"`。

### Task 7: 合并后生产部署与真机验收(需用户确认)

**Files:**
- Modify: `~/.config/super-agent/sa.env`(仅现场,不入库)
- Create: `~/.config/super-agent/im-watcher.env`(仅现场)
- Create: `docs/p1/router-fastpath-validation.md`

**Interfaces:**
- Consumes: 已评审并合并到 main 的 Tasks 1–6;生产 `registry/tasks.db`、sa-router profile、控制会话。
- Produces: 运行中的 sa-im-watcher;真实 Get/结论卡/开工/原生种子延迟与 message_id 证据。

- [ ] **Step 1: 停在合并门**:向用户展示最终评审和测试结果,取得合并 main、重启 sa-coordinator、安装并启动 sa-im-watcher 的明确确认;未确认不得执行。
- [ ] **Step 2: 部署**:从既有 DeepSeek 配置安全注入 env(不打印 key),安装 unit;先重启 coordinator、检查 `/healthz` 与全量测试,再启动 watcher;确认 cursor 从当前时间创建且不回放。
- [ ] **Step 3: 请用户发一条新的无歧义测试任务**:记录消息时间到 Get、结论卡时间;断言 Get ≤5s、结论卡正常 ≤10s、状态 awaiting_go、工场无新种子。
- [ ] **Step 4: 请用户在该 thread A 回“开工”**:断言只确认该任务、种子为原生 text、首行可见、mentions 与发送者正确;重复开工不重复派单。
- [ ] **Step 5: 验证失败恢复与旧兜底**:用 mock/故障注入覆盖 LLM 503 后重投;旧 `/route` 仅到 awaiting_go;真机不做破坏性 kill。
- [ ] **Step 6: 记录并提交验证文档**:包含真实时间线、状态、message_id、话题链接、回滚注意事项;Run `python3 -m pytest tests/ -q` Expected: 全绿;`git commit -m "docs: verify router fastpath in production"`。

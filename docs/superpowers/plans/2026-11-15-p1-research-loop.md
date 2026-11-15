# P1 调研闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成 6-bot 队伍与两个话题群,Router 控制会话接入 sa-coordinator,一个真实调研任务端到端无人值守跑通。

**Architecture:** 三段推进:先补 sa-coordinator 的 ingest/route/clarified HTTP 面(纯代码、TDD);再建队(6 bot、两群、per-bot 模型凭据、Drive 目录树与协作者授权,用户扫码/放行);最后 Router 会话接线与真实任务端到端演练。worker 会话由 botmux 按 @ 唤醒,状态唯一写者是 sa-coordinator(SQLite),所有外发走 sender(outbox 重试)。

**Tech Stack:** botmux 3.59.10(多 bot、send --session-id/--into)、herdr 0.9.5、Claude Code(botmux backend)、lark-cli 1.0.97(im/drive/docs)、Python stdlib coordinator(HTTP :8787)。

**Spec:** 在线飞书《Super-agent 个人落地方案 v1.2》(docx/CIicdWUcLoGXv2xAwgzcm9f8n8d);本地存档 `docs/2026-11-13-落地方案.md`(v1,拓扑以 v1.2 为准)。P0 实测事实见 `docs/p0/`。

## Global Constraints

- P1 退出条件(spec 原文):「一个真实调研任务端到端无人值守跑通:派单 → 双 bot 三轮讨论 → thread A 回投 → 首评/二次回复/Get/OK 均被正确感知且不会重复唤醒」。
- 模型绑定(v1.2 §模型与额度):Router=DeepSeek v4.1 flash;设计/Planner/Executor=GLM;评审/开发评审=Kimi。
- SQLite 单写者 = sa-coordinator;worker 不写库,只经 `sactl report` 上报。
- 发送实测约束(P0):`botmux send` 无 `--bot`/`--text`(正文是位置参数);身份经 `--session-id`(env `SA_BOTMUX_SESSION_ID`);线程回复必须 `--into`;Get 表情用 `lark-cli im reactions create`;话题群创建单次 ≤5 bot。
- 红线:禁止跨话题/跨群 @ 其他 bot;Router 会话零跨系统副作用(全部经 sactl/sa-coordinator);产物文档「谁产出、谁的身份创建」。
- 评论协议:bot 认可评论 → Get 表情(非文字);用户认可 → OK 表情;文字=继续讨论。
- 命名:taskid=日期+三位序号;文档标题 `[RD-yyyymmdd-nnn] 一句话标题 v版本`;任务目录 `yyyymmdd-nnn-英文短名`。
- 测试基线:`python3 -m pytest tests/ -q` 当前 145 全绿,每个代码任务结束时必须仍全绿。

## Review Focus

1. **用户消息重复投递**(botmux 重投/网络重试)→ `/ingest` 按 message_id 幂等返回既有 taskid,不得二次派单。测试:Task 1。
2. **Router verdict 畸形**(非 JSON、未知 task_type、title 空)→ `/route` 返回 422,任务滞留 `clarifying`,不半派单。测试:Task 2。
3. **thread A 回复误判**:澄清答复被当新任务 → `/clarified` 按 thread_a_msg_id 定位 active_task,判新任务时才走 ingest。测试:Task 3。
4. **sender 身份漂移**:多 bot 注册后种子/回投从错误 bot 发出 → 建群后断言种子消息发送者 = Router。验证:Task 8。
5. **会话模型错绑**(评审 bot 跑 GLM)→ 每 bot 固定文本回显时断言模型身份。验证:Task 6。

---

### Task 1: `/ingest` 端点与 `sactl ingest`

**Files:**
- Modify: `services/coordinator/cli.py`(HTTP Handler:do_POST 增加 `/ingest` 分支)
- Modify: `tools/sactl`(子命令 `ingest`)
- Test: `tests/test_ingest_endpoint.py`(新建)

**Interfaces:**
- Consumes: `Coordinator.ingest_task(...)`(已存在,coordinator.py:77)。
- Produces: `POST /ingest`,JSON body `{message_id, chat_id, root_msg_id, text, sender_open_id}`,响应 `{ok, taskid, duplicate}`;`sactl ingest --message-id ... --chat-id ... --root-msg-id ... --text ... [--sender-open-id ...]` 退出码 0=新建或重复。

- [ ] **Step 1: 写失败测试**:进程内起 Handler(参考 tests/ 现有 HTTP 测试模式),POST 两次同 message_id → 第一次 `duplicate=false`,第二次 `duplicate=true` 且 taskid 相同;缺 message_id → 422。

- [ ] **Step 2: 跑测试确认失败** Run: `python3 -m pytest tests/test_ingest_endpoint.py -q` Expected: FAIL(404)

- [ ] **Step 3: 实现 `/ingest`**:Handler 调 `ingest_task`,把 `duplicate` 透传;ingest 内部已做 Get 表情与 thread A 映射,不在端点重复。

- [ ] **Step 4: 跑测试通过** Run: 同上 Expected: PASS

- [ ] **Step 5: `sactl ingest` 子命令**:复用 sactl 现有 HTTP 客户端函数;打印 `{taskid, duplicate}`。

- [ ] **Step 6: 全量测试 + 提交** Run: `python3 -m pytest tests/ -q`(145+新增全绿)→ `git commit -m "feat: coordinator /ingest endpoint and sactl ingest"`

### Task 2: `/route` 端点、种子消息模板与 `sactl route`

**Files:**
- Modify: `services/coordinator/cli.py`(POST `/route`)
- Create: `services/coordinator/seed.py`(种子消息模板,渲染函数)
- Modify: `tools/sactl`(子命令 `route`)
- Test: `tests/test_route_endpoint.py`、`tests/test_seed_template.py`

**Interfaces:**
- Consumes: `Coordinator.dispatch(taskid, seed_text=..., mentions=...)`、`set_clarifying`;`task_types` 表(group_chat_id、roles、rounds_limit)。
- Produces: `POST /route` body `{taskid, ambiguous, clarify_question?, task_type?, title?}`;ambiguous=true → 调 `set_clarifying` 并返回 200 `{ok, state:"clarifying"}`;false → 校验 task_type 在注册表且 enabled → `render_seed()` → `dispatch()` → `{ok, state:"running"}`;任一字段非法 → 422。`render_seed(taskid, title, type_name, roles, rounds_limit) -> str`,模板 = spec §模板要点「种子消息」(标题行 `[taskid] 标题` / 任务简报占位 / 参与角色 / 完成协议 / 三轮规则提示)。
- `sactl route --taskid ... --json '<verdict>'`(verdict 即 sa-router skill 的结构化输出原样)。

- [ ] **Step 1: 写失败测试**:`render_seed` 断言含 `[` + taskid + `]` 标题行、roles 列表、`@Router` 完成协议、三轮提示;`/route` 三个分支 + 422(未知 task_type、title 空、ambiguous 且缺 clarify_question)。
- [ ] **Step 2: 确认失败** Run: `python3 -m pytest tests/test_route_endpoint.py tests/test_seed_template.py -q` Expected: FAIL
- [ ] **Step 3: 实现** seed.py 纯函数;cli.py 分支调 `set_clarifying`/`render_seed`/`dispatch`(dispatch 内部同事务入 outbox,不另发)。
- [ ] **Step 4: 通过** Run: 同上 Expected: PASS
- [ ] **Step 5: sactl route 子命令**(stdin 或 --json 传 verdict;退出码 0=已路由或已置澄清,3=422,4=不可达,对齐现有 sactl 语义)。
- [ ] **Step 6: 全量 + 提交** Run: `python3 -m pytest tests/ -q` → `git commit -m "feat: /route endpoint with seed template and sactl route"`

### Task 3: `/clarified` 端点与 `sactl clarified`

**Files:**
- Modify: `services/coordinator/cli.py`(POST `/clarified`)
- Modify: `tools/sactl`(子命令 `clarified`)
- Test: `tests/test_clarified_endpoint.py`

**Interfaces:**
- Consumes: `Coordinator.apply_clarified(taskid, answer_note)`(已存在)。
- Produces: `POST /clarified` body `{taskid, answer, is_new_task}`;is_new_task=false → `apply_clarified` 后任务回 pending_route,响应 `{ok, state:"pending_route"}`;is_new_task=true → 响应 `{ok, new_taskid}`(调 ingest 语义新建,原任务保持 clarifying 关闭)。`sactl clarified --taskid ... --answer ... [--new-task]`。

- [ ] **Step 1: 写失败测试**:两分支 + taskid 不存在 → 422;is_new_task 新任务 taskid 与原不同。
- [ ] **Step 2: 确认失败** Run: `python3 -m pytest tests/test_clarified_endpoint.py -q` Expected: FAIL
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 通过** → **Step 5: 全量 + 提交** `git commit -m "feat: /clarified endpoint and sactl clarified"`

### Task 4: 6 个 botmux bot(用户扫码)

**Files:**
- Modify: `~/.botmux/bots.json`(botmux setup add 写入,不手编)
- Create: `docs/p1/bot-fleet.md`(appId/名称/角色/模型/workingDir 登记表,不含 secret)

**Interfaces:**
- Consumes: 现有 Router bot `cli_aa561825aab8dbb5`(复用,不新建)。
- Produces: 新 app ×5:`sa-design`、`sa-review`、`sa-planner`、`sa-executor`、`sa-devreview`;每条 bots.json 记录含 `--model`、`--default-working-dir /home/ubuntu/workspace/super-agent/bots/<role>`、`--allowed-users 18611101148`。Router 现有条目改名 `--name sa-router`。

- [ ] **Step 1: 用户扫码建 5 app**:`botmux setup add --create-app --app-name sa-<role> --allowed-users 18611101148 --name sa-<role> --model <模型> --default-working-dir <绝对路径> --backend herdr`(模型:design/planner/executor=glm,review/devreview=kimi;首个 add 扫码,后续登录态内确认免扫码)。逐个执行,`--json` 确认 ok。
- [ ] **Step 2: Router 条目改名**:`botmux setup edit cli_aa561825aab8dbb5 --name sa-router --model deepseek-v4.1-flash`(以 `botmux status` 显示名为准)。
- [ ] **Step 3: 验证**:`botmux setup list` 6 条、路径正确;结果登记进 `docs/p1/bot-fleet.md`。
- [ ] **Step 4: 提交** `git commit -m "docs(p1): register bot fleet (6 bots)"`

### Task 5: per-bot 模型凭据(会话级 settings)

**Files:**
- Create: `bots/design/.claude/settings.json`、`bots/planner/.claude/settings.json`、`bots/executor/.claude/settings.json`(GLM env)、`bots/reviewer/.claude/settings.json`、`bots/devreviewer/.claude/settings.json`(Kimi env)、`bots/router/.claude/settings.json`(DeepSeek env)
- Modify: `~/.claude/settings.json`(移除全局 ANTHROPIC_* env 块,P0 的 GLM env 下沉到 3 个 GLM bot 目录)
- Test: `docs/p1/model-binding.md`(实测记录)

**Interfaces:**
- Consumes: `~/.config/ai/{glm,kimi,deepseek}.json` 的 env 块(与 P0 凭据修复同源)。
- Produces: 每个 bot 会话在读档 cwd=bots/<role> 时加载自己的 env;`{ "env": { "ANTHROPIC_BASE_URL": ..., "ANTHROPIC_AUTH_TOKEN": ... }, "model": ... }`(Kimi/DeepSeek 的 base_url/token 从对应 json 取)。

- [ ] **Step 1: 写 6 份 settings.json**(600 权限;先 Read 源 json 取 env 值,禁止把 token 写进仓库文档)。
- [ ] **Step 2: 移除全局 env 块**(保留 ~/.claude/settings.json 其余内容;改前备份 `settings.json.bak-p1`)。
- [ ] **Step 3: 实测验证**:每 bot 在测试话题 @ 一次,要求回复固定文本并附当前模型名(角色卡已含身份说明);断言:design/planner/executor 会话实际走 GLM、review/devreview 走 Kimi、router 走 DeepSeek(herdr attach 或会话自报 + `botmux logs --bot` 观察 provider)。失败项记录并修复后重测。
- [ ] **Step 4: 记录 + 提交** `docs/p1/model-binding.md` → `git commit -m "feat(p1): per-bot model credentials via session settings"`

### Task 6: 两个话题群与类型注册表回填

**Files:**
- Create: 群「调研工场」「开发工场」(botmux create-group)
- Modify: 生产库 `registry/tasks.db` task_types(group_chat_id 回填,经新 `sactl types set-group`)
- Test: `tests/test_sactl_types.py` + `docs/p1/groups.md`

**Interfaces:**
- Consumes: `botmux create-group --bot <appId> --chat-mode topic`(probe2 实测形态)。
- Produces: `sactl types set-group RD <chat_id>` / `RS <chat_id>` / `DEV <dev_chat_id>`;task_types.group_chat_id 落库;`docs/p1/groups.md` 登记 chat_id、成员清单。

- [ ] **Step 1: 写失败测试**:sactl types set-group/show 对临时库的读写断言。
- [ ] **Step 2: 失败 → 实现 sactl types 子命令**(直接 SQL UPDATE task_types,coordinator 不加端点;单写者原则不受影响——sactl 持库连接仅此刻)。
- [ ] **Step 3: 建群**:调研工场(sa-router+sa-design+sa-review+用户)、开发工场(sa-router+sa-planner+sa-executor+sa-devreview+用户);均 topic 模式;`chats.get` 回读 chat_mode=topic。
- [ ] **Step 4: 回填 RD/RS=调研工场、DEV=开发工场;sactl types show 断言**。
- [ ] **Step 5: 验证 sender 身份**:`SA_BOTMUX_SESSION_ID` 指向 Router 会话后,coordinator 向调研工场发一条测试顶层消息,飞书侧确认发送者=sa-router(Review Focus #4)。
- [ ] **Step 6: 双向互唤验证**(probe3 遗留):sa-design @ sa-review、sa-review @ sa-design 各一轮,双向同话题固定文本回复(`docs/p1/groups.md` 记录 message_id/thread_id)。
- [ ] **Step 7: 提交** `git commit -m "feat(p1): topic groups, types backfill, bidirectional wake verified"`

### Task 7: Drive 目录树、协作者授权与模板(用户放行)

**Files:**
- Create: 我的空间 `SuperAgent/{00-任务看板,01-方案调研,02-开发实现,09-归档,99-模板}` 目录树
- Create: 99-模板 6 份文档(方案/研究报告/Spec/自测报告/任务简报/种子消息)
- Test: `docs/p1/drive-setup.md`

**Interfaces:**
- Consumes: `lark-cli drive +create-folder`、`+import --type docx`(模板从本地 markdown);协作者授权 `lark-cli drive +member-add --perm edit`(Permission Grant 类,需用户 `!` 自跑或 UI 操作)。
- Produces: `01-方案调研` 协作者=sa-design(edit);`02-开发实现` 协作者=sa-planner、sa-executor(edit);模板 token 登记表。

- [ ] **Step 1: 建目录树**(user 身份;按 spec §目录结构逐层 create-folder)。
- [ ] **Step 2: 写 6 份模板 markdown**(内容=spec §模板要点逐项)→ `+import --type docx` 入 99-模板,同位置串行。
- [ ] **Step 3: 用户放行协作者授权**:给出精确命令(`+member-add --token <01-方案调研 token> --type folder --member-id <sa-design bot open_id> --perm edit --as user --yes` 等 3 条),用户 `!` 前缀自跑或 UI 手动加;跑不了就整 Task 标 BLOCKED,不伪造通过。
- [ ] **Step 4: 验证权限继承**:sa-design 身份在 01-方案调研 建测试文档 → user 能读、能评论 → bot 能回评(probe #4 未竟部分一并收口)→ 测试文档移入 09-归档。
- [ ] **Step 5: 记录 + 提交** `git commit -m "feat(p1): drive tree, templates, collaborator grants verified"`

### Task 8: sa-router skill 接线与控制会话验证

**Files:**
- Modify: `skills/sa-router/SKILL.md`(固定动作改为三步:sactl ingest → 语义判定 → sactl route;澄清答复循环加 sactl clarified)
- Test: `python3 tools/verify_skills.py` + `docs/p1/router-session.md`

**Interfaces:**
- Consumes: Task 1–3 的 sactl 子命令;Router bot 单聊会话(botmux)。
- Produces: skill 固定动作:① `sactl ingest --message-id ... --chat-id ... --root-msg-id ... --text ...` 取 taskid;② 歧义 → `sactl route --taskid --json '{"ambiguous":true,...}'`;无歧义 → route 完整 verdict;③ 用户澄清回复 → `sactl clarified`。

- [ ] **Step 1: 更新 SKILL.md**(保留红线;结构化输出示例同步为 sactl 调用序列)。
- [ ] **Step 2: 校验** Run: `python3 tools/verify_skills.py` Expected: PASS(契约校验)
- [ ] **Step 3: 真机验证**:用户私聊 sa-router 发一条测试任务 → 观察:Get 表情出现 → 澄清或直接派单到调研工场(话题建立、@sa-design)→ thread A 收到回投(含话题链接)。全程 `sactl task show` 状态断言。测试任务走完验收后 done。
- [ ] **Step 4: 记录 + 提交** `git commit -m "feat(p1): sa-router wired to sactl ingest/route/clarified"`

### Task 9: 评论 watcher 生产配置

**Files:**
- Modify: `~/.config/super-agent/watcher.env`(--bot-id 改为 sa-design open_id;文档根目录指向 SuperAgent/)
- Test: `docs/p1/watcher-config.md`

**Interfaces:**
- Consumes: `tools/comment_watcher.py` 现有参数(P0 已实测:签名跳过、自过滤、去重)。
- Produces: watcher 以 sa-design 为责任 bot 运行;监控范围覆盖非 done 调研文档。

- [ ] **Step 1: 更新 watcher.env 并重启 unit** `systemctl --user restart sa-comment-watcher`。
- [ ] **Step 2: 验证**:在 Task 8 测试任务的文档上发一条真实评论 → watcher 转达到对应话题并 @sa-design → sa-design 以 bot 身份回评;用户点 OK → is_solved;无重复转达(journal 观察)。
- [ ] **Step 3: 记录 + 提交** `git commit -m "feat(p1): comment watcher production config"`

### Task 10: 真实调研任务端到端 + P1 退出条件核验

**Files:**
- Create: `docs/p1/2026-11-15-e2e-validation.md`
- Test: 全部 P1 退出条件逐条

**Interfaces:**
- Consumes: 全部前置任务。
- Produces: 一次真实任务全流程记录(用户出题)。

- [ ] **Step 1: 用户给真实调研任务题目**,私聊 sa-router 发出。
- [ ] **Step 2: 无人值守观察**(不手动干预;Watchdog 视角记录时间线):派单 → 认领消息+进展楼 → 方案文档落地(01-方案调研,sa-design 身份)→ @sa-review 三轮讨论(意见/回应各一轮起)→ 分歧有则走分歧卡片+升级,无则 sactl report → thread A 回投。
- [ ] **Step 3: 评论回路核验**:用户首评 → bot 文字回复;二次回复 → 继续;bot 认可点 Get;用户 OK → is_solved;全程无重复唤醒(inbox/comment-inbox 断言)。
- [ ] **Step 4: 状态机终态断言**:received→running→report_pending→awaiting_user_review→(in_doc_review↔)→done;inbox/outbox 无未决行。
- [ ] **Step 5: 退出条件清单逐条打勾**(引用 message_id/doc 链接为证),`git commit -m "docs(p1): research loop e2e validated"`

### Task 11: 收口

- [ ] **Step 1: README 更新**(P1 现状小节:队伍表、群、端点、运维命令)。
- [ ] **Step 2: 在线方案文档 v1.2 追加 P1 验证记录**(附录 B 增补,经用户确认后写入)。
- [ ] **Step 3: 提交 + push** `git commit -m "docs: p1 wrap-up"` → `git push origin main`

---

## 用户前置动作(阻塞点提示)

1. **扫码**(Task 4):首次 `botmux setup add --create-app` 需飞书扫码;后续 4 个免扫码。
2. **目录协作者授权**(Task 7):3 条 `+member-add` 命令需用户 `!` 自跑或 UI 操作(权限分类器拦 Claude 执行)。
3. **真实任务题目**(Task 10):一个你真想做的调研题。
4. 可选:容器重启验证(P0 遗留)随时可做,不阻塞 P1。

## Self-Review 结论

- 覆盖检查:v1.2 P1 相关条目(6 bot、两群、bots.json、Router 接入、真实任务、评论回路、双向互唤遗留)均有对应任务;probe #4 收口并入 Task 7 Step 4。开发工场 bot/群在本期建好但仅冒烟(P2 才全量),符合 foundation 计划的 P1 范围。
- 已知留白(有意):sa-planner/executor/devreview 的 Skill 深化、Spec 流程、P2 故障注入——P2 计划再写。

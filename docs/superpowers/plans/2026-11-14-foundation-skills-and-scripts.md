# 基础层:sa-coordinator + sactl + 评论 watcher + 角色 Skill 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地方案 v1.2 的确定性基础层——sa-coordinator(SQLite 唯一写者:inbox/outbox/状态机/sender/reconciler)、sactl 上报 CLI、评论 watcher 轮询降级、7 个角色 Skill 与验证脚本,全部本地可测,不依赖线上飞书环境。

**Architecture:** 单实例 Python 薄控制服务:sa-coordinator 常驻 HTTP(localhost)接收 sactl report,内部以 SQLite 单连接事务保证「状态迁移 + outbox 入队」原子提交;所有飞书副作用收敛到 sender/reader 适配器,argv 模板可配置(P0 实测调参不改码)。Skill 为纯提示词,由 verify_skills.py 做结构化验证并与 report 字段契约交叉校验。

**Tech Stack:** Python 3.12+(本机 3.14.4),运行时零第三方依赖(sqlite3 / http.server / argparse / uuid / hashlib / urllib.request / subprocess);测试 pytest 9.2.0(已装)。Skill 为 Claude Code 标准格式(目录 + SKILL.md)。

**Spec:** 飞书《Super-agent 个人落地方案 v1.2》(在线最新);本地存档 `docs/2026-11-13-落地方案.md`(v1);摘要 `README.md`。计划从 spec 的 D2/D3/D4/D5、「Router 层设计」「Worker 层设计」「任务登记表」「自建 Skill 清单」「P0 验证清单」各节推导。

## Global Constraints

- 运行时仅用 Python 标准库;开发依赖仅 pytest。测试命令统一 `python3 -m pytest tests/ -q`。
- 登记表 `registry/tasks.db` 只有 sa-coordinator 进程写;sactl / comment_watcher / 角色 bot 一律走 HTTP 或适配器接口,禁止直写库。
- taskid = `yyyymmdd-NNN`(当日序号 000–999,每日重置);文档标题 `[类型码-taskid] 一句话标题 v版本`;类型码 RD / RS / DEV。
- 所有飞书副作用(send/reaction/评论读取)集中在适配器,命令模板经环境变量可覆盖(默认值写死在代码,字段名对齐 botmux/lark-cli 现有参数:`--chat-id` / `--top-level` / `--reply-to` / `--mention`)。
- CAS 语义:tasks 表 `version` 列即 spec 的 expected_version 令牌;`UPDATE ... WHERE taskid=? AND version=?`,成功后 version+1。
- 三轮规则:分歧满 3 轮必须 `sactl report --outcome escalated`,禁止第 4 轮;轮数上限存 task_types.rounds_limit,默认 3。
- 代码标识符英文,注释与 docstring 中文;每个 Task 结束 commit,conventional commits(feat/test/docs)。
- 仓库布局对齐 README「仓库布局(目标形态)」:`services/coordinator/`、`tools/`、`skills/`、`bots/`、`registry/`、`tasks/`。

## Review Focus

1. **飞书事件重投**(断线重连后同 event_id / 同 message_id 再次到达)→ 不得重复建任务、不得重复派单(inbox 双唯一约束;Task 4/7 测试)。
2. **report 重试幂等**(sactl 超时重发、coordinator 处理中途被 kill 后同 event_id 再来)→ thread A 不重复回投、状态不二次迁移(Task 7/9 测试)。
3. **派单结果不确定**(发送超时但消息可能已到)→ 重发前必须先对账(按 taskid 检索群内已有种子),禁止盲目重发造出重复话题(Task 6/7 测试)。
4. **同日并发分配 taskid** → 唯一主键冲突重试,不产生重复 taskid(Task 3 测试)。
5. **评论 watcher 自回复死循环**(把责任 bot 自己的回复/表情当新评论再分发)→ 按 bot id 过滤(Task 10 测试)。

---

### Task 1: 仓库脚手架 + DB schema(db.py)

**Files:**
- Create: `pyproject.toml`(pytest 配置,`testpaths=["tests"]`)
- Create: `Makefile`(target:`test`)
- Create: `.gitignore`(`__pycache__/`、`registry/*.db*`、`tasks/`、`.venv/`)
- Create: `services/coordinator/__init__.py`(空)
- Create: `services/coordinator/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `db.connect(path: str) -> sqlite3.Connection`(自动 `PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON`)、`db.init_schema(conn) -> None`(幂等,建全部 6 张表)、`db.SCHEMA_VERSION = 1`。
- 表结构(本任务锁定,后续任务只读):
  - `tasks(taskid TEXT PRIMARY KEY, parent_task_id TEXT, type TEXT NOT NULL, title TEXT NOT NULL, thread_a_msg_id TEXT, group_chat_id TEXT, topic_root_msg_id TEXT, doc_links TEXT DEFAULT '[]', status TEXT NOT NULL, phase TEXT, active_role TEXT, version INTEGER NOT NULL DEFAULT 0, rounds_by_phase TEXT DEFAULT '{}', decisions TEXT DEFAULT '[]', skill_versions TEXT DEFAULT '{}', model TEXT, repo_commit TEXT, worktree TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)`
  - `inbox_events(event_id TEXT PRIMARY KEY, message_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, taskid TEXT, payload TEXT NOT NULL, received_at TEXT NOT NULL)`
  - `outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, taskid TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_retry_at TEXT, sent_msg_id TEXT, needs_reconcile INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)`
  - `comment_inbox(reply_id TEXT NOT NULL, content_hash TEXT NOT NULL, kind TEXT NOT NULL, taskid TEXT, doc_token TEXT NOT NULL, payload TEXT NOT NULL, first_seen_at TEXT NOT NULL, PRIMARY KEY(reply_id, content_hash, kind))`
  - `bot_ownership(bot_app_id TEXT PRIMARY KEY, owner_instance TEXT NOT NULL, lease_token TEXT, lease_expires_at TEXT, updated_at TEXT NOT NULL)`
  - `task_types(type_code TEXT PRIMARY KEY, name TEXT NOT NULL, group_chat_id TEXT, roles TEXT NOT NULL, doc_template TEXT, rounds_limit INTEGER NOT NULL DEFAULT 3, needs_doc INTEGER NOT NULL DEFAULT 1, can_dev INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1)`
  - 附:`seed_task_types(conn)` 写入 RD / RS / DEV 三行默认配置(group_chat_id 为空,待 P0 实测后填)。

- [ ] **Step 1: 写失败测试** `tests/test_db.py`:`test_init_schema_idempotent`(对同一临时库连续两次 init_schema 不报错,`sqlite_master` 中 6 张表齐全)、`test_wal_and_fk_pragmas`(connect 后 `journal_mode` 为 wal)、`test_seed_task_types`(RD/RS/DEV 三行存在,RD.can_dev=1,RS.can_dev=0)。
- [ ] **Step 2: 跑测试确认失败** Run: `python3 -m pytest tests/test_db.py -q`,Expected: FAIL(ModuleNotFoundError / 表不存在)。
- [ ] **Step 3: 实现 db.py** 纯 DDL 常量 + connect/init_schema/seed_task_types,无业务逻辑。
- [ ] **Step 4: 跑测试确认通过** Run: `python3 -m pytest tests/test_db.py -q`,Expected: PASS。
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: repo scaffold and coordinator db schema"`

### Task 2: 状态机(state.py)

**Files:**
- Create: `services/coordinator/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: 无(纯函数)。
- Produces: `STATUS = {"received","clarifying","running","report_pending","awaiting_decision","done"}`;`TERMINAL = {"done"}`;`TRANSITIONS: dict[str, set[str]]`(received→{clarifying,running};clarifying→{running};running→{report_pending};report_pending→{running,done,awaiting_decision};awaiting_decision→{running};done→{});`validate_transition(status_from: str, status_to: str) -> None`(非法时抛 `InvalidTransition(status_from, status_to)`);`is_terminal(status: str) -> bool`。

- [ ] **Step 1: 写失败测试**:合法迁移逐条放行(穷举 TRANSITIONS 全部边);非法迁移(received→done、done→running、running→awaiting_decision)抛 InvalidTransition;is_terminal 判定。
- [ ] **Step 2: 跑测试确认失败** Run: `python3 -m pytest tests/test_state.py -q`,Expected: FAIL。
- [ ] **Step 3: 实现 state.py**(纯字典 + 校验函数,无 IO)。
- [ ] **Step 4: 跑测试确认通过** Run: `python3 -m pytest tests/test_state.py -q`,Expected: PASS。
- [ ] **Step 5: Commit** `git commit -m "feat: task status state machine"`

### Task 3: 登记表 registry.py(taskid 分配 + CAS)

**Files:**
- Create: `services/coordinator/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `db.connect/init_schema/seed_task_types`、`state.validate_transition`。
- Produces:
  - `@dataclass Task`:字段与 tasks 表列一一对应(同名);`Task.doc_links -> list`、`Task.decisions -> list`、`Task.rounds_by_phase -> dict`(dataclass 字段为已 json 反序列化类型)。
  - `allocate_taskid(conn, today: str) -> str`:today 格式 `yyyymmdd`;返回 `yyyymmdd-NNN`;单事务内 `SELECT MAX(taskid) WHERE taskid LIKE today||'-%'` 递增,撞主键(IntegrityError)最多重试 3 次。
  - `create_task(conn, *, type: str, title: str, message_id: str, event_id: str, thread_a_msg_id: str | None, parent_task_id: str | None = None) -> Task`:status=received,version=0,同事务写 inbox_events(kind='task');type 不在 task_types 或 enabled=0 时抛 `UnknownTaskType`。
  - `get_task(conn, taskid: str) -> Task | None`;`list_non_terminal(conn) -> list[Task]`。
  - `cas_update(conn, taskid: str, expected_version: int, *, status: str | None = None, **fields) -> Task`:先 `validate_transition`(仅当 status 变更时);`UPDATE tasks SET ..., version=version+1, updated_at=? WHERE taskid=? AND version=?`,rowcount=0 抛 `StaleVersion(taskid, expected_version)`;成功返回新 Task。**fields 名必须是 Task dataclass 字段,doc_links/decisions/rounds_by_phase 自动 json 序列化。**
  - `append_decision(conn, taskid: str, expected_version: int, decision: dict) -> Task`(复用 cas_update,向 decisions 追加)。

- [ ] **Step 1: 写失败测试**:taskid 当日递增(001→002)与跨日重置(昨日 005 → 今日 001);create_task 落库且字段可读回;cas_update 正确迁移 + version+1;旧 version 更新抛 StaleVersion;非法 status 抛 InvalidTransition;UnknownTaskType;append_decision 追加不覆盖;**并发分配**(两个连接同日各分配 10 次,结果 20 个互不重复)。
- [ ] **Step 2: 跑测试确认失败** Run: `python3 -m pytest tests/test_registry.py -q`,Expected: FAIL。
- [ ] **Step 3: 实现 registry.py**。
- [ ] **Step 4: 跑测试确认通过** Run: `python3 -m pytest tests/test_registry.py -q`,Expected: PASS。
- [ ] **Step 5: Commit** `git commit -m "feat: task registry with taskid allocation and cas updates"`

### Task 4: inbox 事件去重(inbox.py)

**Files:**
- Create: `services/coordinator/inbox.py`
- Test: `tests/test_inbox.py`

**Interfaces:**
- Consumes: db schema。
- Produces: `claim_event(conn, *, event_id: str, message_id: str, kind: str, payload: dict) -> ClaimResult`,其中 `@dataclass ClaimResult(new: bool, taskid: str | None, prev_taskid: str | None)`:新事件 INSERT 成功 new=True;event_id 或 message_id 任一撞唯一约束 → new=False 且返回已存在的 taskid(prev_taskid)。payload 存 JSON 字符串。

- [ ] **Step 1: 写失败测试**:首次 claim new=True;同 event_id 重复 → new=False;同 message_id 不同 event_id → new=False;kind='report' 与 kind='task' 同表共存(不同 event_id 均可 claim)。
- [ ] **Step 2: 跑测试确认失败** Run: `python3 -m pytest tests/test_inbox.py -q`,Expected: FAIL。
- [ ] **Step 3: 实现 inbox.py**(IntegrityError 捕获后按约束名区分回查)。
- [ ] **Step 4: 跑测试确认通过** Run: `python3 -m pytest tests/test_inbox.py -q`,Expected: PASS。
- [ ] **Step 5: Commit** `git commit -m "feat: inbox event dedup by event/message id"`

### Task 5: outbox 队列(outbox.py)

**Files:**
- Create: `services/coordinator/outbox.py`
- Test: `tests/test_outbox.py`

**Interfaces:**
- Consumes: db schema。
- Produces:
  - `enqueue(conn, *, taskid: str, kind: str, payload: dict) -> int`(kind ∈ {dispatch, relay, decision, notify, reaction};payload JSON;status=pending)。
  - `mark_sent(conn, outbox_id: int, sent_msg_id: str) -> None`;`mark_failed_attempt(conn, outbox_id: int, next_retry_at: str, needs_reconcile: bool = False) -> None`(attempts+1);`pending_due(conn, now: str) -> list[OutboxRow]`(status=pending 且 next_retry_at IS NULL 或 ≤ now,按 id 升序)。
  - `@dataclass OutboxRow`(与表列同名,payload 为 dict)。

- [ ] **Step 1: 写失败测试**:enqueue→pending_due 可见;mark_sent 后不再出现;mark_failed_attempt 后 next_retry_at 未到不可见、到点可见;needs_reconcile 标记持久化。
- [ ] **Step 2: 跑测试确认失败** Run: `python3 -m pytest tests/test_outbox.py -q`,Expected: FAIL。
- [ ] **Step 3: 实现 outbox.py**。
- [ ] **Step 4: 跑测试确认通过** Run: `python3 -m pytest tests/test_outbox.py -q`,Expected: PASS。
- [ ] **Step 5: Commit** `git commit -m "feat: persistent outbox queue"`

### Task 6: 飞书发送适配器(sender.py)

**Files:**
- Create: `services/coordinator/sender.py`
- Test: `tests/test_sender.py`

**Interfaces:**
- Consumes: 无(独立适配层)。
- Produces:
  - `@runtime_checkable class Sender(Protocol)`:`send_top_level(chat_id: str, text: str, mentions: tuple[str, ...] = ()) -> str`、`send_reply(chat_id: str, root_msg_id: str, text: str, mentions: tuple[str, ...] = ()) -> str`、`add_reaction(chat_id: str, msg_id: str, emoji: str) -> None`。返回/使用飞书 message_id。
  - `class LarkCliSender(Sender)`:构造参数 `sender_bot: str`(botmux bot 名,默认 router)+ 命令模板取自模块常量,常量可被环境变量覆盖(`SA_SEND_CMD` / `SA_REACTION_CMD`,shlex.split,占位符 `{bot}` `{chat_id}` `{text}` `{root}` `{msg_id}` `{emoji}` `{mentions}`,mentions 以逗号拼接)。默认模板:`botmux send --bot {bot} --chat-id {chat_id} --text {text} [--mention]`;`--top-level` / `--reply-to {root}` 按方法拼加;reaction 走 `lark-cli im`(模板默认 `lark-cli im message add-reaction --chat-id {chat_id} --message-id {msg_id} --emoji {emoji}`)。
  - 异常:`SendError(cmd, rc, stderr)`;子类 `AmbiguousSendError`(子进程超时/被杀——消息可能已发出,调用方必须走对账,不得直接重发)。
  - `parse_send_output(stdout: str) -> str | None`:从 botmux/lark-cli JSON 输出提取 message_id(键名 `message_id` 或 `data.message_id`,两者都试)。

- [ ] **Step 1: 写失败测试**(fake subprocess:monkeypatch `subprocess.run`):正常发送返回 msg_id(两种输出格式);rc≠0 抛 SendError;超时抛 AmbiguousSendError;模板环境变量覆盖生效;mentions 为空时不拼 --mention 参数。
- [ ] **Step 2: 跑测试确认失败** Run: `python3 -m pytest tests/test_sender.py -q`,Expected: FAIL。
- [ ] **Step 3: 实现 sender.py**(subprocess.run timeout=30;文本一律作为单参数传递,不经 shell)。
- [ ] **Step 4: 跑测试确认通过** Run: `python3 -m pytest tests/test_sender.py -q`,Expected: PASS。
- [ ] **Step 5: Commit** `git commit -m "feat: lark sender adapter with ambiguous-failure semantics"`

### Task 7: 协调器核心(coordinator.py)

**Files:**
- Create: `services/coordinator/report.py`(纯校验)、`services/coordinator/coordinator.py`
- Test: `tests/test_report.py`、`tests/test_coordinator.py`

**Interfaces:**
- Consumes: Task 2–6 全部接口。
- Produces:
  - report.py:`REPORT_FIELDS = ("event_id","taskid","outcome","phase","source_msg_id","doc_version","payload")`;`OUTCOMES = ("completed","escalated")`;`validate_report(event: dict) -> None`(必填字段齐全、outcome 合法、taskid 非空,否则抛 `InvalidReport(原因)`,payload 可缺省为 {})。
  - `class Coordinator`:`__init__(self, conn, sender: Sender, now: Callable[[], str] = utcnow_iso, probe: MsgProbe | None = None)`;
    - `ingest_task(*, event_id, message_id, text: str, task_type: str, title: str) -> IngestResult`:claim_event 去重(重复直接返回既有 taskid,不再 Get/派单);新事件 → create_task + add_reaction(Get)+ enqueue(kind='reaction' 由 sender 失败不阻断,仅记日志);返回 taskid。**本方法不做派单**(dispatch 单独调用,便于 clarifying 插入)。
    - `dispatch(taskid: str, *, seed_text: str, mentions: tuple[str, ...]) -> None`:CAS received→running 前,先 enqueue(kind='dispatch', payload 含 chat_id/seed_text/mentions,同事务 CAS received→running…见 Step 3 说明);实际发送由 `process_due_outbox` 完成,成功后把返回 msg_id 写回 `topic_root_msg_id`。
    - `handle_report(event: dict) -> dict`:validate_report → claim_event(kind='report', event_id 去重,重复返回 `{"accepted": False, "duplicate": True}`)→ cas_update running→report_pending(带 phase/active_role 更新;payload.rounds 非空时并入 `rounds_by_phase[phase]`,供三轮规则审计)→ enqueue(kind='relay', payload 含 outcome 摘要模板与 doc 链接);返回 `{"accepted": True, "taskid":..., "status": "report_pending"}`。
    - `relay_decision(taskid: str, decision_text: str) -> None`:CAS awaiting_decision→running + append_decision + enqueue(kind='decision', 发到 topic_root 下 @active_role)。
    - `set_clarifying(taskid: str, question: str) -> None` / `apply_clarified(taskid: str, answer_note: str) -> None`:clarifying↔running 迁移 + thread A 提问/继续 的 outbox。
    - `process_due_outbox(now: str | None = None) -> int`:遍历 pending_due;needs_reconcile 或 kind='dispatch' 且 attempts>0 的行,先调 `probe.find_seed(group_chat_id, taskid)`(MsgProbe 协议,注入;找到 → 视为已发送,mark_sent)再发送;SendError → mark_failed_attempt(next_retry_at=now+60s);AmbiguousSendError → mark_failed_attempt(needs_reconcile=True);成功 → mark_sent + 按 kind 落后续状态:relay+completed→CAS report_pending→done;relay+escalated→CAS report_pending→awaiting_decision;dispatch→写 topic_root_msg_id。返回处理行数。
  - `@runtime_checkable class MsgProbe(Protocol)`:`find_seed(group_chat_id: str, taskid: str) -> str | None`(返回已存在种子消息 id;Phase A 无线上实现,默认 None)。

- [ ] **Step 1: 写失败测试 test_report.py**:合法报文通过;缺 event_id / 缺 taskid / outcome 非法 → InvalidReport。
- [ ] **Step 2: 写失败测试 test_coordinator.py**(FakeSender 记录调用):
  - 重投:同 (event_id, message_id) 二次 ingest_task → 同一 taskid,FakeSender.add_reaction 只调了一次。
  - 派单:dispatch 后 process_due_outbox → FakeSender.send_top_level 收到 seed;tasks.topic_root_msg_id 写回;status=running。
  - **派单模糊失败**:FakeSender 第一次抛 AmbiguousSendError → 行标 needs_reconcile;再次 process_due_outbox 前 probe.find_seed 返回已存在 id → 不再发送,mark_sent 收敛(不产生第二个话题)。
  - report 主线:handle_report(completed) → report_pending;process_due_outbox 发 relay → status=done;escalated → awaiting_decision;**同 event_id 二次 handle_report → duplicate=True 且 FakeSender 只发过一次 relay**;**relay 发送后、CAS done 前崩溃模拟**:直接再跑 process_due_outbox(行已 mark_sent)→ 不再发送。
  - relay_decision:awaiting_decision→running,decision 文本含拍板内容并 @active_role。
- [ ] **Step 3: 实现两个模块**。派单事务顺序锁定:同一事务内「enqueue(dispatch) + cas_update(received→running, active_role=首个 mention)」,发送在事务外由 process_due_outbox 承担——崩溃恢复时以「status=running 且 dispatch 行 pending」为准,重发前必经 probe 对账。relay 摘要文案锁定:「任务 [taskid] <completed|escalated>:」+ payload.summary(缺省取 title)。
- [ ] **Step 4: 跑全部测试确认通过** Run: `python3 -m pytest tests/ -q`,Expected: PASS(含此前任务)。
- [ ] **Step 5: Commit** `git commit -m "feat: coordinator core with report intake and outbox processing"`

### Task 8: HTTP 服务与 CLI(cli.py)+ reconciler.py

**Files:**
- Create: `services/coordinator/reconciler.py`、`services/coordinator/cli.py`、`services/coordinator/__main__.py`
- Test: `tests/test_http.py`、`tests/test_reconciler.py`

**Interfaces:**
- Consumes: Coordinator。
- Produces:
  - `serve(host: str = "127.0.0.1", port: int = 8787, db_path: str = "registry/tasks.db") -> None`:ThreadingHTTPServer;路由 `POST /report`(JSON → handle_report;InvalidReport→422;重复→200 duplicate;未知 taskid→422)、`GET /healthz` → `{"ok":true}`、`GET /tasks?status=running` → 只读列表。单写者护栏:启动时创建 `registry/coordinator.lock`(O_CREAT|O_EXCL,写入 pid;已存在且 pid 存活 → 拒绝启动)。后台线程每 5s 调 process_due_outbox。
  - `reconciler.reconcile(conn, sender, now) -> ReconcileReport`:`@dataclass ReconcileReport(non_terminal: int, resent: int, stuck: list[dict])`;启动扫描:① outbox 全部 pending/needs_reconcile 行交 process_due_outbox;② running 超 4h / report_pending 超 10min / awaiting_decision 超 24h 未变更 → 记入 stuck(只报告不改状态)。
  - CLI:`python3 -m services.coordinator serve|once|init-db`;`once` = reconcile + process_due_outbox 单次执行(systemd timer 兼容形态);`init-db` 建库 + seed。

- [ ] **Step 1: 写失败测试 test_http.py**(进程内起 ephemeral 端口):POST 合法 report → 200 accepted;同 event_id 重发 → 200 duplicate;缺字段 → 422;GET /healthz → ok。test_reconciler.py:构造超时任务 → stuck 命中三类各一;pending outbox 被重发(计入 resent)。
- [ ] **Step 2: 跑测试确认失败** Run: `python3 -m pytest tests/test_http.py tests/test_reconciler.py -q`,Expected: FAIL。
- [ ] **Step 3: 实现 reconciler.py + cli.py**。
- [ ] **Step 4: 跑测试确认通过** Run: `python3 -m pytest tests/ -q`,Expected: PASS。
- [ ] **Step 5: Commit** `git commit -m "feat: coordinator http service, cli and reconciler"`

### Task 9: sactl 上报 CLI(tools/sactl)

**Files:**
- Create: `tools/sactl`(可执行 python 脚本,shebang `#!/usr/bin/env python3`)
- Test: `tests/test_sactl.py`

**Interfaces:**
- Consumes: `POST /report`(Task 8)。
- Produces: 子命令 `report --taskid TASKID --outcome completed|escalated [--phase P] [--source-msg-id ID] [--doc-version V] [--payload-file F] [--url URL]`;`task show TASKID [--url URL]`。默认 URL 取环境变量 `SA_COORDINATOR_URL`(缺省 `http://127.0.0.1:8787`)。行为:event_id 用 `uuid4` 生成一次并跨重试复用;重试 3 次指数退避(1/2/4s);退出码 0=accepted 或 duplicate、3=422、4=不可达。`--payload-file` 读 JSON(escalated 时装分歧卡片:每张含 viewpoint_a/viewpoint_b/evidence/impact/suggestion)。

- [ ] **Step 1: 写失败测试**(monkeypatch urllib.request.urlopen + 计数 fake):正常 → exit 0、payload 字段齐全;服务端 422 → exit 3;前两次 500 第三次 200 → exit 0 且 event_id 三次相同;全失败 → exit 4。
- [ ] **Step 2: 跑测试确认失败** Run: `python3 -m pytest tests/test_sactl.py -q`,Expected: FAIL。
- [ ] **Step 3: 实现 tools/sactl**(仅标准库)。
- [ ] **Step 4: 跑测试确认通过** Run: `python3 -m pytest tests/test_sactl.py -q`,Expected: PASS。
- [ ] **Step 5: Commit** `git commit -m "feat: sactl report cli with idempotent retry"`

### Task 10: 评论 watcher 降级(tools/comment_watcher.py)

**Files:**
- Create: `tools/comment_watcher.py`
- Test: `tests/test_comment_watcher.py`、`tests/fixtures/comments_page1.json`、`tests/fixtures/comments_page2.json`(含 reply_list 分页、reaction、is_solved、bot 自回复)

**Interfaces:**
- Consumes: db schema(comment_inbox)、outbox.enqueue。
- Produces:
  - `@runtime_checkable class CommentReader(Protocol)`:`list_comments(doc_token: str, page_token: str | None) -> dict`(飞书 comments 列表原始 JSON,含 `data.items` / `data.page_token`)、`get_comment(doc_token: str, comment_id: str) -> dict`(取 reply_list 完整分页与 reactions、is_solved)。
  - `LarkCliCommentReader`:argv 模板默认 `lark-cli drive +list-comments --doc {doc_token}`,env `SA_COMMENT_LIST_CMD` 可覆盖;输出按 JSON 解析。
  - `sync_doc(conn, reader, *, doc_token: str, taskid: str, bot_ids: tuple[str, ...]) -> int`:`build_comment_index` 展开评论与 reply 分页 → 逐条 `dedup_key(reply_id, content, update_time)` → 撞库跳过 → **user_id ∈ bot_ids 的回复跳过**(防自回复死循环)→ 新条目写 comment_inbox + enqueue(kind='notify', 转发到 topic);reaction/is_solved 单独 kind 记录不入 notify。返回新增数。
  - `watch_loop(conn, reader, docs: Callable[[], list[dict]], *, active_interval: float = 3.0, idle_interval: float = 60.0, ...)`:活动/空闲分频循环骨架(有非 done 任务用 active_interval,否则 idle_interval;连续异常指数退避),单轮异常不退出。**Phase A 只测单轮逻辑,循环仅冒烟。**
  - `dedup_key(reply_id: str, content: str, update_time: str) -> tuple[str, str]`:content_hash = sha256(content + update_time) 前 16 位十六进制。

- [ ] **Step 1: 写失败测试**:两页 fixture 全量展开条数正确;二次 sync 新增数为 0(去重);bot 自回复被过滤(不产生 notify);用户二次回复产生新 notify;reaction 只记 comment_inbox 不产生 notify;内容更新(hash 变化)产生新条目。
- [ ] **Step 2: 跑测试确认失败** Run: `python3 -m pytest tests/test_comment_watcher.py -q`,Expected: FAIL。
- [ ] **Step 3: 实现 comment_watcher.py**。
- [ ] **Step 4: 跑测试确认通过** Run: `python3 -m pytest tests/test_comment_watcher.py -q`,Expected: PASS。
- [ ] **Step 5: Commit** `git commit -m "feat: comment polling fallback watcher with dedup and self-reply filter"`

### Task 11: P0 探针脚本(tools/probe/)

**Files:**
- Create: `tools/probe/probe_comment_event.py`、`tools/probe/probe_config.json`、`tools/probe/README.md`
- Test: `tests/test_probe_dryrun.py`

**Interfaces:**
- Produces:
  - `probe_comment_event.py --config probe_config.json [--dry-run]`:五步编排:① 建测试文档(docs +create);② 订阅 `drive.notice.comment_add_v1`(POST /open-apis/drive/v1/user/subscription);③ 以 user 身份发首评;④ 以 user 身份二次回复;⑤ 监听窗口(lark-cli event consume,--timeout N)捕获 NDJSON 并断言收到可关联事件。每步外部命令都来自 probe_config.json 的 `steps[].argv` 模板(占位符 `{doc_token}` 等),`--dry-run` 只渲染打印命令与断言计划、零网络。结论写入 `docs/p0/2026-11-14-probe1-评论事件.md`(模板:命令、原始输出、事件可用/不可用判定、降级决策)。
  - README.md:P0 八项清单 → 对应脚本/人工步骤映射表(#2–#8 的 runbook 骨架,现场待填)。

- [ ] **Step 1: 写失败测试**:dry-run 渲染 5 条命令、占位符全部替换、无 `{` 残留;缺 step 字段报错。
- [ ] **Step 2: 跑测试确认失败** Run: `python3 -m pytest tests/test_probe_dryrun.py -q`,Expected: FAIL。
- [ ] **Step 3: 实现** probe 脚本与 config(命令模板先按 lark-cli 文档已知形态填写,标注「P0 现场校准」)。
- [ ] **Step 4: 跑测试确认通过** Run: `python3 -m pytest tests/test_probe_dryrun.py -q` + `python3 tools/probe/probe_comment_event.py --dry-run`,Expected: PASS / 5 条命令打印。
- [ ] **Step 5: Commit** `git commit -m "feat: p0 probe scripts with dry-run mode"`

### Task 12: Skill 契约与验证器 + sa-report + sa-router

**Files:**
- Create: `tools/verify_skills.py`、`skills/sa-router/SKILL.md`、`skills/sa-report/SKILL.md`
- Test: `tests/test_verify_skills.py`(含 tmp fixture:good/bad 各一)

**Interfaces:**
- Consumes: `report.REPORT_FIELDS`、`OUTCOMES`(Task 7)。
- Produces:
  - `verify_skills.py`:walk `skills/sa-*/SKILL.md` → ① frontmatter 合法且 name==目录名、description 非空 ≤200 字;② `REQUIRED_TOKENS: dict[skill_name, tuple[str, ...]]` 逐项子串检查;③ **契约交叉校验**:sa-report 正文必须出现 REPORT_FIELDS 每个字段名与两个 outcome 值(与代码单一事实源对齐)。exit 0/1,输出逐项结果。
  - `REQUIRED_TOKENS` 初始(本任务只启用 sa-router / sa-report 两条,其余 Task 13 解锁):
    - sa-router: `澄清`, `类型注册表`, `结构化`, `禁止`(红线节), `Get`
    - sa-report: `sactl report`, `completed`, `escalated`, `event_id`, `幂等`, `三轮`
  - sa-report SKILL.md 内容:完成协议/升级协议全文、sactl report 用法示例(逐字段表)、幂等(event_id 生成一次复用)、失败重试与退出码、escalated 分歧卡片格式(viewpoint_a/viewpoint_b/evidence/impact/suggestion 五键)。
  - sa-router SKILL.md 内容:职责(歧义检查→一次只问一个问题;类型建议输出 JSON:`{"task_type":"RD","title":"...","ambiguous":false}`)、类型注册表读取方式(GET /tasks 只读 + task_types)、红线(不执行任务、不写库、不承担可靠投递——这些是 sa-coordinator 的事)。

- [ ] **Step 1: 写失败测试**:fixture good skill 通过;缺 frontmatter / name 不匹配 / 缺必需 token / sa-report 缺字段名 → 各自报错退出 1;对真实 skills/ 目录运行 → exit 0(先写好两个 SKILL.md 再跑到绿)。
- [ ] **Step 2: 跑测试确认失败** Run: `python3 -m pytest tests/test_verify_skills.py -q`,Expected: FAIL。
- [ ] **Step 3: 实现 verify_skills.py + 两个 SKILL.md**(SKILL.md 用中文,frontmatter description 用中文一句话)。
- [ ] **Step 4: 跑测试确认通过** Run: `python3 -m pytest tests/test_verify_skills.py -q && python3 tools/verify_skills.py`,Expected: PASS / exit 0。
- [ ] **Step 5: Commit** `git commit -m "feat: skill verifier contract plus sa-router and sa-report skills"`

### Task 13: 五个角色 Skill + bots 角色卡

**Files:**
- Create: `skills/sa-design/SKILL.md`、`skills/sa-review/SKILL.md`、`skills/sa-planner/SKILL.md`、`skills/sa-executor/SKILL.md`、`skills/sa-devreview/SKILL.md`
- Create: `bots/{router,design,reviewer,planner,executor,devreviewer}/CLAUDE.md`(六张角色卡,只放身份+红线)
- Modify: `tools/verify_skills.py`(解锁全部 REQUIRED_TOKENS)
- Test: `tests/test_verify_skills.py`(复用,断言 7 个 skill 全过)

**Interfaces:**
- Consumes: verify_skills 契约、report 字段。
- Produces: REQUIRED_TOKENS 终版:
  - sa-design: `认领消息`, `进展楼`, `方案模板`, `@评审`, `三轮`, `sactl report`
  - sa-review: `检查单`, `可行性`, `风险`, `与原任务偏差`, `三轮`, `sactl report`
  - sa-planner: `Spec`, `方案文档`, `@开发评审`, `@Executor`, `sactl report`
  - sa-executor: `worktree`, `自测`, `自测报告`, `@开发评审`, `sactl report`
  - sa-devreview: `Spec`, `diff`, `自动修复`, `小修`, `大改`, `sactl report`
  - 每个 Skill 显式引用对应 superpowers 子技能:design→brainstorming、planner→writing-plans、executor→test-driven-development、devreview→code-review(写法:「执行时调用 superpowers:<name>」)。
  - CLAUDE.md 角色卡格式:三行以内身份 + 红线列表(「禁止跨话题/跨群 @」「分歧满 3 轮必须 sactl report escalated,禁止第 4 轮」等,按 spec「Worker 层设计」红线节)。

- [ ] **Step 1: 先在 test_verify_skills.py 增加「真实 7 skill 全部通过」断言**,跑红。
- [ ] **Step 2: 写 5 个 SKILL.md + 6 张 CLAUDE.md**,更新 REQUIRED_TOKENS。Run: `python3 tools/verify_skills.py`,Expected: exit 0,7 skill 全绿。
- [ ] **Step 3: 跑全部测试** Run: `python3 -m pytest tests/ -q`,Expected: PASS。
- [ ] **Step 4: Commit** `git commit -m "feat: five role skills and bot role cards"`

### Task 14: 端到端冒烟 + 文档收口

**Files:**
- Create: `tests/test_smoke_e2e.py`
- Modify: `README.md`(基础层现状与运行方式小节)

**Interfaces:**
- Consumes: 全部。

- [ ] **Step 1: 写端到端测试**(FakeSender + FakeProbe,临时库,不起真实 HTTP):ingest_task → dispatch → process_due_outbox → sactl 逻辑直接调 Coordinator.handle_report(或进程内 HTTP)→ escalated → relay_decision → 全程状态断言 received→running→report_pending→awaiting_decision→running→report_pending→done;inbox/outbox 无未决行;kill 模拟(CAS 前重跑)无重复发送。
- [ ] **Step 2: 跑全量** Run: `make test`,Expected: 全 PASS。
- [ ] **Step 3: README 补「基础层(已实现)」小节**:组件表、启动命令(`python3 -m services.coordinator init-db/serve/once`)、sactl 用法、P0 探针待现场执行清单链接。
- [ ] **Step 4: Commit** `git commit -m "test: e2e smoke and readme foundation section"`

---

## 后续波次(不在本计划)

1. **P0 现场验证**:跑 probe #1(事件探针定 D5 走事件还是轮询)、#2–#5(botmux send 实测校准 sender 模板、话题群/bot 互 @/目录协作者/thread A 回复)、#6–#8(故障注入、systemd、轮询配额);产出 `docs/p0/` 验证记录,校准 argv 模板默认值。
2. **P1 调研闭环**:建 6 个 bot 应用与两个话题群、bots.json、Router 控制会话接 sa-router、真实任务端到端。
3. **P2 开发闭环** / **P3 加固**:按 spec 建设计划推进;`sa-bytedcli` / `sa-search`(内部工具 CLI 包装)在 P2 Executor 环境就绪时补写——本波不做,因依赖内部工具实际接口。

# super-agent

个人 Super-agent 落地项目:唯一 Router bot 单聊入口 + 按任务类型划分的话题群 worker,多角色 bot 协作完成「方案调研」与「开发实现」两类工作流,结论与分歧统一回投到你的 thread A,产物沉淀为飞书文档并支持文档评论回路。

> 方案全文(含三轮评审结论)见飞书文档《Super-agent 个人落地方案 v1.2》,本地 v1 存档在 `docs/2026-11-13-落地方案.md`。本 README 是方案的操作性摘要,术语与方案「统一术语」一节保持一致。

## 总体判断

**不部署 dsh-superagent 原型(它绑定 DeepSeek Harness 运行时),而是把它的域模型移植到现有 botmux + herdr + Claude Code/Codex 栈上。** 保留 6 个飞书机器人应用与角色 Skill,新增一个薄确定性控制层 `sa-coordinator`:LLM 只负责语义理解与路由建议,控制层负责派单、状态、回投、去重、重试和恢复;不引入任何新的 agent 框架。

### 八个核心决策

| # | 决策点 | 结论 |
|-|-|-|
| D1 | 总体路线 | 移植域模型,不移植代码。botmux 已提供 IM↔CLI 桥、多 bot、按话题隔离的会话,正好是 Worker 层 |
| D2 | Router 形态 | Router bot 单聊是唯一入口;Router 层 = 控制会话(LLM 语义理解与澄清)+ sa-coordinator(确定性派单/状态/回投)+ 任务登记表 |
| D3 | 派单机制 | worker 群建为话题群,种子消息(顶层)= 新话题,@ 角色 bot 开工;跨群发布用 `send --chat-id`(dispatch 无跨群参数,已实测) |
| D4 | 回报机制 | worker 用 `sactl report` 提交结构化 completed/escalated 事件;sa-coordinator 是 SQLite 唯一写者,持久化 inbox/outbox 负责派单、回投、去重、重试与启动对账 |
| D5 | 文档评论回路 | P0 先探针验证 `drive.notice.comment_add_v1` 能否实际收到事件;可用则事件为主 + 低频对账,不可用则评论 watcher 轮询降级。评论 watcher 与 Watchdog 各自独立 |
| D6 | 模型分配 | Router = DeepSeek v4.1 flash;设计/Planner/Executor = GLM;评审类 = Kimi(异构交叉评审)。额度优先级 GLM > Kimi > DeepSeek > GPT |
| D7 | 容量 | 2C/8G/80G 初始容量目标 4–6 个活跃角色会话(不是任务数);Executor 重命令默认并发 1,实测后再放宽 |
| D8 | 多实例 | 登记表保持实例 1 本地 SQLite + 每日快照,仅 sa-coordinator 写库;跨实例按完整 worker 角色组垂直拆分,worker 向实例 1 上报;每个 bot 记录 owner_instance + lease/fencing token |

## 两条工作流

**流程 R(方案调研):** 你在 Router 会话发任务 A → Router bot 以 Get 表情回应 → 路由到调研 worker 群开话题 → 设计 bot 认领并在「进展楼」持续汇报 → 完成后 @评审 bot → 两 bot 讨论至一致或满 3 轮 → Router 在 thread A 回投结论 + 方案文档链接(含未决分歧点)→ 你在文档上评论 → worker 群 bot 感知并回复,循环至收敛。

**流程 D(开发实现):** 你在 thread A 回复「开始实现」→ Router 路由到开发 worker 群 → Planner 新起话题,把方案文档细化成 Spec 并 @开发评审 → 讨论至一致或 3 轮,不决点经 Router 在 thread A 请你拍板 → 确认后 Executor 开发 + 自测 → @开发评审 比对 Spec 与实现 → 讨论与自动修复,一致或 3 轮,不决点升级给你。

两条流程共用同一套机制:派单(种子消息 + @)、协作(同话题互 @)、收尾(完成协议:分歧满 3 轮必须 `sactl report` 升级,禁止第 4 轮)、决策回传(thread A 拍板 → 话题)。**新任务类型 = 新配置,不是新代码。**

## 架构分层

```text
飞书(事件总线 + 可观察现场)
├── Router 会话(你的单聊,唯一入口)
├── worker 群(话题群:调研工场 / 开发工场 / …)
└── 飞书文档(方案 / Spec / 自测报告,评论回路)

实例 1
├── Router 控制会话(DeepSeek,只做语义理解与澄清,零跨系统副作用)
├── sa-coordinator(Python 薄控制服务,SQLite 唯一写者)
│   ├── inbox:按 event_id/message_id 唯一约束,重复事件不建重复任务
│   ├── 事务:taskid 分配 + 状态迁移 + outbox 入队同事务提交(CAS via expected_version)
│   ├── sender:派单 / thread A 回投 / 决策转发,失败重试,重试前对账
│   ├── report:接收 sactl report 结构化事件,据此回投
│   └── reconciler:启动时扫描非终态任务与未发送 outbox,幂等续跑
├── 评论 watcher(仅事件探针失败时启用的轮询降级)
├── 独立 Watchdog(systemd,业务无关:超时/心跳/存活巡检,不依赖 botmux)
└── registry/tasks.db(SQLite WAL + 每日快照;Base 仅作可选人看镜像)

实例 2+(P3)
└── 完整 worker 角色组(如开发工场),经 bootstrap.sh --role dev 初始化,lease 接管
```

### 群与角色 bot 拓扑

| worker 群(话题群) | 角色 bot(模型) | 会话形态 |
|-|-|-|
| 调研工场 | 设计(GLM)、评审(Kimi) | 每话题各一个独立会话,botmux 按 @ 唤醒 |
| 开发工场 | Planner(GLM)、Executor(GLM,Claude Code;可备 Codex 执行位)、开发评审(Kimi) | 同上;Executor 会话挂完整研发工具链 |

红线:同话题内 bot 互 @ 直接唤醒;**禁止跨话题/跨群 @**(会另起无上下文会话)。Router bot 在 worker 群内只发种子消息与决策,完成/升级统一走 `sactl report`。

### 任务状态机要点

任务字段:taskid、parent_task_id、type、title、thread_a_msg_id、group_chat_id、topic_root_msg_id、doc_links、status、phase、active_role、expected_version、rounds_by_phase、decisions、skill_versions、model、repo_commit、worktree、时间戳。另设 inbox / outbox / comment-inbox / bot_ownership 表。taskid = 日期 + 三位当日序号(000–999)。

## 仓库布局(目标形态)

```text
super-agent/
├── README.md / docs/                  # 方案与决策记录
├── services/coordinator/              # sa-coordinator:inbox/outbox、状态机、sender、reconciler
├── skills/sa-{router,report,design,review,planner,executor,devreview}/
├── tools/comment_watcher.py           # 评论事件不可用时的轮询降级
├── registry/tasks.db                  # SQLite;仅 sa-coordinator 写
├── bots/<角色>/                       # 每 bot 一个工作目录:CLAUDE.md(身份+红线)+ .claude/(软链 skills)
├── roles/{router,research,dev}.toml   # 声明式角色清单(P3 多实例 bootstrap 用)
├── scripts/bootstrap.sh               # 新实例初始化:克隆→bots.json→skills 软链→lark-cli 配置→systemd→自检
└── tasks/<taskid>/                    # 每任务工作区(Executor 建 git worktree)
```

## Skill 清单(8 个,git 版本化)

| Skill | 挂载 | 内容要点 |
|-|-|-|
| sa-router | Router 控制会话 | 语义澄清与路由建议、类型注册表读取、结构化输出;红线:不执行任务、不直接写库 |
| sa-report | 所有角色 bot | sactl report 事件字段、完成/升级协议、幂等与重试规范 |
| sa-design | 设计 bot | 认领消息、进展楼、方案模板写作、@评审协议、三轮规则、完成协议 |
| sa-review | 评审 bot | 评审检查单(可行性/风险/遗漏/与原任务偏差)、意见格式、三轮规则、升级协议 |
| sa-planner | Planner | Spec 模板写作、方案→Spec 细化、@开发评审/@Executor 协议 |
| sa-executor | Executor | git worktree SOP、编码规范、自测 SOP(调 sa-bytedcli/sa-search)、自测报告、@开发评审协议 |
| sa-devreview | 开发评审 | Spec↔实现 diff 比对 SOP、自动修复边界(小修直接改,大改回退 Executor)、三轮规则 |
| sa-bytedcli / sa-search | Executor 等 | 内部工具 CLI 包装:安装位、常用子命令、典型流水、错误码处理 |

迭代方式:skills 放本仓库,各 bot 目录软链加载,改动走 git commit;登记表保存 skill commit 确保可复现;角色卡(CLAUDE.md,只放身份与红线)与 Skill(SOP)分离;每次迭代在对应话题发变更说明。

## 飞书文档体系

```text
我的空间/SuperAgent/
├── 00-任务看板/          # Base 镜像(人看);程序状态在实例本地 SQLite
├── 01-方案调研/<类型>/<yyyymmdd-序号-英文短名>/{方案.md, 分歧与决策记录.md}
├── 02-开发实现/<仓库>/<yyyymmdd-序号-英文短名>/{Spec.md, 自测报告.md, 分歧与决策记录.md}
├── 09-归档/              # done 任务整目录移入(每月一次)
└── 99-模板/              # 6 份模板:方案/研究报告/Spec/自测报告/任务简报/种子消息
```

- 标题格式:文档 `[类型码-taskid] 一句话标题 v版本`;话题 `[taskid] 一句话标题`;类型码 RD(研发调研)/ RS(投研报告)/ DEV(开发实现)。
- 权限模型:**产物文档由产出的角色 bot 以自己身份创建**(设计建方案、Planner 建 Spec、Executor 建自测报告);bot 加为对应子目录协作者(目录级一次授权)。
- 评论协议:角色 bot 回 **Get 表情** = 认可该意见;你回 **OK 表情** = 认可且讨论结束;文字回复则继续讨论。多轮不收敛按三轮规则升级 thread A 拍板。

## 模型与额度分配

| 角色 | 模型 | 理由 |
|-|-|-|
| Router 控制会话 | DeepSeek v4.1 flash | 长输入短输出的路由,轻快模型足够;P1 复核误路由率 |
| 设计 / Planner / Executor | GLM | 产出质量直接决定交付,用额度最足的 |
| 评审 / 开发评审 | Kimi | 异构模型交叉评审优于同源 |
| 异常诊断(可选) | DeepSeek(--model 指定) | Watchdog 告警由确定性脚本负责,模型只做诊断 |

## 建设计划(4 期,总额约 1 周)

| 期 | 内容 | 退出条件 |
|-|-|-|
| P0 | 打通:8 项验证 spike(见下) | 全部通过或明确走降级路径;调研群最小闭环跑通 |
| P1 | 调研闭环 | 一个真实调研任务端到端无人值守跑通(派单→三轮讨论→thread A 回投→评论感知无重复唤醒) |
| P2 | 开发闭环 | 一个真实开发任务跑通(Spec 三轮 + 分歧升级拍板注入 + Executor 自测 + Spec↔实现比对) |
| P3 | 加固 | SQLite 快照可恢复;实例 2 bootstrap 承接开发工场;Watchdog 告警与归档自动化 |

### P0 验证清单(spike,先证伪再动工)

| # | 验证项 | 通过标准 / 降级 |
|-|-|-|
| 1 | **文档评论消息接口探针(必须先做)**:`drive.notice.comment_add_v1` 用户订阅,首评/二次回复/长连接/断线重连/重复事件 | 收到可关联事件 → 事件为主 + 低频对账;收不到 → watcher 轮询降级 |
| 2 | 话题群 `send --chat-id --top-level` 顶层消息成新话题,@ 唤醒群内 bot | 失败则收敛到单 goal 群 + 原生 dispatch/report |
| 3 | bot 间同话题互 @ 可唤醒 | 失败则单 goal 群原生协作 |
| 4 | bot 作为目录协作者建文档、读写、回复评论,权限继承 | 失败则 bot 建后移动到你的目录 |
| 5 | Router 精确回复历史 thread A;DEV 派生时定位 active_task | 失败则 `im +reply --root` 兜底 |
| 6 | 控制层故障注入:重复事件 id;派单/回投边界 kill -9;验证 inbox 唯一约束、outbox 重试、reconciler 续跑 | 不得出现重复任务或丢回投;未通过不进无人值守 |
| 7 | systemd unit + 容器重启演练:全部组件自动恢复,非终态任务续跑 | 无 systemd 镜像用云厂商保活 + entrypoint |
| 8 | 评论降级与配额:完整分页、reply_list、二次回复、Get/OK reaction、is_solved、自回复过滤、轮询配额 | 漏检/自循环则停止自动回复;配额不足则分频 + 退避 |

## 复用 / 自建边界

**复用(零研发):** Claude Code / Codex(会话运行时)、botmux(IM↔CLI 桥、多 bot、话题会话、send/dispatch/report/schedule)、herdr(会话宿主,可随时 attach 附身观察)、lark-cli 全套 skills、superpowers 技能套系(角色 bot 内部流程纪律:brainstorming/writing-plans/TDD/code-review)。

**自建:** 6 个飞书 bot 应用(配置)、8 个角色 Skill(提示词)、评论 watcher(仅降级时)、sa-coordinator(Python 薄服务)、6 份文档模板。

## 风险与对策(摘要)

| 风险 | 对策 |
|-|-|
| 提示注入(文档/评论诱导越权) | worker 无 user 凭据;各 bot 最小 scope;Executor 限制目录与网络;危险操作由确定性策略拦截 + thread A 确认 |
| 会话漂移(长话题爆上下文) | 话题 = 任务粒度天然隔离;Spec 定稿后 Executor 按需 fresh;登记表是事实源 |
| botmux 单点 | systemd 自动拉起 + 启动对账;独立 Watchdog 不依赖 botmux |
| GLM/Kimi 限流 | bots.json 整体切换模型;哨兵用 DeepSeek 分流 |
| 你长时间不看 thread A | 回投带 @;awaiting_decision 超 24h 巡检再提醒一次 |

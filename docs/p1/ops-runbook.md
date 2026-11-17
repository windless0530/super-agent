# P1 生产环境运维要点

- 日期:2026-11-15(来自 Task 8 真机首验踩坑,均已修复并实测)

## sa-coordinator unit 必需环境(`~/.config/super-agent/sa.env`)

| 变量 | 作用 | 踩坑记录 |
|---|---|---|
| `PATH=…~/.botmux/bin + nvm bin…` | sender 子进程找 `botmux`、`lark-cli` | **缺它 = 全部外发失败**(`Errno 2: botmux`),outbox 重试 30+ 次全挂;2026-11-15 17:36 修复后 attempt 34/31 补送成功 |
| `SA_BOTMUX_SESSION_ID=08e3b041-…` | 外发身份(headless 会话,bot=sa-router 新应用) | 换 router 应用时必须同步换绑 |
| `SA_REACTION_CMD=lark-cli --profile sa-router im reactions create …` | Get 表情身份 | **默认模板用 lark-cli 默认应用,在 router 私聊/工场群非成员必报 231002**;P0 未暴露因测试群恰有该应用 |

## lark-cli 命名 profile(bot 身份操作用)

- `default`(cli_aa41c9…)= 用户日常;`sa-design` = bot 建档/回评(01-方案调研);`sa-router` = Get 表情。
- 建法:`python3 -c "<从 ~/.botmux/bots.json 取 larkAppSecret>" | lark-cli profile add --name <role> --app-id <cli_…> --app-secret-stdin`(secret 不落终端)。
- profile 下无 user 登录态 → bot 建档的自动 full_access 授权 skipped,用户访问靠目录 container 继承(实测读/评/移均通)。

## 部署/回滚序列

1. 停 unit → `python3 -m services.coordinator --db registry/tasks.db init-db`(幂等)→ `tools/sactl types set-group/set-member` 回填 → `scripts/gen_bot_settings.py`(合并后必须重跑,文件 gitignored)
2. `~/.config/super-agent/watcher.env`:`SA_WATCHER_BOT_ID_ARGS=--bot-id <sa-design open_id>`
3. `systemctl --user start sa-coordinator sa-comment-watcher` → `/healthz` 断言
4. 回滚 = `git reset --hard <旧版>` + 还原两个 env + 重启;生产库改动均为加法,无需回滚

## Router fastpath 部署/回滚

### 部署顺序

1. 在 `sa.env` 配置 `SA_LLM_BASE_URL=https://api.deepseek.com`、`SA_LLM_MODEL=deepseek-flash`、`SA_LLM_KEY`(只写现场 600 文件,禁止入库/打印)。
2. 复制 `systemd/im-watcher.env.example` 为 `~/.config/super-agent/im-watcher.env`,填控制会话 chat_id 与 sa-router bot open_id;cursor 默认 `registry/im_watcher_cursor.json`。
3. 安装 `sa-im-watcher.service`,`systemctl --user daemon-reload`;先 `systemctl --user restart sa-coordinator`,并断言 `curl -fsS http://127.0.0.1:8787/healthz` 返回 `{"ok":true}`。
4. 再 `systemctl --user enable --now sa-im-watcher`;检查首轮 cursor 从当前时间创建,不得回放历史私聊。
5. 真机顺序:新任务 → Get → 结论卡(awaiting_go,无 dispatch)→ 对应 thread A 回「开工」→ running + 原生 text 种子。旧 `/route` 同样只能到 awaiting_go。
6. 兼容性已实测:watcher unit 需先加载 `sa.env`(PATH)再加载 `im-watcher.env`;lark-cli 纯文本正文可直接读取;本地 naive `create_time` 先按主机时区再归一 UTC;chat 只返回话题根,必须逐 thread 拉回复并持久化 `thread_id → root_msg_id`。未文档化的 `only_thread_root_messages=false` 不可靠,禁止使用。

### 回滚顺序

1. `systemctl --user stop sa-im-watcher`,停止新消息进入;处理完或确认放弃所有 awaiting_go 任务。
2. 如需保留待路由任务,先停 sa-coordinator,再显式把 awaiting_go 迁回 received(仅在 unit 停止后执行):

```bash
python3 - <<'PY'
import sqlite3
from datetime import datetime, timezone
conn = sqlite3.connect('registry/tasks.db')
now = datetime.now(timezone.utc).isoformat()
conn.execute("UPDATE tasks SET status='received', version=version+1, updated_at=? WHERE status='awaiting_go'", (now,))
print(f'migrated awaiting_go→received: {conn.total_changes}')
conn.commit()
PY
```

3. 切回旧代码、还原旧 env,再启动 sa-coordinator。**禁止携带 awaiting_go 直接回滚**:旧状态机不认识该状态,旧 `/route` 也无法处理。
4. 注意:真正回滚到旧代码会恢复旧 `/route` 自动派单语义,仅可作为故障回退并由运维接管;不得把它当作正常入口。

## 已知噪音/遗留

- 任务 20261115-001(用户撤回消息产生)停 clarifying,Watchdog 每 30min 记日志告警,无功能影响。
- worker 会话内 lark-cli 默认身份对 sa-design 建的文档可能无权回评(预期风险,Task 9 Step 2 验证;届时 worker 侧需 `--profile sa-design`)。
- `bots/<role>/` 下的任务产物文件(如 spec-*.md)是运行时输出,已 gitignore,不入库。

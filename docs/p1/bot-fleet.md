# P1 bot 队伍登记表

- 日期:2026-11-15
- 结论:**6 bot 全部建成并 online**(botmux 3.59.10;`botmux setup add --create-app` 全程免扫码,复用既有飞书登录态;权限/长连接事件/发版均自动完成)

| 应用名 | appId | 飞书显示名 | 角色(注册表) | 模型 | workingDir | 群 |
|---|---|---|---|---|---|---|
| sa-router | **cli_aa73e35662385c02**(2026-11-15 用户指定新建,独立应用) | sa-router | router | deepseek-v4-pro(v4.1-flash 被端点拒绝,已回退) | bots/router | 调研+开发两组 |
| sa-design-planner | cli_aa73f547f0f89c1a | design | glm-5.3 | bots/design | 调研工场 |
| sa-design-reviewer | cli_aa73f5dd27f8dbf7 | review | k3.1-pro[1m] | bots/reviewer | 调研工场 |
| sa-develop-planner | cli_aa73f5c27b39dbf6 | planner | glm-5.3 | bots/planner | 开发工场 |
| sa-develop-executor | cli_aa73f5e12e789c14 | executor | glm-5.3 | bots/executor | 开发工场 |
| sa-develop-reviewer | cli_aa73f6094ff99c17 | devreview | k3.1-pro[1m] | bots/devreviewer | 开发工场 |

- logo:logos/<short>.png 已随 `--app-icon` 上传(5 张 worker);sa-router 图标需在控制台手动更新(2026-11-15 用户新版 router.png 待传)。
- **cli_aa561825aab8dbb5(飞书名 Elio The Archangel)= 用户个人老应用,P1 曾复用后已完全退出**:botmux 条目恢复 P1 前原值(name=Elio-The-Archangel,model=glm-5.3,2026-11-15 用户 `!` 自跑),并退出两个工场群;此后与本项目无关,勿再改动。孤儿应用 cli_aa73e1d039f85c01(首次建 sa-router 失败遗留,secret 已丢)待用户在控制台删除。
- 凭据:各 bot 会话读 `bots/<role>/.claude/settings.json`(scripts/gen_bot_settings.py 生成,gitignore);合并后在主检出重跑。
- bot open_id(role_members 的 mention 令牌)待建群后从群成员接口取,回填见 docs/p1/groups.md。

# P1 bot 队伍登记表

- 日期:2026-11-15
- 结论:**6 bot 全部建成并 online**(botmux 3.59.10;`botmux setup add --create-app` 全程免扫码,复用既有飞书登录态;权限/长连接事件/发版均自动完成)

| 应用名 | appId | 飞书显示名 | 角色(注册表) | 模型 | workingDir | 群 |
|---|---|---|---|---|---|---|
| sa-router | cli_aa561825aab8dbb5(复用既有应用) | **Elio The Archangel**(改名需开发者后台,见下) | router | deepseek-v4-pro(v4.1-flash 被端点拒绝,已回退) | bots/router | 调研+开发两组 |
| sa-design-planner | cli_aa73f547f0f89c1a | design | glm-5.3 | bots/design | 调研工场 |
| sa-design-reviewer | cli_aa73f5dd27f8dbf7 | review | k3.1-pro[1m] | bots/reviewer | 调研工场 |
| sa-develop-planner | cli_aa73f5c27b39dbf6 | planner | glm-5.3 | bots/planner | 开发工场 |
| sa-develop-executor | cli_aa73f5e12e789c14 | executor | glm-5.3 | bots/executor | 开发工场 |
| sa-develop-reviewer | cli_aa73f6094ff99c17 | devreview | k3.1-pro[1m] | bots/devreviewer | 开发工场 |

- logo:logos/<short>.png 已随 `--app-icon` 上传(6 张)。
- Router 复用既有应用,飞书侧应用名未改(如需可在开发者后台改名,不影响功能)。
- 凭据:各 bot 会话读 `bots/<role>/.claude/settings.json`(scripts/gen_bot_settings.py 生成,gitignore);合并后在主检出重跑。
- bot open_id(role_members 的 mention 令牌)待建群后从群成员接口取,回填见 docs/p1/groups.md。

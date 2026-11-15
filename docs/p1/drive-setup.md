# P1 Drive 目录树、模板与协作者授权

- 日期:2026-11-15
- 结论:**Task 7 全部落地并实测通过**(probe #4 一并收口)

## 目录树(我的空间 / SuperAgent/,user 身份创建)

| 目录 | token | 协作者(edit) |
|---|---|---|
| 00-任务看板 | `U2WxfFHpYl8fNqdwRU6cwyxJnFf` | — |
| 01-方案调研 | `LesXfhzLNlYKPWdzPN8c5lCcnag` | sa-design、sa-review |
| 02-开发实现 | `JjJ1fl8Rql72Q3dXJyxce9XtnXd` | sa-planner、sa-executor、sa-devreview |
| 09-归档 | `Wc8Wf3fj9lnT2fddET4c04IUnCc` | — |
| 99-模板 | `AWskfSGKOlGU5tdPPU3cVxCSnEh` | — |

- 授权方式:5 条 `lark-cli drive +member-add`(Permission Grant 类,用户 `!` 自跑,member-type openid + bot open_id,open_id 权威值见 docs/p1/groups.md)。
- 授权复核:`+member-list` 显示 bot open_id 被服务端解析为 appId(cli_aa73f547f0f89c1a 等),perm=edit,perm_type=container ✓。

## 模板(99-模板,6 份 docx,本地 markdown 经 `+import --type docx` 导入)

| 模板 | token |
|---|---|
| 方案模板 | `LjHQdKryaosRdRxESXtc6wrZn2e` |
| 研究报告模板 | `WhSfdiDHxohN7JxhWHtcWgnNnme` |
| Spec模板 | `TwHIdqxSCo53XJxeKmZcjsTRnue` |
| 自测报告模板 | `Q0ijdQVoWoJALRxJbgxc3Mt8nuI` |
| 任务简报模板 | `FbdEdgbceouxTpxeAZLcEvzonLP` |
| 种子消息模板 | `QvLUdrk5aoKv14xRJXHcyghNnsc` |

## 权限继承验证(probe #4 收口,2026-11-15)

测试文档 `[P1验证] 权限继承测试-可删`(`LAGSdUkwhoiSMBx4b4LcvzIXn3f`,验证后已移入 09-归档):

1. **bot 建档** ✓ — `lark-cli --profile sa-design drive +import --type docx --folder-token <01>`(sa-design bot 身份,目录 edit 授权对建档生效;ticket 7708287974753291276)。
2. **用户读 + 评论** ✓ — user 身份 `docs +fetch` 可读;`+add-comment --block-id doxcnTdYLooALo9e1PtHGAmasAb` 局部评论成功(comment_id 7708288231468469263)。
3. **bot 回评** ✓ — sa-design bot `+add-reply`(reply_id 7708288329598422042)。
4. **归档** ✓ — user 身份 `+move` 至 09-归档(目录 owner full_access 对子文档的移动权限足够)。

### bot 身份建档的实现路径(spec D6)

- lark-cli 命名 profile:`lark-cli profile add --name sa-design --app-id cli_aa73f547f0f89c1a --app-secret-stdin`(secret 从 ~/.botmux/bots.json 管道输入,不落终端)。
- 产出文档身份 = sa-design bot:`lark-cli --profile sa-design <drive 命令> --as bot`。
- 注意:`--profile sa-design` 下无 user 登录态,bot 建档后的「自动授予当前用户 full_access」会 skipped(status=skipped,不报错);用户访问依赖目录 owner/container 继承,实测读/评/移均通。其余 worker bot 建 profile 同法(P2 需要时再建)。

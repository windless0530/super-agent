# P1 话题群与成员登记

- 日期:2026-11-15(最终版)
- 结论:两群满员、`chat_mode=topic`、群主=Elio

| 群 | chat_id | bot 成员 | 用户 |
|---|---|---|---|
| 调研工场 | **oc_0862450e0a36e1ff8bb9e08ffadf7b18** | sa-router、sa-design-planner、sa-design-reviewer | Elio(owner) |
| 开发工场 | **oc_a9096dbec0eb96f1fe4706118cbe1f1c** | sa-router、sa-develop-planner、sa-develop-executor、sa-develop-reviewer | Elio(owner) |

## bot open_id(权威来源:群成员列表 member_id;bot 的 open_id 跨 app 稳定)

> 注意:用户 open_id 是 app 域的(每个 app 看到的同一用户 open_id 不同);bot open_id 稳定。
> 勿用 `botmux logs --bot <name>` 里出现的 ou_ 当 bot id——那可能是该 app 视角下的用户(如 Elio)id。

| app | appId | open_id |
|---|---|---|
| sa-router | cli_aa561825aab8dbb5 | ou_baa4e976b84975fa081823a9e7d13941 |
| sa-design-planner | cli_aa73f547f0f89c1a | ou_3daa13bffceb4619b7ef041913bbc4d2 |
| sa-design-reviewer | cli_aa73f5dd27f8dbf7 | ou_efd1a7a445edf44720c5a91e83fbe714 |
| sa-develop-planner | cli_aa73f5c27b39dbf6 | ou_15cd8ac5738acbdf3681d4272a19a159 |
| sa-develop-executor | cli_aa73f5e12e789c14 | ou_8db6a0e6661bbf4932f616531ad2c3c2 |
| sa-develop-reviewer | cli_aa73f6094ff99c17 | ou_da278cdd65196fa53deb6574c8dc175c |

## 建群实测结论(重要,后续建群照此)

- **可靠路径:`lark-cli im +chat-create --chat-mode topic --bots <appId,...> --as user`** —— bot 建群时入 id_list,全部成功;owner=Elio。
- 不可靠路径(勿用):`botmux create-group` 的建群后拉人时好时坏;`im chat.members create --member-id-type app_id` 对 bot 返回假成功(空 invalid/not_existed)但实际不入群、不报错。
- 新应用需先 `botmux setup configure <bot> --json` 补 scope(缺 `contact:user.id:readonly` 等会导致 botmux 路径拉人被拒)。
- `+chat-members-list --as user` 的 bots 列表可能不完整(曾只显示 router),以 +chat-create 响应与 @ 唤醒实测为准。

## 遗留清理项(待用户确认后删)

以下为本轮试错产生的废群(均已无用途):
- oc_d183c529102b290536016c528e96551d(botmux 建的旧调研工场,仅 router 入)
- oc_38c1bfbb91ce12561796045ca05b0c27(botmux 建的旧开发工场,仅 router 入)
- oc_e1c09b0f31983c88833ca0bd19995cc6(botmux 二次尝试,worker 被拒)
- oc_e015a9df7eab5ee182d33f4307ecdabc(临时验证群)

## task_types / role_members 回填(合并后在主检出执行)

```bash
tools/sactl types set-group RD oc_0862450e0a36e1ff8bb9e08ffadf7b18 --db registry/tasks.db
tools/sactl types set-group RS oc_0862450e0a36e1ff8bb9e08ffadf7b18 --db registry/tasks.db
tools/sactl types set-group DEV oc_a9096dbec0eb96f1fe4706118cbe1f1c --db registry/tasks.db
tools/sactl types set-member design ou_3daa13bffceb4619b7ef041913bbc4d2:sa-design-planner --db registry/tasks.db
tools/sactl types set-member review ou_efd1a7a445edf44720c5a91e83fbe714:sa-design-reviewer --db registry/tasks.db
tools/sactl types set-member planner ou_15cd8ac5738acbdf3681d4272a19a159:sa-develop-planner --db registry/tasks.db
tools/sactl types set-member executor ou_8db6a0e6661bbf4932f616531ad2c3c2:sa-develop-executor --db registry/tasks.db
tools/sactl types set-member devreview ou_da278cdd65196fa53deb6574c8dc175c:sa-develop-reviewer --db registry/tasks.db
```

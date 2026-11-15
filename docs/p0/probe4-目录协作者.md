# P0 探针 #4:bot 作为目录协作者建文档

- 日期:2026-11-14
- 结论:**已收口(2026-11-15,经 P1 Task 7 在真实目录上验证通过)**,详见 `docs/p1/drive-setup.md`
- 原测试目录:`CaTif0rxil97jldPK8WcL2ysnRf`(`P0探针-目录协作者-可删除`,未使用,可删)
- 原测试 bot open_id:`ou_c1114f7a0d332a84cfade51aad8bc269`

## 已完成

目录由 user 身份创建成功。以下授权请求 dry-run 结构正确:

```bash
lark-cli drive +member-add \
  --token CaTif0rxil97jldPK8WcL2ysnRf --type folder \
  --member-id ou_c1114f7a0d332a84cfade51aad8bc269 \
  --member-type openid --perm edit --as user --yes
```

## 阻塞点(已解除)

真实命令曾被 Claude Code auto-mode 权限分类器拒绝(Permission Grant 类)。2026-11-15 用户以 `!` 前缀自跑 5 条真实授权(01-方案调研 ×2 bot、02-开发实现 ×3 bot),随后完成 bot 建档 → 用户读评 → bot 回评 → 归档 全链路验证,记录见 `docs/p1/drive-setup.md`。

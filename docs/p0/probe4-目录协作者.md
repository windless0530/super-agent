# P0 探针 #4:bot 作为目录协作者建文档

- 日期:2026-11-14
- 结论:**阻塞,未执行真实授权;不能判通过或失败**
- 测试目录:`CaTif0rxil97jldPK8WcL2ysnRf`(`P0探针-目录协作者-可删除`)
- 测试 bot open_id:`ou_c1114f7a0d332a84cfade51aad8bc269`

## 已完成

目录由 user 身份创建成功。以下授权请求 dry-run 结构正确:

```bash
lark-cli drive +member-add \
  --token CaTif0rxil97jldPK8WcL2ysnRf --type folder \
  --member-id ou_c1114f7a0d332a84cfade51aad8bc269 \
  --member-type openid --perm edit --as user --yes
```

## 阻塞点

真实命令被 Claude Code auto-mode 权限分类器拒绝,请求未到达飞书 API,目录权限未改变。后续「bot 在目录建档、user 读评、权限继承」均未执行。

需要用户允许上述精确授权命令,或在飞书目录权限界面把该 bot 加为可编辑协作者;完成后继续本探针。不得把 dry-run 成功写成权限验证通过。

# P0 探针 #7:systemd 与恢复演练

- 日期:2026-11-14
- 结论:**unit 校验和进程级自愈通过;开机自启与容器重启部分完成**

## 环境与产物

- PID 1=`systemd`;user manager 可用;`Linger=yes`。
- 新增 `systemd/sa-coordinator.service`、`systemd/sa-comment-watcher.service` 与 env 示例。
- `systemd-analyze --user verify systemd/sa-coordinator.service systemd/sa-comment-watcher.service` 无输出、退出 0。

## 真实运行与 kill 恢复

用 transient user unit 启动 coordinator 于 `127.0.0.1:18787`:

```text
ActiveState=active, MainPID=852247, NRestarts=0
GET /healthz → {"ok": true}
kill -9 852247
→ ActiveState=activating, SubState=auto-restart, ExecMainStatus=9
→ ActiveState=active, MainPID=853563, NRestarts=1
GET /healthz → {"ok": true}
```

测试后 transient unit 与临时 DB 已清理。

## 未完成

- `botmux autostart enable` 被权限分类器拒绝,未注册 botmux 开机自启。
- watcher 常驻 unit 需要先在 `~/.config/super-agent/watcher.env` 配置真实 bot IDs,未启动。
- 未重启容器;该操作会影响当前所有会话,需用户单独确认。重启后还要检查 enabled units、非终态任务与 outbox 续跑。

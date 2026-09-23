# MCP 服务器运维

API Key 入口参考 [直连部署](server-deploy-runbook.md)，OAuth 入口参考
[WorkBuddy 部署](workbuddy-buddy-app.md)。本文只保留部署后的日常操作。

两个入口及其 systemd 服务统一使用 `yuncyb` 命名。仍在运行旧服务名的服务器必须先
按[完整重命名迁移](yuncyb-rename-migration.md)完成切换，再使用本文命令。

## 服务清单

| 服务 | systemd | 入口 | 监听 |
|---|---|---|---|
| API Key MCP | `yuncyb-mcp` | `yuncyb.app:app` | `127.0.0.1:8102` |
| WorkBuddy MCP | `yuncyb-workbuddy-mcp` | `yuncyb.workbuddy_app:app` | `127.0.0.1:8103` |

## 状态与日志

```bash
systemctl --no-pager --full status yuncyb-mcp
systemctl --no-pager --full status yuncyb-workbuddy-mcp
journalctl -u yuncyb-mcp -n 100 --no-pager
journalctl -u yuncyb-workbuddy-mcp -n 100 --no-pager
```

持续跟踪日志：

```bash
journalctl -u yuncyb-workbuddy-mcp -f
```

日志只允许记录脱敏后的请求摘要；服务不提供任何输出完整凭据的调试开关。
DEBUG 只应短时使用，完成后必须重启回 INFO 并处理调试日志。

## 启停与重启

```bash
systemctl restart yuncyb-workbuddy-mcp
systemctl stop yuncyb-workbuddy-mcp
systemctl start yuncyb-workbuddy-mcp
```

不要用模糊 `pkill uvicorn`，它会同时终止 8102 和 8103。只有历史 nohup 进程才按完整
入口名处理：

```bash
pgrep -af 'uvicorn yuncyb.app:app'
pgrep -af 'uvicorn yuncyb.workbuddy_app:app'
```

## 更新前检查

```bash
cd /root/yuncyb-mcp
git status --short --branch
git fetch origin
uv run ruff check src tests
uv run pytest -q
```

工作区不干净时先确认改动归属，不得直接覆盖。`main`、`test` 更新方式及环境选择见对应
部署文档。

## 一键更新

直连 MCP 的测试代码与生产代码分别使用：

```bash
cd /root/yuncyb-mcp
./scripts/deploy.sh test
./scripts/deploy.sh production
```

两个命令操作同一个服务槽位：`test` 对应 `origin/test`，`production` 对应
`origin/main`。脚本只允许 fast-forward，使用锁文件同步生产依赖，重启后检查健康状态；
健康检查失败时自动回到部署前提交。首次部署与并行环境约束见
[直连部署手册](server-deploy-runbook.md)。

## 健康检查

```bash
ss -ltnp | grep -E ':(8102|8103)\b'
curl -i http://127.0.0.1:8102/healthz
curl -i http://127.0.0.1:8103/healthz
curl -i http://127.0.0.1:8103/.well-known/oauth-authorization-server
curl -i -X POST http://127.0.0.1:8103/mcp \
  -H 'Content-Type: application/json' --data '{}'
```

WorkBuddy 三项预期依次为 `200`、`200`、`401`。最后一项证明 OAuth 保护生效，不是
故障。公网检查应得到相同状态，并确认响应来自 JSON 服务而不是前端 SPA。

## 常见故障

| 现象 | 检查 |
|---|---|
| 端口未监听 | `systemctl status` 与 `journalctl -u <service>` |
| 域名返回前端 HTML | Nginx `server_name` 未命中或仍有 SPA fallback |
| `/mcp` 返回 405 | 请求进入了错误的 Nginx 虚拟主机 |
| WorkBuddy 启动失败 | OAuth 数据库路径、Fernet 密钥和 ERP URL 是否存在 |
| ERP 401/403 | 当前用户绑定的 ERP Token 已失效，应重新连接 |
| OAuth 状态无法解密 | Fernet 密钥与现有 SQLite 不匹配；恢复原密钥或清空测试状态后重连 |

修改 Nginx 前始终执行 `nginx -t`；修改 systemd 单元后执行
`systemd-analyze verify <unit>` 和 `systemctl daemon-reload`。

# ERP 开单 MCP 服务器部署

本文只描述 legacy MCP（`erp_billing.app:app`）的服务器部署。WorkBuddy 使用独立
入口、端口和 OAuth 配置，见 [WorkBuddy 部署](workbuddy-buddy-app.md)；日常启停与
排障见 [服务器运维](server-service-ops.md)。

## 部署基线

| 项目 | 测试 | 生产 |
|---|---|---|
| 分支 | `test` | `main` 或发布 tag |
| `GJP_ENV` | `local` | `production` |
| ERP API | `https://test-ai.yuncyb.com/aicyberp-api` | `https://new.yuncyb.com/aicyberp-api` |
| ASGI 入口 | `erp_billing.app:app` | 同左 |
| 内部端口 | `8102` | `8102` |

生产必须使用 `main` 构建的 wheel 或已打 tag 的提交。公网测试如果需要生产级 JWT
校验，可以使用 `GJP_ENV=production` 并显式覆盖测试 ERP API 地址。

## 首次部署

服务器要求 Python 3.11+、`uv`、Git 和 Nginx：

```bash
git clone --branch main https://github.com/757607106/gjp-cyb-mcp.git /root/gjp-cyb-mcp
cd /root/gjp-cyb-mcp
uv sync --extra dev
uv run ruff check src tests
uv run pytest -q
```

生产非敏感默认值位于 `config/production.env`。以下敏感值只放在 systemd
`EnvironmentFile` 或部署平台 Secret 中，不提交仓库：

- `ERP_BILLING_JWT_SECRET`：接收 Bearer JWT 时必需；
- 第三方平台或 ERP 的任何 Token、Cookie、Client Secret。

推荐 systemd 单元：

```ini
[Unit]
Description=ERP Billing MCP Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/gjp-cyb-mcp
Environment=GJP_ENV=production
EnvironmentFile=-/etc/erp-billing/legacy.env
ExecStart=/usr/local/bin/uv run --frozen uvicorn erp_billing.app:app --host 127.0.0.1 --port 8102
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
install -d -m 700 /etc/erp-billing
test -e /etc/erp-billing/legacy.env || install -m 600 /dev/null /etc/erp-billing/legacy.env
# 使用安全编辑器写入 ERP_BILLING_JWT_SECRET 等敏感值
chmod 600 /etc/erp-billing/legacy.env
systemd-analyze verify /etc/systemd/system/erp-billing-mcp.service
systemctl daemon-reload
systemctl enable --now erp-billing-mcp
```

Nginx 使用独立域名反代 `127.0.0.1:8102`，保留真实 Host 和转发协议，并关闭代理
缓冲以支持 Streamable HTTP/SSE：

```nginx
location / {
    proxy_pass http://127.0.0.1:8102;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;
}
```

修改后执行：

```bash
nginx -t && systemctl reload nginx
```

## 更新

`scripts/deploy.sh` 默认部署 legacy 入口。生产更新：

```bash
cd /root/gjp-cyb-mcp
BRANCH=main GJP_ENV=production ./scripts/deploy.sh
```

脚本会拉取目标分支、同步依赖、重启并检查端口。systemd 部署的运行环境来自 service
文件及其 `EnvironmentFile`，不会继承执行脚本时临时设置的 shell 变量。

## 验收

```bash
systemctl is-active erp-billing-mcp
ss -ltnp | grep ':8102'
curl -i -X POST http://127.0.0.1:8102/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"deploy-check","version":"1.0"}}}'
```

生产验收还应确认：HTTPS 正常、未授权请求被拒绝、日志不包含凭据、读工具可用；写工具
只使用专门测试数据并在验证后作废。

## 回滚

生产优先回滚到已验证 tag：

```bash
git fetch --tags origin
git switch --detach <previous-tag>
uv sync --frozen
systemctl restart erp-billing-mcp
```

确认恢复后再通过正常分支流程修复；不要在服务器直接修改源码或执行
`git reset --hard` 覆盖未知改动。

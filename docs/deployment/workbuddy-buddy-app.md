# WorkBuddy Buddy 应用接入与部署

WorkBuddy 使用独立组合入口 `erp_billing.workbuddy_app:app`，在既有开单 ToolSet 外增加
OAuth 2.1、动态客户端注册、PKCE、ERP AI Token 绑定和加密存储。legacy
`erp_billing.app:app`、商品匹配和销售单行为不变。

## 组件与端口

| 组件 | 位置或值 |
|---|---|
| 正式 OAuth 连接器 | `integrations/workbuddy/gjp-erp-billing/` |
| API Key 联调连接器 | `integrations/workbuddy/gjp-erp-billing-token/` |
| ASGI 入口 | `erp_billing.workbuddy_app:app` |
| systemd 服务 | `erp-billing-workbuddy-mcp` |
| 内部监听 | `127.0.0.1:8103` |
| 公网根地址 | `https://workbuddy-mcp.yuncyb.com` |
| MCP URL | `https://workbuddy-mcp.yuncyb.com/mcp` |

正式连接器由服务端 OAuth 绑定 ERP Token。联调连接器把用户在 WorkBuddy 本地填写的
测试 Token 注入 `X-API-Key`，只用于旧测试端点验证，不能作为正式方案。

## 环境选择

公网可访问的验收服务也使用 `GJP_ENV=production`，以启用严格配置校验；ERP 地址决定
实际连接测试还是生产：

| 场景 | 分支 | ERP API | OAuth 数据库和密钥 |
|---|---|---|---|
| 验收 | `test` | `https://test-ai.yuncyb.com/aicyberp-api` | 独立测试库、测试密钥 |
| 生产 | `main` / tag | `https://new.yuncyb.com/aicyberp-api` | 独立生产库、生产密钥 |

同一公网域名同一时刻只能指向一个环境。原地从验收切生产时连接器 ZIP 无需修改，但必须
更换数据库和 Fernet 密钥，并让用户重新连接。测试与生产长期并行时必须使用不同域名、
connector source、数据库和密钥。

## 服务配置

敏感配置放在仓库外，例如 `/etc/erp-billing/workbuddy-test.env`：

```dotenv
GJP_ENV=production
ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
WORKBUDDY_PUBLIC_BASE_URL=https://workbuddy-mcp.yuncyb.com
WORKBUDDY_OAUTH_DB_PATH=/var/lib/erp-billing/workbuddy-oauth-test.db
WORKBUDDY_OAUTH_ENCRYPTION_KEY=<只生成一次并安全保存的Fernet密钥>
WORKBUDDY_CONNECTOR_SOURCE=gjp-erp-billing
```

首次生成密钥：

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
chmod 600 /etc/erp-billing/workbuddy-test.env
```

同一数据库必须始终使用同一密钥；随意重新生成会导致已保存状态无法解密。单实例可使用
加密 SQLite。多 worker 或多副本上线前必须迁移到共享存储，不能让多个实例写同一个
网络盘 SQLite。

## systemd

`/etc/systemd/system/erp-billing-workbuddy-mcp.service`：

```ini
[Unit]
Description=ERP Billing WorkBuddy MCP Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/gjp-cyb-mcp
EnvironmentFile=/etc/erp-billing/workbuddy-test.env
ExecStart=/usr/local/bin/uv run --frozen uvicorn erp_billing.workbuddy_app:app --host 127.0.0.1 --port 8103
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
systemd-analyze verify /etc/systemd/system/erp-billing-workbuddy-mcp.service
systemctl daemon-reload
systemctl enable --now erp-billing-workbuddy-mcp
```

## Nginx

为新域名单独建虚拟主机，不能复用前端 SPA fallback：

```nginx
server {
    listen 80;
    server_name workbuddy-mcp.yuncyb.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name workbuddy-mcp.yuncyb.com;
    ssl_certificate /usr/local/vango/certificate/yuncyb.com.pem;
    ssl_certificate_key /usr/local/vango/certificate/yuncyb.com.key;

    location / {
        proxy_pass http://127.0.0.1:8103;
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
}
```

```bash
nginx -t && systemctl reload nginx
```

## 连接器打包与平台配置

ZIP 根目录必须直接包含 `connector-meta.json`、`mcp.json` 和 `icon.svg`：

```bash
mkdir -p dist/workbuddy-release
(cd integrations/workbuddy/gjp-erp-billing && \
  zip -q -r -FS ../../../dist/workbuddy-release/gjp-erp-billing-0.1.0.zip .)
unzip -l dist/workbuddy-release/gjp-erp-billing-0.1.0.zip
```

在 WorkBuddy 开放平台：

1. 上传正式 OAuth 连接器并设为 Buddy 应用内置连接器；
1. 绑定 `skills/erp-billing/SKILL.md`；
1. 工作模式使用 `ERP_BILLING_SYSTEM_PROMPT`；
1. 拍照开单选择支持视觉理解的模型，由模型组装 `order_text`；
1. 预览验证通过后再提交审核。

Buddy 应用的 Client Secret 属于 WorkBuddy Open API，不写入连接器 ZIP，也不用于 MCP
动态客户端注册。连接器 OAuth 客户端使用 DCR + PKCE，`token_endpoint_auth_method`
为 `none`。

## 验收

```bash
curl -i https://workbuddy-mcp.yuncyb.com/healthz
curl -i https://workbuddy-mcp.yuncyb.com/.well-known/oauth-authorization-server
curl -i -X POST https://workbuddy-mcp.yuncyb.com/mcp \
  -H 'Content-Type: application/json' --data '{}'
```

预期依次为 `200`、`200`、`401`，且 401 的 `WWW-Authenticate` 包含
`resource_metadata`。随后在 WorkBuddy 完成一次真实连接并验证读工具；写操作必须使用
专门测试数据、明确确认，并在验证结束后作废测试单。

OAuth 还应满足：回调 URI 白名单、授权码一次性使用、PKCE S256、refresh token 轮换、
access token 绑定当前 `/mcp` resource，以及 ERP Token 不进入日志、工具 Schema 或模型
上下文。

## 更新与生产切换

代码更新仍走 `test` → 验收 → `main`/tag。systemd 环境来自 `EnvironmentFile`，不会继承
执行部署脚本时的临时 shell 变量。日常更新使用专用一键入口：

```bash
./scripts/deploy-workbuddy.sh
BRANCH=test ./scripts/deploy-workbuddy.sh
```

第一条部署 `main`，第二条部署 `test`。包装脚本只选择 WorkBuddy ASGI 入口；服务会自动
使用 `erp-billing-workbuddy-mcp` 和 8103，不改变 legacy 部署脚本的默认行为。systemd
日志继续通过 `journalctl -u erp-billing-workbuddy-mcp` 查看。
实际连接测试或生产 ERP 仍由 systemd `EnvironmentFile` 决定。

切换生产前：

1. 备份或保留测试 OAuth 数据库，不复用到生产；
1. 创建生产专用环境文件、数据库路径和 Fernet 密钥；
1. 把 ERP 地址改为生产；
1. 重启服务并重复三项公网验收；
1. 在 WorkBuddy 重新连接并做最小读写验收。

日常状态、日志、重启和故障处理见 [服务器运维](server-service-ops.md)。

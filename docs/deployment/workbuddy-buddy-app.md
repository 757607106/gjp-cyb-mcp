# WorkBuddy Buddy 应用接入与部署

WorkBuddy 使用生产组合入口 `yuncyb.workbuddy_app:app`，在既有开单 ToolSet 外增加
OAuth 2.1、动态客户端注册、PKCE、ERP AI Token 绑定和加密存储。独立的
`yuncyb.app:app` 继续服务生产 ERP Bearer Token / `X-API-Key` 调用，商品匹配和
销售单行为不变。

## 组件与端口

| 组件 | 位置或值 |
|---|---|
| 正式 OAuth 连接器 | `integrations/workbuddy/yuncyb/` |
| API Key 联调连接器 | `integrations/workbuddy/yuncyb-token/` |
| ASGI 入口 | `yuncyb.workbuddy_app:app` |
| MCP 服务名 | `yuncyb` |
| systemd 服务 | `yuncyb-workbuddy-mcp` |
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

`source` 已统一为 `yuncyb`。从旧 source 升级时 WorkBuddy 会识别为新连接器，现有
连接需要重新安装或授权；服务器迁移顺序见[完整重命名迁移](yuncyb-rename-migration.md)。

## 服务配置

敏感配置放在仓库外，例如 `/etc/yuncyb/workbuddy-test.env`：

```dotenv
GJP_ENV=production
YUNCYB_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
WORKBUDDY_PUBLIC_BASE_URL=https://workbuddy-mcp.yuncyb.com
WORKBUDDY_OAUTH_DB_PATH=/var/lib/yuncyb/workbuddy-oauth-test.db
WORKBUDDY_OAUTH_ENCRYPTION_KEY=<只生成一次并安全保存的Fernet密钥>
WORKBUDDY_CONNECTOR_SOURCE=yuncyb
```

首次生成密钥：

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
chmod 600 /etc/yuncyb/workbuddy-test.env
```

同一数据库必须始终使用同一密钥；随意重新生成会导致已保存状态无法解密。单实例可使用
加密 SQLite。多 worker 或多副本上线前必须迁移到共享存储，不能让多个实例写同一个
网络盘 SQLite。

## systemd

`/etc/systemd/system/yuncyb-workbuddy-mcp.service`：

```ini
[Unit]
Description=YunCYB WorkBuddy MCP Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/yuncyb-mcp
EnvironmentFile=/etc/yuncyb/workbuddy-test.env
ExecStart=/usr/local/bin/uv run --frozen uvicorn yuncyb.workbuddy_app:app --host 127.0.0.1 --port 8103
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
systemd-analyze verify /etc/systemd/system/yuncyb-workbuddy-mcp.service
systemctl daemon-reload
systemctl enable --now yuncyb-workbuddy-mcp
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

Buddy 应用基础信息统一使用以下文案：

| 配置项 | 内容 |
|---|---|
| 应用名称 | 管家婆云创业版 |
| 应用简介 | 智能进销存经营助手，支持销售、采购、库存、资金与报表分析 |

应用名称是 Buddy 应用在 WorkBuddy 内的对外展示名称；连接器和内置 Skill 的中文
展示名保持一致，统一为“管家婆云创业版”。

ZIP 根目录必须直接包含 `connector-meta.json`、`mcp.json` 和 `icon.svg`：

```bash
mkdir -p dist/workbuddy-release
(cd integrations/workbuddy/yuncyb && \
  zip -q -r -FS ../../../dist/workbuddy-release/yuncyb-0.2.1.zip .)
unzip -l dist/workbuddy-release/yuncyb-0.2.1.zip
```

在 WorkBuddy 开放平台：

1. 上传正式 OAuth 连接器并设为 Buddy 应用内置连接器；
1. 绑定 `skills/yuncyb/SKILL.md`（Skill 展示名为“管家婆云创业版”）；
1. 工作模式使用 `YUNCYB_SYSTEM_PROMPT`；
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
./scripts/deploy-workbuddy.sh production
./scripts/deploy-workbuddy.sh test
```

第一条部署 `main`，第二条部署 `test`。包装脚本只选择 WorkBuddy ASGI 入口；服务会自动
使用 `yuncyb-workbuddy-mcp` 和 8103，不改变 legacy 部署脚本的默认行为。systemd
日志继续通过 `journalctl -u yuncyb-workbuddy-mcp` 查看。
实际连接测试或生产 ERP 仍由 systemd `EnvironmentFile` 决定。

### 切换 ERP 环境

同一域名和 WorkBuddy 应用从测试 ERP 切到生产 ERP 时，只修改服务器环境文件中的一行：

```dotenv
YUNCYB_BASE_URL=https://生产环境地址/aicyberp-api
```

然后重启并验证：

```bash
systemctl restart yuncyb-workbuddy-mcp
curl -fsS https://workbuddy-mcp.yuncyb.com/healthz
```

不需要修改 Nginx、WorkBuddy 公网域名、OAuth 回调地址或 Buddy 应用的 Client ID。
ERP 凭据与环境绑定，切换后用户需要重新完成授权，不能继续使用测试 ERP 的授权凭据。

这是同一套服务从测试环境正式切换到生产环境的做法。如果测试和生产必须长期并行，才需要
另一套服务、环境文件、OAuth 数据库和域名；不属于当前的单地址切换场景。

日常状态、日志、重启和故障处理见 [服务器运维](server-service-ops.md)。

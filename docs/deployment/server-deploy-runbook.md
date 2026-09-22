# ERP 长期凭据直连 MCP 部署

`erp_billing.app:app` 是 ERP 长期凭据直连接口。三方客户端在每个 MCP 请求中动态发送
`Authorization: Bearer <ERP token>` 或 `X-API-Key`。服务端使用身份字段或摘要隔离
会话，并把原凭据交给固定 ERP API 最终鉴权。WorkBuddy MCP OAuth 客户端使用
`erp_billing.workbuddy_app:app`，两种 Bearer Token 不得混用入口。

## 配置与启动

```bash
export GJP_ENV=production
export ERP_BILLING_BASE_URL=https://new.yuncyb.com/aicyberp-api
export GJP_MCP_ALLOWED_HOSTS=mcp.example.com
export GJP_MCP_ALLOWED_ORIGINS=https://app.example.com

uv run uvicorn erp_billing.app:app \
  --host 127.0.0.1 --port 8102
```

默认同一 Bearer Token 或 API Key 每 60 秒最多 120 个 `/mcp` 请求：

```bash
export GJP_MCP_RATE_LIMIT_REQUESTS=120
export GJP_MCP_RATE_LIMIT_WINDOW_SECONDS=60
```

公网只通过 HTTPS 网关反向代理到回环端口。网关保留真实 `Host`，不得记录或改写
`Authorization`/`X-API-Key`，并应限制请求体大小、连接数和超时。部署脚本默认监听
`127.0.0.1`：

```bash
BRANCH=main GJP_ENV=production ./scripts/deploy.sh
```

## 验收

```bash
curl -i http://127.0.0.1:8102/healthz

curl -i -X POST http://127.0.0.1:8102/mcp \
  -H 'Host: localhost' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  -H 'X-API-Key: <test-api-key>' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/list' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{},"io.modelcontextprotocol/clientInfo":{"name":"deploy-check","version":"1.0"}}}}'
```

还应分别使用测试 ERP Bearer Token 和 API Key 完成调用验证。验收要求：两种合法凭据
都可列出和调用工具；缺失或无效凭据返回鉴权错误；超过阈值返回 429 和
`Retry-After`；日志不得出现凭据原文。

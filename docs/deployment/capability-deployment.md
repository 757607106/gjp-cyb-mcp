# ERP 销售开单能力部署约定

## 固定端点

生产开单只连接一个部署级 ERP API 地址：

```bash
ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
```

该 URL 在进程启动时校验并固定，不从租户会话、MCP Header、Tool 参数或 CLI
请求读取。Adapter 只允许源码中定义的相对路径。

## 生产装配

部署方实现：

- `McpIdentityResolver`：验证 MCP JWT/OAuth2 并生成 `InvocationContext`。
- `McpToolSetResolver`：按 `(tenant_id, account_id, session_id)` 返回隔离 ToolSet。
- `BusinessApiCredentialProvider`：只按上下文返回当前会话 Bearer，不返回 URL。

```python
from erp_billing.adapters import (
    BusinessAuthenticatedJsonClient,
    ErpAuthenticatedHttpAdapter,
)

http = BusinessAuthenticatedJsonClient(
    base_url=settings.erp_billing_base_url,
    credential_provider=billing_credential_provider,
    timeout_seconds=30,
)
api = ErpAuthenticatedHttpAdapter(http)
```

再使用 `create_billing_mcp_service()` 发布 `/mcp` 与 `/sse`。生产服务不构建模型。

## 权限与写单

- 查询、同步和生成预览要求 `billing:read`。
- `submit_sales_order` 要求 `billing:write`。
- 写单还必须有当前 `preview_id`、用户明确确认和唯一 `idempotency_key`。
- 单进程会话内防重只适合验证；多 worker 或多副本使用 Redis/数据库或 ERP 网关
  提供共享幂等。

## CLI 边界

`gjp_cli.billing_validation` 发布 `/mcp` 与 `/sse`，MCP 客户端直接使用
ERP JWT 作为 Bearer Token。它不提供账号、密码或验证码登录，也不接受
动态 URL。生产 MCP 应由对接方实现 JWT/OAuth2 验签。

## 安全要求

- 固定 Base URL 必须为 HTTPS，不能含用户信息、query 或 fragment。
- Tool 参数不得包含身份、URL、JWT、Cookie、Token 或密码。
- 上游 Bearer 只存于服务端凭据提供者，日志默认脱敏。
- ToolSet、Session、商品目录、预览和幂等结果按会话隔离。
- MCP 不接收音频、图片或附件；媒体先在业务页面转换并确认成文本。

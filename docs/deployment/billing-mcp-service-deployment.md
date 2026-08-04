# ERP 销售开单 MCP 部署

服务端点：

- `POST /mcp`：Streamable HTTP
- `GET /sse`：SSE 兼容入口

MCP 客户端直接使用 ERP JWT 作为 Bearer Token，无需换票。

## 配置与启动

ERP URL 是固定部署配置：

```zsh
export ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
export ERP_BILLING_TIMEOUT_SECONDS=30
```

仓库自带可运行入口 `erp_billing.app:app`：直接从 MCP Bearer 中的 ERP JWT
解析身份，按 `(tenant, account, session)` 隔离 ToolSet，并把同一个 JWT 注入
ERP API 调用。

```zsh
uv run uvicorn erp_billing.app:app --host 0.0.0.0 --port 8102
```

生产如需接入自有会话存储或 JWT/OAuth2 验签，可自行实现
`McpIdentityResolver` 与 `McpToolSetResolver`，再用
`create_billing_mcp_service()` 装配 Starlette 应用：

```python
from erp_billing.mcp_service import create_billing_mcp_service

app = create_billing_mcp_service(
    schema_toolset=billing_toolset,
    identity_resolver=identity_resolver,
    toolset_resolver=toolset_resolver,
)
```

MCP 客户端把 ERP JWT 直接作为 Bearer Token 传给 MCP 服务，服务端从 JWT payload
解析 tenantId、loginId 构造 InvocationContext，并把同一个 JWT 用于
ERP API 调用。业务 URL 始终来自 `ERP_BILLING_BASE_URL`。生产 MCP 应由
对接方实现 JWT/OAuth2 验签。

## 工具

| 工具 | 权限 | 副作用 |
|---|---|---|
| `sync_products` | `billing:read` | 替换当前 Session 内存商品目录 |
| `search_products` | `billing:read` | 无 |
| `search_billing_references` | `billing:read` | 无 |
| `preview_sales_order` | `billing:read` | 保存会话内不可变预览 |
| `submit_sales_order` | `billing:write` | `POST /sales/orders` |

## 完整销售单契约

业务必填：客户、出库仓库、经手人、录单日期、商品明细。备注可选且最多 200 字。
系统字段 `id=0` 与 `save_type` 由工具生成，映射为草稿 `0`、预收 `1`、正式 `2`。

准备示例：

```json
{
  "name": "preview_sales_order",
  "arguments": {
    "order_text": "土豆5斤，牛肉2斤",
    "customer": "客户甲",
    "warehouse": "一号仓",
    "handler": "张三",
    "order_date": "2026-08-04",
    "remark": "下午送达",
    "save_type": "final",
    "confirmed_products": []
  }
}
```

工具返回 `missing_required_fields`、`needs_confirmation`、`unit_warnings`、三个商品匹配
数组以及 `ready_to_submit`。只有就绪时才返回 `preview_id` 和 `preview`。

用户看到当前预览并明确确认后提交：

```json
{
  "name": "submit_sales_order",
  "arguments": {
    "preview_id": "sales-preview-...",
    "idempotency_key": "conversation-42-sales-v1",
    "confirmed_by_user": true
  }
}
```

成功响应含 `submitted=true` 和 `order_id`。相同幂等键重试返回第一次结果；同一键
用于不同预览会被拒绝。

## 生产接入

生产配置固定 URL，只动态解析当前会话 Bearer：

```python
http = BusinessAuthenticatedJsonClient(
    base_url="https://test-ai.yuncyb.com/aicyberp-api",
    credential_provider=billing_credential_provider,
    timeout_seconds=30,
)
api = ErpAuthenticatedHttpAdapter(http)
```

`billing_credential_provider.resolve(context)` 只返回 `BusinessApiCredential`。凭据不
进入 `InvocationContext`、Tool Schema、模型消息或工具结果。

生产检查：

- URL 固定且为 HTTPS。
- MCP 直接解析 ERP JWT payload，ERP API 拒绝过期 JWT。
- Bearer、ToolSet、目录、预览和幂等结果按会话隔离。
- 多副本使用共享会话/幂等存储。
- ERP 401/403 映射为重新授权错误，不让 Agent 索要 Token。
- `uv run pytest -q` 与 `uv run ruff check src tests` 全部通过。

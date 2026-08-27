# ERP 销售开单 MCP 部署

服务端点：

- `POST /mcp`：Streamable HTTP
- `GET /sse`：SSE 兼容入口

MCP 客户端直接使用 ERP JWT 作为 Bearer Token，无需换票。

## 环境制品范围

生产环境不包含测试相关的代码和文档，仓库内容按归属划分：

| 内容 | 归属 | 生产制品 |
|---|---|---|
| `src/erp_billing`、`src/gjp_common` | 运行时代码 | 包含 |
| `config/production.env` | 生产配置模板 | 包含 |
| `config/local.env` | 测试环境配置 | 不包含 |
| `tests/`、`tests/billing/fixtures/` | 测试代码与数据 | 不包含 |
| `docs/`、`AGENTS.md`、`README.md` | 开发文档 | 不包含 |

生产从构建产物安装，不把仓库目录整体搬上服务器：

```zsh
uv build --wheel
uv pip install dist/gjp_erp_billing_mcp-*.whl
```

wheel 只含 `src/erp_billing` 与 `src/gjp_common` 两个包，测试、文档与
测试环境配置天然不进入制品；`config/production.env` 随部署清单单独投放。
**生产 wheel 必须从 `main` 分支构建**，禁止用 `test` 或 `feature/*`
分支的产物上生产。
本地宽松解析器 `DirectJwtIdentityResolver` 虽与生产代码同包，但只在
`GJP_ENV` 非 production 时被装配，生产启动强制走验签路径。

## 配置与启动

ERP URL 与验签密钥是固定部署配置，由部署平台注入环境变量：

```zsh
export GJP_ENV=production
export ERP_BILLING_BASE_URL=https://正式域名/aicyberp-api
export ERP_BILLING_JWT_SECRET=<HS256 验签密钥>
export ERP_BILLING_TIMEOUT_SECONDS=30
export ERP_BILLING_AUTO_SYNC_LIMIT=10000
```

`ERP_BILLING_AUTO_SYNC_LIMIT` 是目录为空时自动同步的商品拉取上限
（缺省 10000），防止超大商品目录把首次开单拖到超时；显式调用
`syncProducts` 传 `limit` 时不受该上限约束。

仓库自带可运行入口 `erp_billing.app:app`：按 `GJP_ENV` 选择鉴权强度，
production 下 `VerifiedJwtIdentityResolver` 强制 HS256 验签并校验 JWT
过期，通过后按 `(tenant, account, session)` 隔离 ToolSet，并把同一个
JWT 注入 ERP API 调用。缺失 `ERP_BILLING_JWT_SECRET` 时，生产 Bearer 请求会被拒绝。

```zsh
uv run uvicorn erp_billing.app:app --host 0.0.0.0 --port 8102
```

生产如需接入自有会话存储或 OAuth2 换签，可自行实现
`McpIdentityResolver` 与 `McpToolSetResolver`，再用
`create_billing_mcp_service()` 装配 Starlette 应用：

```python
from erp_billing.mcp_service import create_billing_mcp_service

app = create_billing_mcp_service(
    schema_toolset=billing_toolset,
    identity_resolver=identity_resolver,
    toolset_resolver=toolset_resolver,
    shutdown=toolset_resolver.close,  # Resolver 持有连接池时传入
)
```

内置应用在 ASGI shutdown 阶段清空会话 ToolSet 并关闭共享 HTTP 连接池。自定义
Resolver 如果持有数据库或 HTTP 客户端，也应通过 `shutdown` 注册异步释放函数。

MCP 客户端把 ERP JWT 直接作为 Bearer Token 传给 MCP 服务，服务端验签后从
JWT payload 解析 tenantId、loginId 构造 InvocationContext，并把同一个 JWT
用于 ERP API 调用。业务 URL 始终来自 `ERP_BILLING_BASE_URL`。
直接读 payload 不验签的 `DirectJwtIdentityResolver` 仅限 local 测试环境。

## 工具

| 工具 | 权限 | 副作用 |
|---|---|---|
| `syncProducts` | `billing:read` | 替换当前 Session 内存商品目录 |
| `listProducts` | `billing:read` | 无 |
| `searchProducts` | `billing:read` | 无 |
| `searchBillingReferences` | `billing:read` | 无 |
| `previewSalesOrder` | `billing:read` | 保存会话内不可变预览 |
| `submitSalesOrder` | `billing:write` | `POST /sales/orders` |
| `getSalesOrder` | `billing:read` | 无 |
| `listSalesOrders` | `billing:read` | 无 |
| `voidSalesOrder` | `billing:write` | `PUT /sales/orders/{id}/void` |
| `updateSalesOrder` | `billing:write` | `PUT /sales/orders/{id}` |

## 完整销售单契约

业务必填：客户、出库仓库、经手人、录单日期、商品明细。备注可选且最多 200 字。
系统字段 `id=0` 与 `save_type` 由工具生成，映射为草稿 `0`、预收 `1`、正式 `2`。

准备示例：

```json
{
  "name": "previewSalesOrder",
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

工具返回 `missing_required_fields`、`reference_resolutions`、`unit_warnings`、三个商品
匹配数组、`required_actions` 以及 `ready_to_submit`。Agent 严格按
`required_actions` 顺序处理；只有就绪时才返回 `preview_id` 和 `preview`。

用户看到当前预览并明确确认后提交：

```json
{
  "name": "submitSalesOrder",
  "arguments": {
    "preview_id": "sales-preview-...",
    "idempotency_key": "conversation-42-sales-v1",
    "confirmed_by_user": true
  }
}
```

成功响应含 `submitted=true` 和 `order_no`（业务单号，如 XS 开头；回查失败时
降级为内部 ID，仍可用于后续查询）。相同幂等键重试返回第一次结果；同一键
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

- `GJP_ENV=production`，制品来自 wheel，不含 tests、docs 与 `config/local.env`。
- URL 固定且为 HTTPS；`ERP_BILLING_JWT_SECRET` 仅经环境变量注入。
- JWT 经 HS256 验签与过期校验，非 HS256 算法一律拒绝。
- Bearer、ToolSet、目录、预览和幂等结果按会话隔离。
- 多副本使用共享会话/幂等存储。
- ERP 401/403 映射为重新授权错误，不让 Agent 索要 Token。
- `uv run pytest -q` 与 `uv run ruff check src tests` 全部通过。

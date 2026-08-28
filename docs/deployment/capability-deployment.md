# ERP 销售开单能力部署约定

## 固定端点

生产开单只连接一个部署级 ERP API 地址：

```bash
ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
```

该 URL 在进程启动时校验并固定，不从租户会话、MCP Header 或 Tool 参数
读取。Adapter 只允许源码中定义的相对路径。

## 鉴权方式

MCP 服务同时支持两种鉴权方式，按请求头自动选择，互不影响：

| 方式 | 请求头 | 身份来源 | ERP 业务 API 凭据 |
|---|---|---|---|
| Bearer JWT | `Authorization: Bearer <JWT>` | JWT payload 的 `tenantId`/`loginId` | `Authorization: Bearer <JWT>` |
| X-API-Key | `X-API-Key: ak_xxx` | key 本身（会话隔离标识） | `X-API-Key: ak_xxx` |

- Bearer JWT：生产要求 HS256 验签，密钥由 `ERP_BILLING_JWT_SECRET` 注入；
  本地（测试）不验签。身份与凭据用同一个 JWT。
- X-API-Key：每个租户在创业版生成 key，等同于 token。客户端传 `X-API-Key`
  即可，服务端无需预配映射——用 key 作会话隔离标识，ERP 业务 API 调用用
  `X-API-Key` 头由 ERP 识别真实租户。
- 组合解析器优先 `Authorization` 头，缺失时回退 `X-API-Key`。
- 纯 API Key 部署无需 `ERP_BILLING_JWT_SECRET`，仅在收到 Bearer 请求时
  才需要（生产缺密钥时该请求报错）。
- Bearer/API-Key 只存于服务端凭据提供者，不进入 Tool 参数，日志默认脱敏。

## 会话隔离与商品目录共享

MCP 传输无状态，会话键由服务端从请求头构造：

- 客户端可在每次 MCP 请求携带 `X-Conversation-Id`（对话/会话标识，最长
  64 字符），服务端把它拼进 `(tenant, account, session)` 会话键，使同一用户
  的不同对话窗口各自隔离预览与幂等缓存。
- 未携带时退化为按登录账号隔离（与既有行为一致）；建议对接方始终传入，
  避免同账号多对话互相挤出预览。

商品目录是租户级数据，不随会话隔离：同一租户全部会话共享一份目录缓存
（`TenantCatalogState`），新对话或会话淘汰后重建时无需重新同步，避免首次
开单被目录同步拖慢。目录过期由后台任务刷新，期间继续用旧目录；刷新失败
退避 60 秒重试。目录有效期由 `ERP_BILLING_CATALOG_TTL_SECONDS`（缺省 600
秒）控制；显式调用 `syncProducts` 不受该 TTL 约束，始终强制拉取。

## 生产装配

部署方实现：

- `McpIdentityResolver`：按请求头验证 Bearer JWT 或 X-API-Key 并生成 `InvocationContext`。
- `McpToolSetResolver`：按 `(tenant_id, account_id, session_id)` 返回隔离 ToolSet。
- `BusinessApiCredentialProvider`：只按上下文返回当前会话 Bearer/API-Key，不返回 URL。

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
- `submitSalesOrder` 要求 `billing:write`。
- 写单还必须有当前 `preview_id`、用户明确确认和唯一 `idempotency_key`。
- 单进程会话内防重只适合验证；多 worker 或多副本使用 Redis/数据库或 ERP 网关
  提供共享幂等。

## 安全要求

- 固定 Base URL 必须为 HTTPS，不能含用户信息、query 或 fragment。
- Tool 参数不得包含身份、URL、JWT、Cookie、Token 或密码。
- 上游 Bearer/API-Key 只存于服务端凭据提供者，日志默认脱敏。
- ToolSet、Session、预览和幂等结果按会话隔离；商品目录按租户共享，会话淘汰不丢目录。
- MCP 不接收音频、图片或附件；语音由前端 ASR 转文本后传入。多模态模型（VL）可直接读图组装 order_text，非 VL 模型仍由前端 OCR 转文本。

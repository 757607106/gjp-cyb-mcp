# GJP ERP AI 销售开单 MCP

基于 AgentScope 2.0.5 的完整销售单 MCP 服务，服务名为 `erp-billing`。

```text
AI 平台 / SaaS 对话页
  │ Authorization: Bearer <ERP JWT / OAuth2 token>
  ▼
ERP 开单 MCP
  ├─ McpIdentityResolver：解析身份与 billing scopes
  ├─ McpToolSetResolver：按 tenant/account/session 隔离会话
  ├─ BillingToolSet：资料追问、商品匹配、预览和提交
  └─ BillingApiPort
       └─ Adapter：固定 ERP URL + 按会话注入 Bearer
```

ERP URL 不是租户动态参数，由部署环境唯一配置：

```bash
ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
```

URL 不进入 `InvocationContext` 或 Tool Schema。生产环境由 MCP 客户端直接携带
ERP JWT / OAuth2 Bearer，服务端只从 payload 解析无凭据身份，并在调用 ERP API 时
按当前请求注入原 Bearer；账号、密码、验证码、Cookie 和 Token 都不进入模型可见
工具参数。

## 销售单流程

业务必填项：

- 客户
- 出库仓库
- 经手人
- 录单日期（`YYYY-MM-DD`）
- 商品明细

备注可选，最多 200 字。接口必填的 `id=0` 与 `saveType` 由工具内部管理：草稿
`0`、预收 `1`、正式 `2`。

MCP 发布十个工具：

| 工具 | 作用 |
|---|---|
| `syncProducts` | 同步当前账号可见商品到隔离会话内存 |
| `listProducts` | 分页浏览商品目录 |
| `searchProducts` | 按关键词定位真实 ERP 商品 |
| `searchBillingReferences` | 查询客户、仓库或经手人候选 |
| `previewSalesOrder` | 返回有序待办、商品匹配和不可变销售单预览 |
| `submitSalesOrder` | 用户确认后以 `billing:write` 写入真实 ERP |
| `getSalesOrder` | 查询销售单详情 |
| `listSalesOrders` | 分页查询销售单列表 |
| `voidSalesOrder` | 用户确认后作废销售单 |
| `updateSalesOrder` | 用户确认后修改销售单 |

实际调用接口：

```text
GET  /product/page
GET  /customer/page
GET  /warehouse/page
GET  /staff/page
POST /sales/orders
GET  /sales/orders/page
GET  /sales/orders/{id}
PUT  /sales/orders/{id}
PUT  /sales/orders/{id}/void
```

只有 `previewSalesOrder` 返回 `ready_to_submit=true` 且
`required_actions=["confirm_submit"]`，用户明确确认当前预览，并且调用身份具有
`billing:write` 时，才允许提交。`submitSalesOrder` 还要求唯一
`idempotency_key`；当前实现提供会话内防重，生产多副本应接入共享幂等存储。

提示词只保留两个入口：`ERP_BILLING_MCP_INSTRUCTIONS` 由 MCP initialize 自动下发，
`ERP_BILLING_SYSTEM_PROMPT` 供 AI 平台装配 Agent。两者职责不同，不需要对接方再
拼接第三份响应契约。

## 开发

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
```

代码结构：

```text
src/
├── erp_billing/  # 销售单领域、ToolSet、Port、Adapter、Prompt 和 MCP
└── gjp_common/   # 身份、固定端点、凭据、MCP、配置和日志
```

主要文档：

- [工具、API 与商品匹配](docs/architecture/ai-billing-tools-api-matching.md)
- [业务数据流](docs/architecture/business-data-flow.md)
- [AI 平台对接](docs/architecture/billing-mcp-integration-guide.md)
- [部署说明](docs/deployment/billing-mcp-service-deployment.md)
- [当前工具与提示词契约 ADR](docs/adr/0002-billing-mcp-tool-and-prompt-contract.md)
- [历史云开单边界 ADR](docs/adr/0001-cloud-billing-mcp-boundary.md)

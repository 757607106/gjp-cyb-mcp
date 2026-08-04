# GJP ERP AI 销售开单 MCP

基于 AgentScope 2.0.5 的完整销售单 MCP 服务，服务名为 `erp-billing`。

```text
AI 平台 / SaaS 对话页
  │ Authorization: Bearer <短期 MCP token>
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

URL 不进入 `InvocationContext`、CLI Token 请求或 Tool Schema。Bearer 仍按用户会话
隔离；账号、密码、验证码、Cookie 和 Token 都不进入模型可见工具参数。

## 销售单流程

业务必填项：

- 客户
- 出库仓库
- 经手人
- 录单日期（`YYYY-MM-DD`）
- 商品明细

备注可选，最多 200 字。接口必填的 `id=0` 与 `saveType` 由工具内部管理：草稿
`0`、预收 `1`、正式 `2`。

MCP 发布五个工具：

| 工具 | 作用 |
|---|---|
| `sync_products` | 同步当前账号可见商品到隔离会话内存 |
| `search_products` | 独立查询真实 ERP 商品 |
| `search_sales_order_options` | 查询客户、仓库或经手人候选 |
| `prepare_sales_order` | 返回缺失项、候选、商品匹配和不可变销售单预览 |
| `submit_sales_order` | 用户确认后以 `billing:write` 写入真实 ERP |

实际调用接口：

```text
GET  /product/page
GET  /customer/page
GET  /warehouse/page
GET  /staff/page
POST /sales/orders
```

只有 `prepare_sales_order` 返回 `readyToSubmit=true`，用户明确确认当前预览，并且
调用身份具有 `billing:write` 时，才允许提交。`submit_sales_order` 还要求唯一
`idempotency_key`；当前实现提供会话内防重，生产多副本应接入共享幂等存储。

## 本地 CLI 验证

CLI 不提供账号密码或验证码登录，只使用浏览器等渠道已取得的 ERP Token。启动
Token-only 验证服务：

```zsh
cd /Users/pusonglin/gjp-cyb-mcp
export ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
uv run uvicorn gjp_cli.billing_validation:app --host 0.0.0.0 --port 8102
```

另一个终端直接启动 CLI，按提示粘贴 ERP Token（输入不回显，可直接粘贴
`Bearer ...` 或裸 JWT，回车提交）：

```zsh
cd /Users/pusonglin/gjp-cyb-mcp
uv run python -m gjp_cli mcp-chat
```

如需运行 SaaS 对话模拟器，把上面命令中的 `mcp-chat` 改为 `demo`，启动后同样
会提示粘贴 Token。也支持 `--upstream-token` 或 `ERP_BILLING_UPSTREAM_TOKEN`
环境变量用于脚本自动化，但交互粘贴不会把 Token 留在 shell history 或进程
参数中。CLI 直接把 ERP JWT 作为 MCP Bearer 使用，无需换票。

已有 MCP Bearer 时直接使用：

```bash
uv run python -m gjp_cli mcp-chat \
  --url https://<billing-host>/mcp \
  --token '<ERP JWT>'
```

## 开发

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
uv run python -m gjp_cli doctor
```

代码结构：

```text
src/
├── erp_billing/  # 销售单领域、ToolSet、Port、Adapter、Prompt 和 MCP
├── gjp_cli/      # Token-only 本地对话和验证服务
└── gjp_common/   # 身份、固定端点、凭据、MCP、配置和日志
```

主要文档：

- [工具、API 与商品匹配](docs/architecture/ai-billing-tools-api-matching.md)
- [业务数据流](docs/architecture/business-data-flow.md)
- [AI 平台对接](docs/architecture/billing-mcp-integration-guide.md)
- [部署说明](docs/deployment/billing-mcp-service-deployment.md)
- [ADR](docs/adr/0001-cloud-billing-mcp-boundary.md)

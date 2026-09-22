# GJP ERP AI 业务 MCP

基于 MCP Python SDK 的 ERP 业务 MCP 服务，服务名为 `erp-billing`，覆盖管家婆
云创业版销售、采购、库存、资金往来与报表的对话式业务场景，共 59 个工具。

```text
AI 平台 / SaaS 对话页 / WorkBuddy
  │ ERP Bearer / X-API-Key / MCP OAuth token
  ▼
ERP 业务 MCP（ERP 长期凭据：app；OAuth：workbuddy_app）
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

URL 不进入 `InvocationContext` 或 Tool Schema。生产支持两条独立链路：直连接口逐
请求接收 ERP `Authorization: Bearer` 或 `X-API-Key` 长期凭据；OAuth 入口将 MCP
access token 与服务端绑定的 ERP 凭据分离。账号、密码、验证码、Cookie 和 Token
都不进入模型可见工具参数。

## 业务场景

五个业务域，按场景组织工具，不镜像 ERP 全部接口：

| 业务域 | 场景 |
|---|---|
| 销售 | 销售单开单、修改、作废、查询；销售退货（含快捷退货）；继续收款 |
| 采购 | 采购单开单、修改、作废、查询；采购退货；继续付款 |
| 库存 | 库存查询、库存流水、库存调拨、其他出入库（报损报溢）、预警与采购建议 |
| 资金往来 | 收款单、付款单（含核销）、应收/应付汇总与明细 |
| 报表分析 | 销售/采购/利润报表、财务状况、结算统计、客户对账（只读） |

基础资料（商品、客户、供应商、仓库、职员、结算账户）只开放只读查询。全部
工具清单与对应 ERP 接口域见 [AGENTS.md](AGENTS.md) 与
[工具、API 与商品匹配](docs/architecture/ai-billing-tools-api-matching.md)。

## 写操作两段式确认

所有写单据沿用 preview → submit：预览生成不可变 payload 并返回
`preview_id`，只有预览返回 `ready_to_submit=true` 且
`required_actions=["confirm_submit"]`、用户明确确认当前预览、并且调用身份
具有 `billing:write` 时才允许提交。`submit` 系列工具要求唯一
`idempotency_key`；当前实现提供会话内防重，生产多副本应接入共享幂等存储。
除销售单暴露 `save_type`（草稿 `0`、预收 `1`、正式 `2`）外，其余单据统一
`saveType=2` 保存过账，不暴露草稿态。

提示词只保留两个入口：`ERP_BILLING_MCP_INSTRUCTIONS` 由 MCP initialize 自动下发，
`ERP_BILLING_SYSTEM_PROMPT` 供 AI 平台装配 Agent，并复用前者后只补充跨工具确认、
安全与图片规则。工具选择和参数含义以实时 `tools/list` Schema 为准，不需要对接方再
拼接或维护第三份响应契约。

## 开发

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check src tests
```

服务器首次安装见[部署手册](docs/deployment/server-deploy-runbook.md)。完成首次安装后，
测试代码与生产代码分别一键更新：

```bash
./scripts/deploy.sh test
./scripts/deploy.sh production
```

`test` 对应 `origin/test`，`production` 对应 `origin/main`；两者默认更新同一个
`erp-billing-mcp` 服务槽位，不能同时运行。

代码结构：

```text
src/
├── erp_billing/  # ERP 业务领域、ToolSet、Port、Adapter、Prompt 和 MCP 入口
└── gjp_common/   # 身份、固定端点、凭据、MCP、配置和日志
integrations/workbuddy/  # WorkBuddy 连接器元数据与 erp-billing Skill
```

主要文档：

- [工具、API 与商品匹配](docs/architecture/ai-billing-tools-api-matching.md)
- [业务数据流](docs/architecture/business-data-flow.md)
- [AI 平台对接](docs/architecture/billing-mcp-integration-guide.md)
- [WorkBuddy Buddy 应用接入](docs/deployment/workbuddy-buddy-app.md)
- [WorkBuddy OAuth 适配边界 ADR](docs/adr/0003-workbuddy-oauth-adapter.md)
- [部署契约](docs/deployment/billing-mcp-service-deployment.md)
- [服务器首次部署](docs/deployment/server-deploy-runbook.md)
- [服务器日常运维](docs/deployment/server-service-ops.md)
- [当前工具与提示词契约 ADR](docs/adr/0002-billing-mcp-tool-and-prompt-contract.md)
- [历史云开单边界 ADR](docs/adr/0001-cloud-billing-mcp-boundary.md)

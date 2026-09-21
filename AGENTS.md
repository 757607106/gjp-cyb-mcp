# GJP Agent

基于 MCP Python SDK 的 ERP AI 业务 MCP 服务，覆盖管家婆云创业版的
开单、采购、库存、资金往来与报表对话场景。仓库只保留 ERP 业务产品、
MCP 工具基础设施和通用领域类型。

## 服务边界

| 产品 | ToolSet | 业务端口 | MCP 服务名 |
|---|---|---|---|
| ERP 业务服务 | `BillingToolSet` | `BillingApiPort` | `erp-billing` |

对接模式：

```text
ERP 业务产品 → AI 平台智能体 → 绑定 erp-billing MCP → 调用 ERP 业务 API
```

代码命名保持 `erp-billing` / `BillingToolSet` / `BillingApiPort` 历史名称，
不因业务范围扩展而重命名。

## 业务场景覆盖

MCP 只覆盖对话式业务场景，按场景组织工具，不镜像 ERP 全部接口。当前共
59 个工具，由 `BILLING_MCP_TOOL_NAMES` 白名单统一管理，导出为 camelCase。
覆盖范围以 ERP 测试环境 OpenAPI（655 个接口）核对结果为准，分为五个业务域：

| 业务域 | 场景 | 对接 ERP 接口域 |
|---|---|---|
| 销售 | 销售单开单、修改、作废、查询；销售退货（含快捷退货）；销售单继续收款 | `/sales/orders`、`/sales/returns`、`/sales/orders/{id}/quick-return`、`/sales/orders/{id}/receipt` |
| 采购 | 采购单开单、修改、作废、查询；采购退货；采购单继续付款 | `/purchase/orders`、`/purchase/returns`、`/purchase/orders/{id}/quick-return`、`/purchase/orders/{id}/payment` |
| 库存 | 库存查询（按商品/仓库/汇总）、库存流水、库存调拨、其他入库/出库（报损报溢）、库存预警与采购建议 | `/inventory/*`、`/inventory/alerts/*` |
| 资金往来 | 收款单、付款单（含核销）、应收/应付汇总与明细 | `/financial/receipt-orders`、`/financial/payment-orders`、`/financial/receivables`、`/financial/payables` |
| 报表分析 | 销售分析/明细/排行、采购统计/明细、利润统计、财务状况、结算统计、客户对账（只读） | `/sales/analysis`、`/sales/details`、`/sales/ranking`、`/purchase/statistics`、`/purchase/details`、`/financial/profits`、`/financial/status`、`/financial/settlements`、`/financial/reconciliation` |

基础资料（商品、客户、供应商、仓库、职员、结算账户）只开放**只读查询**，
用于开单时的候选检索；新建、修改、停用等写操作不属于 MCP 场景。

### 明确不覆盖

以下接口域不做进 MCP，扩展工具时不允许触碰：

- 小程序 / 商城 / POS（购物车、商城订单、POS 挂单与支付、轮播图、运费模板等）。
- 平台管理端（租户管理、平台认证、平台统计、优惠券、FAQ、公告、慢 SQL 等）。
- 认证登录类（登录注册、微信授权、AI 令牌管理）；凭据由服务端注入，见鉴权设计。
- 文件类（导入导出、打印数据、图片/视频上传）；生产 MCP 不处理媒体。
- 分享 H5 / 远程收款公开接口。
- 系统重建、角色 / 权限 / 部门管理、参数设置、期初库存、价格跟踪、盘点。
- 基础资料写操作（新增/修改/删除/导入）。

### 扩展约定

- **先核对契约再实现**：新增工具前先到 OpenAPI 核对路径、方法、请求/响应结构与
  `components.schemas` 约束，再定义 `BillingApiPort` 方法与 Adapter 映射。
- **写操作两段式**：所有写单据工具沿用 preview → confirm 确认模式，与现有
  销售单一致；未经确认不得直接写 ERP。
- **统一过账语义**：单据 `saveType` 统一用 `2`（保存过账）提交，不暴露草稿态
  （`0`）与预付（`1`）；需要单独生效的财务单据也在提交时一次过账，不拆草稿。
- **只读工具直接发布**：查询与报表类工具无副作用，不需要确认流程，但注意分页
  与时间范围参数，避免大结果集。
- **场景优先于接口**：一个工具对应一个完整业务动作（如「采购退货」可能组合
  quick-return 预填 + 退货创建），不为每个 REST 端点单独发一个工具。

## 鉴权与安全设计

- **生产禁止账号密码登录**：生产 MCP 使用 Bearer JWT / OAuth2。legacy 客户端直接使用 ERP JWT 作为 Bearer Token，可信接入方先完成鉴权，服务端只解析 payload 用于会话隔离并把原 Token 交给 ERP 最终鉴权；legacy 入口不得作为无访问控制的公网认证端点。
- **工具参数只含业务数据**：账号、密码、JWT、Cookie 和业务 Token 不进入 MCP 工具 JSON Schema，也不允许模型生成。
- **对接方处理鉴权**：服务通过 `BillingApiPort` 留出入口，由 Adapter 根据 `InvocationContext` 注入当前账套凭据。
- **身份隔离**：`InvocationContext` 不含凭据，通过 `ContextVar` 绑定当前异步任务，请求结束后恢复。
- **媒体边界**：生产 MCP 不处理音频、图片、附件、ASR 或 OCR，只接收文本。使用多模态模型（VL）时，Agent 按 `ERP_BILLING_SYSTEM_PROMPT` 第十二章图片识别规则直接读图并组装 `order_text`，无需独立 OCR 步骤；`source` 传 `image` 标记来源。语音仍由前端 ASR 转文本后传入。

## 技术栈

| 类别 | 选型 |
|---|---|
| MCP 框架 | MCP Python SDK（mcp >= 1.28，< 2，Streamable HTTP） |
| 语言 | Python >= 3.11 |
| 包管理 | uv + pyproject.toml |
| 模型支持 | OpenAI / Anthropic / DashScope / DeepSeek / Gemini / Moonshot / xAI / Ollama（多模态 VL 模型可直接读图开单） |

## 代码结构

```text
src/
├── erp_billing/  # ToolSet、Port、Adapter、Prompt、MCP 入口与领域代码
└── gjp_common/   # 上下文、连接、MCP、配置、路径与日志
integrations/workbuddy/gjp-erp-billing/  # WorkBuddy 连接器元数据与 erp-billing Skill
```

`Session` 是领域状态容器，不定义工具，不调用远端登录接口。`BillingToolSet`
是 MCP 的唯一工具来源。生产服务不构建模型，通过两个 ASGI 入口发布：

- `erp_billing.app:app` — 直连入口，Bearer JWT / X-API-Key 鉴权。
- `erp_billing.workbuddy_app:app` — WorkBuddy 入口，在直连入口之上增加
  WorkBuddy OAuth、ERP AI Token 绑定与 HTTP 保护层。

## 开发规范

### 架构原则

- 站在 Agent 应用开发架构师角度设计项目架构。
- 遵循 MCP Python SDK（modelcontextprotocol/python-sdk）官方语法，遇到问题先查官方文档。
- `mcp` 依赖使用稳定正式版本，当前约束 `>=1.28,<2`；升级时同步更新依赖锁文件并回归工具契约。
- 禁止过度设计，逻辑清晰易维护。
- 遇到设计问题应重构，不以兼容分支或临时补丁掩盖问题。
- 业务逻辑和测试逻辑严格分开，不遗留无关代码或文件。
- 生产 MCP 不构建模型，模型构建与 Agent 装配由对接方 AI 平台负责。

### 文档与文件组织

- 架构文档放在 `docs/architecture/`，部署文档放在 `docs/deployment/`。
- 工程文件命名与功能对应，代码功能概要使用中文注释。
- Git 提交信息使用中文。

### API 与框架参考

- ERP 测试环境 OpenAPI 文档：[API 接口与数据结构](https://test-ai.yuncyb.com/aicyberp-api/v3/api-docs)。后续新增或扩展 MCP 工具时，先核对接口路径、HTTP 方法、请求参数、响应结构及 `components.schemas` 中的数据约束，再实现 `BillingApiPort` 与 Adapter 映射。
- ERP 测试环境业务 API 基地址：`https://test-ai.yuncyb.com/aicyberp-api`；`/v3/api-docs` 是文档地址，不是业务请求基地址。生产地址由部署配置提供。
- MCP Python SDK 官方仓库：[modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk)。工具定义、FastMCP 服务组装与传输层以仓库 README 和文档为准，不直接套用其他框架示例。
- OpenAPI 用于核对契约，不代表所有接口都应发布为 MCP；按「业务场景覆盖」章节约定的范围扩展，凭据继续由服务端注入。

### 项目文档

- `docs/architecture/architecture-diagrams.md` — 系统架构图
- `docs/architecture/business-data-flow.md` — 业务数据与数据流
- `docs/architecture/saas-mcp-integration.md` — SaaS 对话页与 MCP 租户连接
- `docs/architecture/ai-billing-tools-api-matching.md` — 工具、ERP API 与商品匹配
- `docs/architecture/product-matching-algorithm.md` — 商品匹配算法
- `docs/architecture/mcp-reliability-update.md` — MCP 可靠性优化、2.0.7 升级与重试边界
- `docs/deployment/capability-deployment.md` — 鉴权与会话隔离约定
- `docs/deployment/billing-mcp-service-deployment.md` — 开单服务部署契约
- `docs/deployment/server-deploy-runbook.md` — legacy 服务首次部署
- `docs/deployment/server-service-ops.md` — legacy 与 WorkBuddy 日常运维
- `docs/deployment/workbuddy-buddy-app.md` — WorkBuddy 接入、部署与验收

## 本地开发

```bash
uv sync --extra dev
uv run pytest -q
```

### 本地启动服务

本地常驻启动复用 `scripts/deploy.sh` 的 nohup 方式，但**不要在本地直接运行
deploy.sh**：其 `pull_code` 会 `git reset --hard` 丢弃未提交改动，且写死
`/var/log` 与 Linux `ss` 命令。本地等价命令：

```bash
export GJP_ENV=local
export ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
export GJP_LOG_LEVEL=INFO   # 调试时改 DEBUG

nohup uv run uvicorn erp_billing.app:app \
    --host 127.0.0.1 --port 8102 \
    >> /tmp/erp-billing-mcp.log 2>&1 &
```

- MCP 端点 `http://127.0.0.1:8102/mcp`（Streamable HTTP），请求头带
  `X-API-Key` 与 `X-Conversation-Id`；就绪检查 `curl http://127.0.0.1:8102/healthz`。
- 日志 `tail -f /tmp/erp-billing-mcp.log`；停止
  `pkill -f "uvicorn erp_billing.app:app"`。
- 手动调工具可写临时脚本经 `mcp` 客户端 SDK 连接（initialize 握手后
  `call_tool`），或将端点配置进 MCP 客户端（Qoder 等）。
- WorkBuddy 入口换 `erp_billing.workbuddy_app:app`、端口 `8103`；本地手测
  优先用直连入口（WorkBuddy 需 OAuth 配置）。
- `submit*` / `update*` / `void*` 会真实写测试环境 ERP，手测后记得作废。

### 真实环境 e2e

`tests/e2e/` 需同时设置两个环境变量才启用（默认跳过，CI 不受影响）；
会启动真实服务子进程，对真实 ERP 测试环境覆盖销售、采购、退货、资金、
库存与报表全流程（可作废单据末尾作废）：

```bash
ERP_BILLING_E2E_API_KEY=<X-API-Key> \
ERP_BILLING_E2E_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api \
uv run pytest tests/e2e -v
```

### 环境区分

配置按 `GJP_ENV` 选择环境文件，系统环境变量始终优先于文件值：

| GJP_ENV | 加载文件 | 用途 |
|---|---|---|
| 缺省 / `local` | `config/local.env` | 本地开发，即测试环境 |
| `production` | `config/production.env` | 生产部署模板，敏感值由部署环境变量注入 |

`GJP_ENV_FILE` 显式指定文件时优先于上述选择；测试由
`tests/conftest.py` 隔离项目环境文件，不读取任何真实配置。

生产制品只从 wheel 安装（仅含 `src/erp_billing`、`src/gjp_common`），
不包含 `tests/`、`docs/`、`AGENTS.md` 与 `config/local.env`；详见
`docs/deployment/billing-mcp-service-deployment.md`。

### 分支策略

采用 `test` + `main` 双长期分支 + `feature/*` 短分支的轻量模型：

| 分支 | 职责 | 对应环境 |
|---|---|---|
| `main` | 只含已验收功能，每次发布打 tag；生产 wheel 只从 `main` 构建 | 生产 |
| `test` | 集成多个实验/验收中功能 | 测试环境 |
| `feature/*` | 单个功能开发，从 `test` 切出，合回 `test` | — |

工作流要点：
- 实验性功能从 `test` 切 `feature/*`，PR 合回 `test`，不直接碰 `main`。
- 功能验收通过、决定上生产时，对 `feature/*` 执行 `git rebase main` 后
  `git merge --ff-only` 合入 `main` 并打 tag；已混入 `test` 的改用 `cherry-pick`。
- 紧急修复从 `main` 切 `hotfix/*`，合回 `main` 打 tag 后同步回 `test`。
- `main` 禁止直接 push，必须经 PR 且 CI（`.github/workflows/ci.yml`）通过。
- Git 提交信息使用中文。

分支管「哪些功能出现在哪个分支」（功能级），与 `GJP_ENV` 管的「同一功能
在两环境怎么跑」（配置级）互补，不冲突。

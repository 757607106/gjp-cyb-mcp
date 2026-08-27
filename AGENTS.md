# GJP Agent

基于 AgentScope 2.0.5 的 ERP AI 开单 MCP 服务。仓库只保留开单产品、AgentScope
工具基础设施和通用领域类型。

## 服务边界

| 产品 | Agent | ToolSet | 业务端口 | MCP 服务名 |
|---|---|---|---|---|
| 开单服务 | ErpBillingAgent | `BillingToolSet` | `BillingApiPort` | `erp-billing` |

对接模式：

```text
开单产品 → AI 平台智能体 → 绑定 erp-billing MCP → 调用 ERP 商品 API
```

## 鉴权与安全设计

- **生产禁止账号密码登录**：生产 MCP 使用 Bearer JWT / OAuth2。MCP 客户端直接使用 ERP JWT 作为 Bearer Token，服务端从 JWT payload 解析身份。
- **工具参数只含业务数据**：账号、密码、JWT、Cookie 和业务 Token 不进入 AgentScope JSON Schema，也不允许模型生成。
- **对接方处理鉴权**：服务通过 `BillingApiPort` 留出入口，由 Adapter 根据 `InvocationContext` 注入当前账套凭据。
- **身份隔离**：`InvocationContext` 不含凭据，通过 `ContextVar` 绑定当前异步任务，请求结束后恢复。
- **媒体边界**：生产 MCP 不处理音频、图片、附件、ASR 或 OCR，只接收文本。使用多模态模型（VL）时，Agent 按 `ERP_BILLING_SYSTEM_PROMPT` 第八章图片识别规则直接读图并组装 `order_text`，无需独立 OCR 步骤；`source` 传 `image` 标记来源。语音仍由前端 ASR 转文本后传入。

## 技术栈

| 类别 | 选型 |
|---|---|
| Agent 框架 | AgentScope 2.0.5 |
| 语言 | Python >= 3.11 |
| 包管理 | uv + pyproject.toml |
| MCP 协议 | mcp >= 1.28（Streamable HTTP） |
| 模型支持 | OpenAI / Anthropic / DashScope / DeepSeek / Gemini / Moonshot / xAI / Ollama（多模态 VL 模型可直接读图开单） |

## 代码结构

```text
src/
├── erp_billing/  # 开单 Runtime、ToolSet、Port、Adapter、Prompt、MCP 与领域代码
└── gjp_common/   # 上下文、连接、MCP、配置、路径与日志
```

`Session` 是领域状态容器，不定义工具，不调用远端登录接口。`BillingToolSet` 是
Agent 与 MCP 的唯一工具来源。生产服务不构建模型，只通过
`erp_billing.mcp_service.create_billing_mcp_service()` 发布开单工具。

## 开发规范

### 架构原则

- 站在 Agent 应用开发架构师角度设计项目架构。
- 遵循 AgentScope 2.0.5 官方语法，遇到问题先查官方文档。
- 禁止过度设计，逻辑清晰易维护。
- 遇到设计问题应重构，不以兼容分支或临时补丁掩盖问题。
- 业务逻辑和测试逻辑严格分开，不遗留无关代码或文件。
- 生产 MCP 不构建模型，模型构建与 Agent 装配由对接方 AI 平台负责。

### 文档与文件组织

- 架构文档放在 `docs/architecture/`，部署文档放在 `docs/deployment/`。
- 工程文件命名与功能对应，代码功能概要使用中文注释。
- Git 提交信息使用中文。

### 主要文档

- `docs/architecture/architecture-diagrams.md` — 系统架构图
- `docs/architecture/business-data-flow.md` — 业务数据与数据流
- `docs/architecture/saas-mcp-integration.md` — SaaS 对话页与 MCP 租户连接
- `docs/architecture/ai-billing-tools-api-matching.md` — 工具、ERP API 与商品匹配
- `docs/architecture/product-matching-algorithm.md` — 商品匹配算法
- `docs/deployment/capability-deployment.md` — 鉴权与会话隔离约定
- `docs/deployment/billing-mcp-service-deployment.md` — 开单服务部署

## 本地开发

```bash
uv sync --extra dev
uv run pytest -q
```

### 真实环境 e2e

`tests/e2e/` 需同时设置两个环境变量才启用（默认跳过，CI 不受影响）；
会启动真实服务子进程并对真实 ERP 测试环境完成开单全流程（单据末尾作废）：

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

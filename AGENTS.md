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

- **生产禁止账号密码登录**：生产 MCP 使用 Bearer JWT / OAuth2。账号密码换票只允许存在于 `gjp_cli` 的本地或 test/live 验证服务。
- **工具参数只含业务数据**：账号、密码、JWT、Cookie 和业务 Token 不进入 AgentScope JSON Schema，也不允许模型生成。
- **对接方处理鉴权**：服务通过 `BillingApiPort` 留出入口，由 Adapter 根据 `InvocationContext` 注入当前账套凭据。
- **身份隔离**：`InvocationContext` 不含凭据，通过 `ContextVar` 绑定当前异步任务，请求结束后恢复。
- **媒体边界**：业务方在调用 Agent 前把语音和图片转换并确认为文本；生产 MCP 不处理音频、图片、附件、ASR 或 OCR。

## 技术栈

| 类别 | 选型 |
|---|---|
| Agent 框架 | AgentScope 2.0.5 |
| 语言 | Python >= 3.11 |
| 包管理 | uv + pyproject.toml |
| MCP 协议 | mcp >= 1.28（Streamable HTTP） |
| 模型支持 | OpenAI / Anthropic / DashScope / DeepSeek / Gemini / Moonshot / xAI / Ollama |

## 代码结构

```text
src/
├── erp_billing/  # 开单 Runtime、ToolSet、Port、Adapter、Prompt、MCP 与领域代码
├── gjp_cli/      # 本地 CLI、Agent 装配、账号换票和 test/live 验证服务
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
- 模型构建与 Agent 装配只放在 `gjp_cli`，生产 MCP 不构建模型。

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
- `docs/deployment/local-ngrok-mcp-guide.md` — 本地 ngrok 接入

## 本地开发

```bash
uv sync --extra dev
uv run pytest -q
uv run python -m gjp_cli demo
uv run python -m gjp_cli doctor
```

`gjp_cli` 只用于本地验证，不进入生产部署。

# GJP ERP AI 开单架构图

最后更新：2026-08-04

## 1. 系统边界

```mermaid
flowchart LR
    subgraph Business["业务方 SaaS"]
        Page["对话页"]
        Media["ASR / OCR / 文本确认"]
        Backend["业务后端"]
        Agent["AI Agent 平台"]
        Page --> Media --> Agent
        Backend --> Binding["短期 Bearer 会话绑定"]
    end

    subgraph Billing["ERP 开单部署单元"]
        MCP["erp-billing /mcp"]
        Identity["McpIdentityResolver"]
        Resolver["McpToolSetResolver"]
        Tools["BillingToolSet"]
        Catalog["会话内 ProductCatalog"]
        Matcher["ProductMatcher"]
        Port["BillingApiPort"]
    end

    Agent --> MCP
    Binding --> MCP
    MCP --> Identity --> Resolver --> Tools
    Tools --> Catalog --> Matcher
    FixedUrl["固定 ERP_BILLING_BASE_URL"] --> Port
    Tools --> Port --> ERP["ERP 商品 / 基础资料 / 销售单 API"]
```

业务方先把语音和图片转换为用户确认后的当前订单完整文本。生产 MCP 不提供媒体
上传、ASR、OCR 或附件工具。

## 2. 代码分层

```mermaid
flowchart TB
    Service["erp_billing/mcp_service.py"] --> MCP["gjp_common/mcp.py"]
    MCP --> Context["InvocationContext / ContextVar"]
    MCP --> ToolSet["BillingToolSet"]
    ToolSet --> Session["ErpBillingSession"]
    ToolSet --> Port["BillingApiPort"]
    Port --> Adapter["服务端 Adapter"]
    FixedUrl["固定 ERP API URL"] --> Adapter
    Adapter --> Credential["BusinessApiCredentialProvider"]
    Session --> Catalog["ProductCatalog"]
    Session --> Matcher["ProductMatcher"]
```

| 层 | 职责 |
|---|---|
| `erp_billing.mcp_service` | 创建只发布 `BillingToolSet` 的 MCP 应用 |
| `gjp_common.mcp` | 复用 AgentScope Tool Schema，按调用绑定身份和 ToolSet |
| `gjp_common.context` | 保存无凭据的租户、账号、会话和 scopes |
| `gjp_common.connections` | 校验固定 ERP 地址，并按会话解析 Bearer |
| `erp_billing` | 资料查询、商品匹配、销售单预览和写入 |

## 3. MCP 单次调用

```mermaid
sequenceDiagram
    autonumber
    participant Agent as Agent 平台
    participant MCP as 开单 MCP
    participant Identity as IdentityResolver
    participant Resolver as ToolSetResolver
    participant Tool as BillingToolSet
    participant Adapter as Billing Adapter
    participant ERP as 固定 URL ERP API

    Agent->>MCP: tools/call + Authorization
    MCP->>Identity: 验证短期 MCP Bearer
    Identity-->>MCP: InvocationContext
    MCP->>Resolver: resolve(context)
    Resolver-->>MCP: 隔离 ToolSet
    MCP->>Tool: bind_context + 业务参数
    opt 商品目录为空或显式同步
        Tool->>Adapter: fetch_products(context)
        Adapter->>ERP: 固定路径 + 服务端凭据
        ERP-->>Adapter: 当前账套商品
        Adapter-->>Tool: BillingProductSnapshot
    end
    Tool-->>Agent: structuredContent JSON
```

固定 Base URL、Bearer 和 Cookie 不进入 `InvocationContext`、工具参数、工具结果
或模型上下文。

## 4. 开单链路

```mermaid
flowchart TB
    ERP["ERP 商品 API"] --> Sync["syncProducts"] --> Catalog["会话内商品目录"]
    Query["商品关键词"] --> Search["searchProducts"] --> Matcher["确定性与模糊匹配"]
    Text["当前订单完整文本"] --> Draft["previewSalesOrder"] --> Matcher
    Catalog --> Matcher
    Matcher --> Confirmed["confirmedProducts"]
    Matcher --> Recommended["recommendedProducts / similarProducts"]
    Matcher --> Unmatched["unmatchedProducts"]
    Recommended --> Choice["用户选择 ptypeid"]
    Choice --> Retry["完整文本 + confirmed_products"] --> Draft
    Draft --> Preview["不可变 preview"] --> UserConfirm{"用户明确确认?"}
    UserConfirm -->|是| Submit["submitSalesOrder"] --> SalesApi["POST /sales/orders"]
```

工具还包含 `search_sales_order_options` 与 `submitSalesOrder`。服务不生成草稿文件；
只有确认提交工具在满足 `billing:write`、当前预览和幂等键后写入 ERP。

## 5. 隔离规则

- ToolSet 和商品目录按 `(tenant_id, account_id, session_id)` 隔离。
- MCP Bearer 只标识当前会话；ERP Bearer 只存在于服务端凭据存储。
- ERP API URL 是部署级固定配置，不按会话解析。
- Adapter 只接收源码中固定的相对路径，拒绝模型提供完整 URL。
- 工具 Schema 不含身份、地址、鉴权、音频、图片、附件或文件路径字段。
- `confirmed_products` 必须引用当前租户目录中的真实商品。

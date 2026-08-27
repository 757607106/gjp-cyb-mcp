# 销售开单 MCP 与 AI 平台对接

## 服务信息

| 项目 | 值 |
|---|---|
| MCP Server | `erp-billing` |
| Streamable HTTP | `POST /mcp` |
| SSE | `GET /sse` |
| 工具数 | 10 |
| 读取权限 | `billing:read` |
| 写单权限 | `billing:write` |

生产环境使用 HTTPS。AI 平台在 MCP Header 中直接传 ERP JWT / OAuth2 Bearer：

```text
Authorization: Bearer <ERP JWT / OAuth2 token>
```

建议每次 MCP 请求同时携带对话标识，使同一用户的不同对话窗口各自隔离
预览与幂等缓存（未携带时按登录账号隔离）：

```text
X-Conversation-Id: <对话/会话标识，最长 64 字符>
```

ERP API URL 是部署级固定值 `ERP_BILLING_BASE_URL`，不随租户、请求或 Tool 调用
变化。会话存储只需要按 `InvocationContext` 解析当前上游 Bearer。

```mermaid
sequenceDiagram
    participant U as 用户
    participant A as 三方 Agent
    participant M as erp-billing MCP
    participant C as Bearer Provider
    participant E as 固定 URL ERP API

    U->>A: 商品与销售单信息
    A->>M: previewSalesOrder + ERP Bearer
    M->>C: 按 InvocationContext 解析 ERP Bearer
    M->>E: GET 商品/客户/仓库/职员
    E-->>M: 当前账号真实数据
    M-->>A: 缺失项/候选/商品匹配/preview
    A-->>U: 展示预览并询问确认
    U->>A: 明确确认
    A->>M: submitSalesOrder
    M->>E: POST /sales/orders
    E-->>M: 单据 ID（内部）
    M->>E: GET /sales/orders/{id} 回查业务单号
    M-->>A: submitted=true + order_no（业务单号）
```

## 工具契约

| 工具 | 说明 |
|---|---|
| `syncProducts` | 主动刷新隔离会话商品目录 |
| `listProducts` | 分页浏览当前会话商品目录 |
| `searchProducts` | 独立查商品；模糊结果只推荐 |
| `searchBillingReferences` | `reference_type` 为 customer/warehouse/handler |
| `previewSalesOrder` | 校验完整销售单并生成不可变预览 |
| `submitSalesOrder` | 明确确认后真实写单 |
| `getSalesOrder` | 查询销售单详情 |
| `listSalesOrders` | 分页查询销售单列表 |
| `voidSalesOrder` | 明确确认后作废单据 |
| `updateSalesOrder` | 先查询详情，明确确认后修改单据 |

业务必填项：客户、出库仓库、经手人、录单日期和商品明细。备注可选。接口技术字段
`id=0` 由工具内部生成；模型只传 `draft`、`pre_receipt`、`final`，工具再映射 ERP
的 `saveType=0/1/2`。

## 提示词配置

- `ERP_BILLING_MCP_INSTRUCTIONS`：服务端自动放入 MCP initialize 响应，对接方无需
  复制或手工拼接。
- `ERP_BILLING_SYSTEM_PROMPT`：AI 平台 Agent 的完整系统提示词，包含表格输出、
  工具路由、确认和图片识别规则。

只使用这两个入口；不存在独立的第三份响应契约。若 AI 平台已配置完整 System
Prompt，仍保留 MCP Instructions，因为它属于 MCP 服务自描述，而不是重复的 Agent
配置。

准备调用示例：

```json
{
  "order_text": "牛肉2斤，土豆5斤",
  "customer": "客户甲",
  "warehouse": "一号仓",
  "handler": "张三",
  "order_date": "2026-08-04",
  "remark": "下午送达",
  "save_type": "final",
  "source": "text",
  "confirmed_products": []
}
```

Agent 根据返回值处理：

- `required_actions`：严格按数组顺序处理当前全部待办；仅出现
  `confirm_submit` 时进入最终提交确认。
- `missing_required_fields`：继续追问必填项。
- `reference_resolutions`：根据状态展示客户/仓库/经手人候选，不自行选择；该字段是
  基础资料解析结果的唯一来源，避免重复候选数据。
- `recommended_products`：展示商品候选，确认后重调完整准备请求。
- `unmatched_products`：请用户修改名称或检查 ERP 商品。
- `unit_warnings`：请用户按 ERP 单位重新确认数量，不猜测换算。
- `ready_to_submit=true`：展示 `preview` 并询问是否确认提交。

`searchBillingReferences` 默认每页 5 条并返回 `page`、`page_size`、`total`、
`has_more`；候选已按匹配质量排序，只返回名称和默认标记。客户未匹配时应换用
更准确的关键词，不得用空关键词枚举完整客户列表。`preview.items[].line_amount` 和
`preview.total_amount` 由 MCP 服务计算；价格不完整时不返回合计。

提交调用示例：

```json
{
  "preview_id": "sales-preview-...",
  "idempotency_key": "tenant-1-conversation-42-v1",
  "confirmed_by_user": true
}
```

只有用户看到当前预览后明确确认，才可传 `confirmed_by_user=true`。任何内容修改都
必须重新准备并使用新的 `preview_id`。

## AgentScope 2.0.5 客户端

```python
from agentscope.mcp import HttpMCPConfig, MCPClient
from agentscope.tool import Toolkit

client = MCPClient(
    name="erp-billing",
    is_stateful=False,
    mcp_config=HttpMCPConfig(
        url="https://<billing-host>/mcp",
        headers={"Authorization": "Bearer " + mcp_bearer},
    ),
)
toolkit = Toolkit(mcps=[client])
```

## 生产凭据提供者

```python
from gjp_common.connections import BusinessApiCredential

class BillingCredentialProvider:
    def resolve(self, context):
        record = session_repository.require(
            tenant_id=context.tenant_id,
            account_id=context.account_id,
            session_id=context.session_id,
        )
        return BusinessApiCredential(
            kind="bearer",
            value=secret_manager.decrypt(record.encrypted_bearer),
        )
```

URL 不在会话记录中。HTTP 客户端在启动时绑定固定地址：

```python
http = BusinessAuthenticatedJsonClient(
    base_url=settings.erp_billing_base_url,
    credential_provider=BillingCredentialProvider(),
)
```

## 安全检查

- Tool Schema 不出现 URL、账号、密码、验证码、JWT、Cookie 或 Token。
- 固定 ERP URL 为 HTTPS，且不含 query、fragment 或用户信息。
- MCP Bearer 即 ERP JWT，服务端从 payload 解析身份。
- `billing:write`、用户确认、当前预览和幂等键缺一不可。
- 多副本把预览与幂等结果迁移到共享存储。

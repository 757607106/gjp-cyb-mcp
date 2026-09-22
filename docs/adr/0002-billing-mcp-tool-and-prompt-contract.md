# ADR 0002：销售开单 MCP 工具与提示词契约

- 状态：已采纳；决策 1 的十工具清单已被 2026-09-20 五大业务域 59 工具扩展
  取代（框架同步迁移至 MCP Python SDK），鉴权、两段式确认与提示词契约仍然有效
- 日期：2026-08-27
- 替代：ADR 0001 中的五工具边界与双 Bearer 设计

## 背景

销售开单能力已经从新增单据扩展到商品浏览、基础资料查询、销售单查询、修改和
作废。旧文档仍记录五个历史工具和独立 MCP Bearer，且预览响应存在重复候选与静态
字段，增加了模型上下文和对接歧义。

## 决策

1. MCP 服务名保持 `erp-billing`，发布以下十个工具：
   `syncProducts`、`listProducts`、`searchProducts`、
   `searchBillingReferences`、`previewSalesOrder`、`submitSalesOrder`、
   `getSalesOrder`、`listSalesOrders`、`voidSalesOrder`、`updateSalesOrder`。
2. 生产 MCP 客户端直接使用 ERP JWT / OAuth2 Bearer。服务端从 payload 解析无凭据
   `InvocationContext`，原 Bearer 只由服务端凭据提供者注入固定地址的 ERP API。
3. `previewSalesOrder` 以 `required_actions` 作为下一步的唯一有序决策契约：只有
   `required_actions=["confirm_submit"]` 且 `ready_to_submit=true` 才进入提交确认。
4. 缺失项保留在 `missing_required_fields`，基础资料解析结果只保留在
   `reference_resolutions`，商品结果保留在三个商品匹配数组；不再返回可由这些字段
   推导出的 `field_requirements`、`needs_confirmation` 或候选排序标签。
5. 基础资料候选由 MCP 内部按精确命中、默认项、名称相关度排序，对外只返回名称和
   默认标记。内部 ID 仅用于服务端构造 ERP Payload。
6. 商品、基础资料和销售单分页统一返回 `page`、`page_size`、`total`、`has_more`。
7. 预览行金额由 MCP 按实际提交单价逐行保留两位小数；只有全部商品都有价格时才
   返回 `total_amount`，Agent 不自行补算金额。
8. `confirmed_products` 只接受 Tool Schema 声明的数组格式，每项必须包含
   `line_id` 和 `product_id`，不保留字典、JSON 字符串或 camelCase 兼容分支。
9. 提示词只保留两个入口：`ERP_BILLING_MCP_INSTRUCTIONS` 由 initialize 自动下发，
   `ERP_BILLING_SYSTEM_PROMPT` 供 AI 平台配置；不再拆分第三份响应契约。
10. MCP 只接收文本业务参数。VL Agent 可直接读取图片并组装 `order_text`，语音由
    前端 ASR 转文本；账号、密码、Token、Cookie、文件和媒体均不进入 Tool Schema。
11. 第三方客户端无需配置额外 System Prompt 即可理解工具：`tools/list` 为每个工具
    提供中文 `title`、用途与相邻工具边界；参数含义、来源、默认值和约束只在
    `inputSchema` 中声明一次，顶层对象拒绝未知字段。
12. 工具行为通过 annotations 明确表达：查询和预览为只读，提交为可幂等的新增，
    修改与作废为破坏性操作，所有 ERP 工具均标记为可能访问外部系统。
13. `tools/call` 使用双通道结果：`content` 是可直接展示的中文 Markdown，单值对象
    用纵向表格、列表用横向表格；`structuredContent` 保留 Agent 后续调用需要的结构化
    字段。内部 ID、预览令牌和控制字段不进入展示文本。
14. 有显式 `inputSchema` 的工具不再在函数 docstring 重复维护 `Args:`；参数说明以
    Schema 为唯一来源。`ERP_BILLING_SYSTEM_PROMPT` 直接复用 MCP Instructions，
    只补充 Schema 无法表达的跨工具策略，避免三处文案漂移。

## 结果

- Agent 只需处理一个有序待办数组和一份基础资料候选，响应更短且不易误判。
- 工具列表、鉴权边界和提示词入口在 README、架构文档、部署文档中保持一致。
- 删除不可达兼容分支和未读取的草稿字段，领域模型只保留开单流程实际消费的数据。
- 写操作仍要求 `billing:write` 与明确用户确认；新增单据额外要求当前不可变预览和
  幂等键。
- 不依赖对接方提示词进行基础排版或脱敏；不支持 `structuredContent` 的客户端也能
  直接展示 `content`，支持结构化结果的 Agent 仍可完成多轮预览与提交。

## 非目标

- 不修改对接方 Agent、聊天 UI 或 Markdown 渲染器。
- 不在生产 MCP 中构建模型、执行 OCR/ASR 或处理媒体文件。
- 不改变 ERP API 路径、保存类型映射或写操作确认语义。

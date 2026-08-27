# ADR 0002：销售开单 MCP 工具与提示词契约

- 状态：已采纳
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

## 结果

- Agent 只需处理一个有序待办数组和一份基础资料候选，响应更短且不易误判。
- 工具列表、鉴权边界和提示词入口在 README、架构文档、部署文档中保持一致。
- 删除不可达兼容分支和未读取的草稿字段，领域模型只保留开单流程实际消费的数据。
- 写操作仍要求 `billing:write` 与明确用户确认；新增单据额外要求当前不可变预览和
  幂等键。

## 非目标

- 不修改对接方 Agent、聊天 UI 或 Markdown 渲染器。
- 不在生产 MCP 中构建模型、执行 OCR/ASR 或处理媒体文件。
- 不改变 ERP API 路径、保存类型映射或写操作确认语义。

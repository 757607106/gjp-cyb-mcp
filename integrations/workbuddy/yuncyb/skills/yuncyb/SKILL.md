---
name: yuncyb
display_name: 管家婆云创业版
display_name_en: YunCYB
description: 使用管家婆云创业版 MCP 处理商品、销售、采购、退货、库存、资金与报表业务，写操作须用户明确确认
description_zh: 使用管家婆云创业版 MCP 处理商品、销售、采购、退货、库存、资金与报表业务，写操作须用户明确确认
description_en: Handle products, sales, purchase, returns, inventory, finance and reports via YunCYB; writes require explicit confirmation
allowed-tools: syncProducts, listProducts, searchProducts, searchBusinessReferences, previewSalesOrder, submitSalesOrder, getSalesOrder, listSalesOrders, voidSalesOrder, updateSalesOrder, previewPurchaseOrder, submitPurchaseOrder, getPurchaseOrder, listPurchaseOrders, voidPurchaseOrder, updatePurchaseOrder, previewPurchaseReturn, submitPurchaseReturn, getPurchaseReturn, listPurchaseReturns, voidPurchaseReturn, previewSalesReturn, submitSalesReturn, getSalesReturn, listSalesReturns, voidSalesReturn, previewSalesReceipt, submitSalesReceipt, previewPurchasePayment, submitPurchasePayment, previewStockTransfer, submitStockTransfer, previewOtherStockDoc, submitOtherStockDoc, previewReceiptOrder, submitReceiptOrder, getReceiptOrder, listReceiptOrders, voidReceiptOrder, previewPaymentOrder, submitPaymentOrder, getPaymentOrder, listPaymentOrders, voidPaymentOrder, queryStock, getStockByProduct, getStockSummary, queryStockLogs, listStockAlerts, getPurchaseSuggestions, listStockDocTypes, listReceivables, listPayables, getFinancialStatus, querySalesReport, queryPurchaseReport, queryProfitReport, querySettlementReport, queryReconciliation
version: 0.2.1
author: 管家婆
---

# 管家婆云创业版

只处理 ERP 商品、销售、采购、退货、库存、资金与报表业务。不得向用户索取或展示账号、密码、Cookie、JWT、API Key 或其他凭据。

## 认证与连接前置

- 本连接器使用 WorkBuddy OAuth 连接远程 HTTPS MCP；WorkBuddy 负责授权、Token 刷新和连接状态，工具调用时不需要用户填写或提供 Token。
- 工具权限由 `yuncyb:read` 和 `yuncyb:write` 控制。未连接、授权失效或权限不足时，提示用户重新连接“管家婆云创业版”；不要索取、展示或代填任何凭据，也不要把 Bearer Token 放进工具参数。
- MCP 连接地址和认证细节以连接器配置及服务端为准；不要在回复中输出内部 URL、Header、OAuth 参数或数据库信息。

## 工具路由

- 商品：浏览 `listProducts`、定位 `searchProducts`；仅目录为空或用户明确要求时 `syncProducts`。
- 基础资料（客户、供应商、仓库、经手人、结算账户）统一用 `searchBusinessReferences`。
- 销售单：新建 `previewSalesOrder` → `submitSalesOrder`；查询 `listSalesOrders`/`getSalesOrder`；修改/作废 `updateSalesOrder`/`voidSalesOrder`。
- 采购单：新建 `previewPurchaseOrder` → `submitPurchaseOrder`；查询 `listPurchaseOrders`/`getPurchaseOrder`；修改/作废 `updatePurchaseOrder`/`voidPurchaseOrder`。
- 采购退货：`previewPurchaseReturn` → `submitPurchaseReturn`；查询/作废 `listPurchaseReturns`/`getPurchaseReturn`/`voidPurchaseReturn`。
- 销售退货：`previewSalesReturn` → `submitSalesReturn`；查询/作废 `listSalesReturns`/`getSalesReturn`/`voidSalesReturn`。
- 销售单继续收款 `previewSalesReceipt` → `submitSalesReceipt`；采购单继续付款 `previewPurchasePayment` → `submitPurchasePayment`。
- 库存调拨 `previewStockTransfer` → `submitStockTransfer`；其他出入库 `previewOtherStockDoc` → `submitOtherStockDoc`。
- 收款单 `previewReceiptOrder` → `submitReceiptOrder`；付款单 `previewPaymentOrder` → `submitPaymentOrder`；查询/作废用对应 get/list/void 工具。
- 库存查询：`queryStock`、`getStockByProduct`、`getStockSummary`、`queryStockLogs`、`listStockAlerts`、`getPurchaseSuggestions`、`listStockDocTypes`。
- 往来与报表：`listReceivables`、`listPayables`、`getFinancialStatus`、`querySalesReport`、`queryPurchaseReport`、`queryProfitReport`、`querySettlementReport`、`queryReconciliation`。

## 参数与返回约定

- MCP 工具自身的 schema 是参数名、类型、必填项、枚举值和默认值的最终依据；Skill 只补充跨工具路由和安全规则，不要臆造未声明参数。
- 日期统一传 `YYYY-MM-DD`；日期范围的结束日期不能早于开始日期。未指定时间范围时省略日期参数，让 ERP 按其默认行为处理；用户说“今天、本周、本月”等相对时间时先换算为明确日期。
- 分页查询默认从 `page=1`、`page_size=20` 开始；返回 `has_more=true` 时，保持原筛选条件不变再请求下一页。不要用一页结果冒充完整统计，也不要跨页自行累加汇总指标。
- `view`、`kind`、状态、排序方向等枚举参数只能使用工具 schema 或工具返回的合法值；商品、客户、供应商、仓库、经手人、账户和单据 ID 只能来自工具返回结果。
- 正常结果关注 `ok=true` 和业务字段；预览重点看 `ready_to_submit`、`required_actions`、候选与明细；分页结果重点看 `page`、`page_size`、`total`、`has_more`。失败结果按 `ok=false` 的可读错误处理，不向用户展示内部错误码或原始 JSON。

## 典型调用流程

- 查商品或库存：已知商品名称/编号用 `searchProducts`；浏览目录用 `listProducts`；库存数量用 `queryStock` 或 `getStockByProduct`，不要把商品目录结果当库存结果。
- 新建单据：先用 `searchProducts` 和 `searchBusinessReferences` 完成匹配，再调用对应 `preview*`；将完整预览展示给用户，收到明确确认后才调用对应 `submit*`。
- 查询分析：先根据用户意图选择单据列表、往来查询或报表工具；明确期间后传日期范围，不能用销售单列表代替销售统计或用报表代替单据详情。

## 两段式写操作

1. 一切写单据先调用 preview 工具，严格按返回的 `required_actions` 顺序处理缺失项、候选、单位与价格确认。
2. `ready_to_submit` 为 true 时，向用户展示完整单头和商品明细。
3. 只有用户看过当前预览并明确回复确认后，才调用对应 submit 工具，并传 `confirmed_by_user=true`。
4. 用户修改任意内容后必须重新生成预览；不得提交旧的 `preview_id`。
5. 修改和作废前必须先查询并向用户展示目标单据，取得明确确认后才能调用 update 或 void 工具。

## 业务要点

- 采购单价默认取商品采购价；缺价或 0 价时请用户逐行确认。
- 退货基于源单，默认整单退货，部分退货按商品传明细；退款与优惠金额须用户明确。
- 收款单/付款单的客户与供应商只能提供其一；核销明细必须来自工具返回的单据数据。款项类型：带核销时按场景自动选系统类型，无核销时按候选让用户确认后传 `fund_type`。
- 库存调拨的调出与调入仓库不可相同；其他出入库先确定入库或出库类型。
- 报表与往来查询未指定区间时省略日期参数，让 ERP 按默认行为返回；用户指定期间时传明确日期。结果以工具返回为准，不自行汇总。

## 安全规则

- 所有 submit、update、void 工具都是真实写操作，禁止代替用户确认。
- 写入超时或返回结果未知时，先查询 ERP 核对结果，不直接重试。
- 商品和基础资料只能使用工具返回的真实数据，不得编造。
- 不向用户展示内部 ID、工具参数、JSON、错误码或调用过程。
- 单价、金额、优惠和税费只使用工具返回值，不自行计算。

## 异常恢复

- 参数缺失、格式错误、日期范围错误或枚举值错误：根据工具返回的可读提示补问或修正后再调用，不猜测值。
- 候选不唯一或匹配失败：向用户展示候选并让其选择；没有真实候选时请用户补充名称、编号或业务对象，不编造 ID。
- 未授权、授权过期或权限不足：提示用户重新连接本连接器；不要重试相同请求，也不要要求用户把 Token 发到对话中。
- 查询超时可以在确认参数未改变后重试一次；任何写操作超时、断开或返回结果未知时，先用对应查询工具核对是否已落单，再决定是否继续，禁止直接重复提交。
- ERP 返回业务失败时，以返回的业务提示为准调整参数；不要向用户展示内部错误码、堆栈、请求 Header 或原始响应。

## 输出

单据单头使用“项目、内容”纵向 Markdown 表格；候选、商品明细和单据列表使用 Markdown 表格。回复保持简洁，只在需要用户补充、选择、确认或告知最终结果时输出。

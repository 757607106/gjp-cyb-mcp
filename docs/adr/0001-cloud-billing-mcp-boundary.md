# ADR 0001：第三方 Agent 开单 MCP 与云创业版商品接口边界

- 状态：已采纳
- 日期：2026-08-04

## 背景

第三方 Agent 平台需要通过标准 MCP 调用开单能力。商品匹配必须使用云创业版
`GET /aicyberp-api/product/page` 返回的当前租户真实商品，而不是旧版 ERP
`GetPtypeAll` 接口或本地静态商品文件。

## 决策

1. 使用 AgentScope 2.0.5 的 Stateless HTTP MCP Client 接入方式；服务端通过
   MCP SDK 发布 `/mcp`，服务名为 `erp-billing`。
2. MCP 发布 `sync_products`、`search_products`、`search_sales_order_options`、
   `prepare_sales_order`、`submit_sales_order` 五个工具。
3. 商品 Adapter 固定调用 `GET /product/page`，默认 `pageSize=20`、`status=1`，
   按 `data.total` 自动翻页并从 `data.list` 归一化商品。
4. 客户、出库仓库和经手人分别通过 `GET /customer/page`、
   `GET /warehouse/page`、`GET /staff/page` 精确解析；真实写单固定调用
   `POST /sales/orders`。
5. ERP API URL 是部署级固定配置 `ERP_BILLING_BASE_URL`，不从会话、Token 登记
   请求或 Tool 参数读取。
6. 第三方平台携带的 MCP Bearer 只用于识别租户、账号和会话；云创业版上游
   Bearer 只保存在服务端连接存储中。两类 Bearer 不复用，也不进入 Tool Schema、
   模型上下文或工具结果。
7. 生产 MCP 不提供上游 Token 登记入口，不提供账号密码或验证码登录，也不接受
   动态 ERP URL。
8. 每个 `(tenant_id, account_id, session_id)` 使用隔离的 `BillingToolSet`、
   `ErpBillingSession` 和内存商品目录。
9. 业务必填项是客户、出库仓库、经手人、录单日期和商品明细；备注可选。
   `id=0` 与 `saveType` 由工具管理，草稿/预收/正式分别映射 `0/1/2`。
10. 写单必须使用 `billing:write`、当前不可变预览、用户明确确认和幂等键。

## 结果

- 第三方平台只需配置 MCP URL 和短期 MCP Bearer。
- 商品匹配始终基于当前租户的云创业版真实商品。
- Agent 可以完成从资料追问、候选确认、预览到真实提交的完整销售单流程。
- 上游业务凭据不会暴露给 Agent，且可以独立轮换和撤销。
- 商品同步可能产生多次分页请求；会话内目录会复用到后续查询和开单匹配。

## 非目标

- MCP 不处理音频、图片、OCR 或 ASR。
- MCP 不处理收款明细、折扣账户、商城订单或销售退货；当前仅覆盖新增销售单的
  基础抬头、商品明细、备注和三种保存类型。

# MCP 设计与实现审查（2026-09-10）

## 结论与范围

当前“平台负责模型与对话，MCP 负责确定性开单能力，ERP 负责业务落库”的边界合理，应保留。10 个业务工具覆盖查询、预览、提交、修改、作废，没有必要引入另一套 Agent、工作流引擎或向量数据库。但当前实现存在凭据进入身份日志、并发重复开单、提交结果不确定、预览和输入校验不足等问题，不能仅凭现有测试通过就认定生产边界完备。

本次分析当前工作区的 src、相关测试和部署说明，核对本地实际安装的 AgentScope 2.0.5 与 MCP SDK 源码，并读取下列资料：

- [ERP OpenAPI](https://test-ai.yuncyb.com/aicyberp-api/v3/api-docs)：本次成功获取，重点核对 paths 与 components.schemas；没有调用真实开单、修改或作废 API。
- [AgentScope 2.0.5 官方文档](https://docs.agentscope.io/versions/2.0.5/zh)：核对框架定位；FunctionTool、ToolBase、权限与执行细节另以本地 2.0.5 源码核实。

本次只修改文档，不修改运行时代码。工作区原有 tests/e2e/test_real_billing_mcp.py 改动以及两个未跟踪测试文件保持原样；缺陷测试的结果属于当前工作区，不代表已提交分支或 CI 的状态。

## 各环节判断

| 环节 | 判断 | 依据与最小调整 |
|---|---|---|
| 装配与产品边界 | 合理 | app.py 只装配身份、ToolSet、HTTP Adapter；生产不构建模型。src 中没有实际 ErpBillingAgent，AGENTS 的 Agent 名称属于文档遗留，不应据此另造 Agent |
| 工具定义 | 基本合理 | BillingToolSet 统一提供 AgentScope FunctionTool 与 MCP schema；维持业务任务粒度，无需把全部 ERP API 一对一发布 |
| MCP 传输 | 基本合理 | Streamable HTTP 使用 stateless=True；业务预览仍有状态。SSE 仅在确有旧客户端时保留；HTTP 无状态不等于可随意多进程部署 |
| 身份和凭据 | 必须修正 | ContextVar 绑定/恢复正确，但 API Key 被写入 tenant_id 等身份字段，破坏了“不含凭据”的约定 |
| 权限 | 有边界条件 | 解析器统一授予 read/write；ERP 必须继续校验实际数据权限。confirmed_by_user 由模型填写，只能表达调用契约，不能证明真人点击确认 |
| 会话 | 单进程可用 | ToolSet 有数量与 TTL 上限；对话 ID 可省略、截断，凭据存储不淘汰，预览无业务级过期时间 |
| 商品同步 | 方向合理，有并发缺口 | 租户共享、过期继续读旧目录可减少等待；首次并发加载仍重复全量同步；searchProducts 不经过 ensure_catalog |
| 商品匹配 | 合理 | 唯一精确/别名匹配自动采用，模糊结果让用户选择。当前规模先保留确定性算法，不引入检索服务 |
| 文本解析 | 需要收紧 | 正则适合标准化商品行，但“各”硬切词、自定义单位空格分隔、负数量、缺省数量存在语义风险 |
| 基础资料 | 需要小幅修正 | 真实查询和唯一匹配正确；丢弃编号/ID、按名称前缀去重和仅检索前 10 条会降低消歧能力 |
| 预览 | 核心设计合理 | 保存 payload 深拷贝、提交只接 preview_id，可防提交阶段改字段；但新预览不会废弃旧预览，数量约束未完整执行 |
| 提交 | 高优先级修正 | 幂等仅缓存已完成结果，await 远端调用前没有互斥；超时后不能确定远端是否创建成功 |
| 修改与作废 | 边界基本合理 | 写权限、明确确认、单号解析俱全；修改金额和保存状态应对齐 API，完整明细修改需防模型漏行 |
| HTTP Adapter | 合理 | 固定部署 URL、每次注入认证、复用连接池、TLS 校验，业务错误集中映射；不自动重试写操作是正确的 |
| 错误与日志 | 需要修正 | 业务错误结构统一，但 ok=false 仍可能表现为 MCP isError=false，并记录“调用成功”；普通日志会暴露 API Key |
| 部署和资源 | 先保持简单 | 健康检查仅证明进程存活；目录和凭据字典无上限，后台刷新任务关停未主动取消/等待。先修本地生命周期，不默认加 Redis |

## 需要优先处理的具体问题

### 1. API Key 在普通日志中泄漏（高）

位置：erp_billing/app.py 的 ApiKeyIdentityResolver.resolve；gjp_common/mcp.py 的 call_tool 日志。

API Key 原文被赋给 tenant_id、subject_id、account_id。MCP 的 INFO 日志直接输出 tenant/account，异常与大结果日志也输出 tenant；因此即使关闭 DEBUG 凭据转储仍然会泄漏。使用合成测试 Key 已验证身份字段等于原始 Key。

最小修正：优先采用可信认证返回的租户/主体标识；如果当前 ERP 没有身份查询入口，可先以稳定摘要作为隔离键，原始 Key 只放凭据容器。摘要解决泄漏，不等于验证了 Key。API Key 目前由 ERP 在真正业务调用时验证，缓存读取前没有重新验证撤销状态，需要明确有效期策略。不要把任意非空 Key 解析成功描述为认证成功。

### 2. 并发可以重复开单（高）

位置：toolset.py 的 submit_sales_order；session.py 的 submission_result/remember_submission/consume_prepared_sales_order。

两个请求可以同时读到“未提交”和有效预览，随后同时 await create_sales_order。成功缓存和消费预览都发生在远端调用之后。同一个 key，以及不同 key 提交同一预览，均存在重复写入窗口。现有工作区测试对此有两个 xfail，用例实际仍失败。

FunctionTool 的 is_concurrency_safe=False 是 AgentScope 调度元数据，当前 MCP invoke_raw 路径没有自动执行它，也没有任何会话提交锁。

最小修正：在会话/预览范围加 asyncio.Lock，将幂等检查、预览检查和提交结果落内存包在同一临界区；一旦拿到远端成功 ID，先保存成功结果并消费预览，再做业务单号回查。单进程先解决，不需要先上分布式锁。

### 3. 超时不等于未创建（高）

位置：adapters.py 的 _request_json；toolset.py 的 submit_sales_order；prompt.py 的“失败必须明确未创建”。

POST 已在 ERP 落库但响应超时，客户端会得到 business_upstream_unavailable；本地未记录成功，预览仍可重试。本地 idempotency_key 没有传到 ERP，重试可能再次创建。进程退出、会话 TTL 淘汰也会丢失本地提交记录。单纯加锁不能解决这个问题。

最小修正：区分明确失败与提交结果未知；未知时提示核对销售单，不承诺未创建、不直接重发。若 ERP 支持客户端业务键/幂等能力再接入；本次 OpenAPI 新增销售单 DTO 未声明幂等字段，不能假定后端已经提供。不要仅靠日期/客户模糊查单就自动判定是同一笔。

### 4. JWT 与环境配置没有失败即拒绝（高）

位置：app.py 的 _decode_jwt_verified/_context_from_jwt；gjp_common/config.py 的 current_env_name。

- JWT 验证会检查存在的 exp，但没有 require exp；用合法签名、没有 exp 的合成 JWT 已验证可通过。
- tenantId/loginId 缺失时落到 unknown，可能让不同身份共用缓存键；应拒绝缺失或不合法的关键 claim。
- GJP_ENV 拼写错误回退 local，从而选择不验签的解析器。应对非法环境值报配置错误；显式 local 仍可服务本地测试。
- billing:read/write 当前是固定赋值，不能当作已实现 ERP 用户细粒度授权。发行者/受众是否需要校验应按 ERP 真实令牌约定决定，不能凭空增加不兼容要求。

### 5. 预览版本、数量和价格约束不一致（中高）

位置：session.py 的 store_prepared_sales_order/parse_order_text；toolset.py 的 _build_sales_order_preview/_build_modify_items。

新预览最多保留 20 份，却没有将同一订单的旧预览废弃。用户修改后，旧 preview_id 仍可提交，与提示词的“旧预览失效”不一致。最简单的产品约定是一个对话只保留当前待提交预览，并设置合理确认有效期；无需完整工作流状态机。

ERP SalesOrderItemDTO 要求 quantity >= 0.0001、unitPrice >= 0；新增预览允许 0，修改仅检查 >0，未完全对齐。优惠/收款金额在 API 中也要求 >=0，当前修改入口没有检查。应统一执行有限数、范围校验，再做 Decimal 金额计算，避免超大数量导致 InvalidOperation。

解析已知反例：书本2本 土豆3斤、自定义单位在前时不能正确拆行；火龙果猕猴桃各5斤被双字硬切；土豆-2斤把负号并入名称。不要继续堆自然语言猜测规则：平台先标准化为“一行一个商品+明确数量单位”，MCP 对歧义和非法数拒绝并请求澄清。当前提示词允许用户“继续/跳过”时数量默认 1，但 parser 对缺省数量也直接用 1，不能证明用户做过该选择。

### 6. 商品缓存与基础资料消歧有小而实际的缺口（中）

TenantCatalogState.ensure 首次未加载时调用 refresh，锁内没有二次判断是否已加载；两个并发冷启动会串行拉两次目录。已用合成 loader 验证调用数为 2。过期刷新在入队时也未去重，失败冷却没有在等待锁后复核；关停未清理这些任务。增加一个在途刷新任务或锁内复核足够。

searchProducts 直接绑定 session.search_products，不调用 ensure_catalog，也没有独立 scope 检查。第一次查询要先 sync，后续纯搜索不会触发过期刷新；listProducts/preview 却会。将其包装为 ToolSet 的只读异步工具，统一 read 权限与 ensure 即可。

基础资料 _reference_option 仅保留 id/name/default，随后对外又去掉 ID；_resolve_reference 却仍试图按 code 匹配。按第一个横线或括号前缀去重会把不同门店/仓库合并候选，并且只查首批 10 条不能证明全局唯一。保留编号和机器可用 ID、按真实 ID 去重；用户界面可以隐藏 ID，不必从工具结果删除。大分页消歧按需获取，勿默认拉全量客户。

租户共享商品目录的前提是同租户用户有相同商品/价格可见性；若 ERP 按人员或仓库过滤，缓存键必须包含实际权限维度。这一点需对接方确认，当前代码无法证明已发生越权，不应直接下结论。

## 与当前 ERP API 契约的对照

| 能力 | 当前映射 | 核对结果 |
|---|---|---|
| 商品分页 | GET /product/page | 存在，但 OpenAPI 标记 deprecated；同文档提供 GET /product。验证响应等价后迁移即可 |
| 客户/仓库/职员 | GET /customer/page、/warehouse/page、/staff/page | 路径及分页、keyword/status 参数基本对应 |
| 新增销售单 | POST /sales/orders | id=0、orderDate、customerId、warehouseId、handlerId、saveType、items 与 DTO 字段对应；id=0 的实际后端语义须由联调确认 |
| 查询列表与详情 | GET /sales/orders/page、GET /sales/orders/{id} | 路径与主要筛选字段对应；先将业务单号解析为内部 ID 的设计合理，但首批 20 条模糊结果可能漏掉精确项 |
| 作废 | PUT /sales/orders/{id}/void | 对应；作废是业务能力，当前无需顺手扩展 DELETE |
| 修改 | PUT /sales/orders/{id} | 主要字段对应；UpdateDTO 的 saveType 描述仅为 0 保持、2 过账，工具却复用新增枚举并允许 pre_receipt=1。是契约差异，尚未证明后端一定拒绝 |
| 数量/金额 | SalesOrderItemDTO 与 Create/UpdateDTO | 当前校验不完整，见上文 |
| 多单位、收款、商城关联 | API 中存在额外字段/接口 | 当前未覆盖属于产品范围，不是必然缺陷；有明确需求再扩展，单位不一致继续要求确认 |

OpenAPI 中产品/客户 status 为 string，当前 query 的 1 经 HTTP 编码为字符串，不能仅因 Python 传整数就判定不兼容。新增返回 ResultString，Adapter 回查单据号是合理做法；失败降级 ID 时应给出清晰标识，避免把内部 ID 宣称为业务单号。

## AgentScope 2.0.5 适配判断

本地安装版本确为 2.0.5。FunctionTool 支持项目使用的 is_read_only、is_concurrency_safe 参数，ToolBase/ToolChunk、权限对象的使用不存在明显版本错配。保留当前封装比更换框架更合适。

MCP 原始调用返回 dict，再由 SDK 生成 structuredContent 与文本兼容输出，做法合理。本地 MCP Server.call_tool 默认 validate_input=True，并验证 outputSchema，因此不能把 inspect.signature.bind 只校验参数名误判为“HTTP MCP 完全没有类型校验”。领域数值约束仍须自己实现，尤其 order_text 内嵌数量无法靠顶层 string schema 校验。

但 invoke_raw 明确跳过 AgentScope 的执行路径，不会自动获得 Agent 的权限、中间件和调度保证。SessionFunctionTool.check_permissions 一律 ALLOW，其“只修改本地草稿”注释已不符合真实写 ERP 的工具。用户确认可以由 AI 平台现有工具审批承接，绑定具体预览/目标；不必在 MCP 中另建审批系统。若仅用模型布尔值，应如实描述为提示词约束。

普通 dict 的 ok=false 不会自动设置 MCP isError=true；当前也会记“调用成功”。建议在 MCP 边界统一表达工具错误并区分业务失败日志，避免客户端统计误报。_invoke_tool 还有潜在的非 dict 返回时再次调用工具路径；当前 10 个工具都返回 dict，暂未触发，扩展前应改为一次执行、一次归一化。

AgentScope 本地工具保留 snake_case，MCP 发布 camelCase，而当前提示词使用 camelCase。纯 MCP 部署没有问题；如果将来直接 tools() 绑定平台 Agent，应选择对应名称的提示词，不能声称两种路径现成可互换。

## 部署取舍与建议顺序

1. 先修 API Key 身份脱敏、JWT/环境失败拒绝、同预览提交互斥和超时语义；这是正常开单可靠性的底线。
2. 再修预览有效性、数量金额约束、修改枚举差异、商品刷新与资料消歧；每类问题添加能证明边界行为的回归测试。
3. 单进程部署时为凭据和租户目录加有界回收，关闭时取消并等待刷新任务；对话 ID 超长应拒绝或摘要，避免截断碰撞。缺省对话 ID 会按账号/API Key 共用会话，平台应明确传入稳定对话 ID。
4. 只有确实要多 worker/副本时再设计预览/幂等共享。stateless MCP 的普通负载均衡会造成预览在另一进程找不到；当前先明确一个进程即可。
5. 暂不引入向量数据库、全量 API 自动发布、通用流程引擎、额外 Agent 和复杂抽象。保留 ToolSet → Port → Adapter，清理重复日期校验、过时注释即可。

## 验证结果与限制

执行 `.venv/bin/python -m pytest -q`，显式移除两个真实 E2E 环境变量：**198 passed、53 skipped、8 xfailed**。7 个 warning 来自测试 JWT 的短 HMAC 密钥，不能据此认定生产密钥长度不合规。

8 个 xfail 包含解析、零数量、超大数量、两种并发提交和空白单号工具层测试。最后一个空白单号测试使用假 Adapter；真实 Adapter 的 _resolve_order_id 已拒绝空白值，因此它属于层间校验不一致，不应误报为生产必然发送空单号请求。

额外用合成输入验证了无 exp 的签名 JWT 被接受、缺失身份退化 unknown、API Key 进入身份字段、两次并发目录冷启动重复加载。没有使用或打印真实认证凭据，没有执行真实 ERP 写操作，也未做负载、SSE 端到端或多进程运行测试；上述涉及这些环境的事项为代码路径分析与明确限制。

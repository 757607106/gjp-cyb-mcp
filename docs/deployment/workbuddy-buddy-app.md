# WorkBuddy Buddy 应用接入

本接入只新增 WorkBuddy 组合入口和连接器资产，不改变原有
`erp_billing.app:app`、`BillingToolSet`、商品匹配或销售单行为。

## 目录

```text
integrations/workbuddy/
├── gjp-erp-billing/        # 正式 OAuth 连接器
└── gjp-erp-billing-token/  # API Key 联调连接器，使用不同 source
```

正式连接器使用 `erp_billing.workbuddy_app:app`，由 MCP 服务提供 OAuth 2.1、
动态客户端注册、PKCE S256、刷新令牌轮换和 ERP AI Token 绑定页。测试连接器直接
把用户在 WorkBuddy 本地填写的 AI Token 注入 `X-API-Key`，只用于联调。

## 一、先验证 Token 连接器

现有测试服务仍运行 `erp_billing.app:app` 时即可验证：

1. 将 `integrations/workbuddy/gjp-erp-billing-token/` 打成 zip。
2. 在 WorkBuddy 开放平台创建连接器并上传。
3. 连接时填写测试 ERP AI Token。
4. 验证 `listProducts`、`searchProducts`、`searchBillingReferences` 和
   `previewSalesOrder`。
5. 写操作只使用专门测试数据，并在测试结束后作废测试单。

测试连接器 source 是 `gjp-erp-billing-token`，不得与正式 OAuth 连接器混用。

## 二、启动正式 OAuth 入口

生成加密密钥：

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

设置环境变量：

```bash
export GJP_ENV=production
export ERP_BILLING_BASE_URL=https://new.yuncyb.com/aicyberp-api
export WORKBUDDY_PUBLIC_BASE_URL=https://test-mcp-server.yuncyb.com
export WORKBUDDY_OAUTH_DB_PATH=/var/lib/erp-billing/workbuddy-oauth.db
export WORKBUDDY_OAUTH_ENCRYPTION_KEY=<Fernet密钥>
export WORKBUDDY_CONNECTOR_SOURCE=gjp-erp-billing
```

数据库目录只授予服务账号读写权限。启动独立入口：

```bash
uv run uvicorn erp_billing.workbuddy_app:app --host 0.0.0.0 --port 8102
```

公开端点：

| 路径 | 用途 |
|---|---|
| `POST /mcp` | OAuth 保护的 Streamable HTTP MCP |
| `GET /.well-known/oauth-protected-resource/mcp` | MCP 资源发现 |
| `GET /.well-known/oauth-authorization-server` | OAuth 服务发现 |
| `POST /oauth/register` | WorkBuddy 公共客户端动态注册 |
| `GET /oauth/authorize` | PKCE 授权入口 |
| `POST /oauth/token` | code 换票与 refresh token 轮换 |
| `POST /oauth/revoke` | 撤销 MCP Token |
| `GET/POST /oauth/bind` | ERP AI Token 绑定页 |

绑定页只接受 ERP AI Token，不接受账号密码。Token 加密存储，不进入 MCP 工具参数、
模型上下文或普通日志。WorkBuddy 获取的是独立的短期 MCP access token；该 Token
不会透传给 ERP。

当前仓库部署模型是单实例，因此默认使用加密 SQLite。升级为多副本前，需把 OAuth
状态、预览和幂等结果迁移到共享存储；不得把 SQLite 放到多个副本同时写入的网络盘。

## 三、WorkBuddy 开放平台配置

1. 创建 Buddy 应用，名称建议“管家婆智能开单”。
2. 首页配置三个工作模式：销售开单、销售单查询、单据修改与作废。
3. 场景胶囊建议：文字开单、拍照开单、查询今日销售单、修改销售单、作废销售单。
4. 上传 `integrations/workbuddy/gjp-erp-billing/` 连接器并设为内置连接器。
5. 将 `skills/erp-billing/SKILL.md` 绑定到销售开单相关模式。
6. 工作模式 System Prompt 使用 `ERP_BILLING_SYSTEM_PROMPT`；MCP 初始化仍保留
   `ERP_BILLING_MCP_INSTRUCTIONS` 作为服务端安全兜底。
7. 拍照开单模式选择支持图片理解的模型；图片由模型识别后组装 `order_text`，MCP
   仍只接收结构化文本参数。
8. 完成预览调试后再提交连接器和 Buddy 应用审核。

## 四、验收

- 未带 Token 请求 `/mcp` 返回 HTTP 401，且 `WWW-Authenticate` 包含
  `resource_metadata`。
- 动态注册只接受 `token_endpoint_auth_method=none` 的公共客户端。
- 只允许 `workbuddy://.../connector%3Agjp-erp-billing/...` 或
  `http://127.0.0.1:{动态端口}/oauth/callback` 回调。
- 授权码一次性使用，PKCE 校验失败时不签发 Token。
- access token 只能访问当前 `/mcp` resource；refresh token 每次使用后轮换。
- `billing:read` 与 `billing:write` 继续由 ToolSet 做最终权限检查。
- 用户未明确确认时，创建、修改和作废工具拒绝执行。
- 日志、错误、工具 Schema 和连接器文件均不出现真实 ERP Token。

# ERP 开单 MCP 部署契约

本文记录部署必须遵守的运行时边界。服务器操作见
[部署手册](server-deploy-runbook.md)，日常排障见 [运维手册](server-service-ops.md)，
WorkBuddy 见 [专用部署文档](workbuddy-buddy-app.md)。

## 制品

生产 wheel 只包含 `src/erp_billing` 与 `src/gjp_common`：

```bash
uv build --wheel
uv pip install dist/gjp_erp_billing_mcp-*.whl
```

生产制品必须从 `main` 或发布 tag 构建。`tests/`、`docs/`、`AGENTS.md`、本地状态与
测试配置不进入 wheel；`config/production.env` 由部署清单单独投放。

## 入口与配置

| 模式 | ASGI 入口 | 鉴权 | 默认端口 |
|---|---|---|---|
| legacy | `erp_billing.app:app` | Bearer JWT / `X-API-Key` | `8102` |
| WorkBuddy | `erp_billing.workbuddy_app:app` | OAuth 2.1，服务端绑定 ERP Token | `8103` |

共同配置：

- `GJP_ENV=production`：加载生产配置并启用生产约束；
- `ERP_BILLING_BASE_URL`：部署级固定 HTTPS 地址；
- `ERP_BILLING_TIMEOUT_SECONDS`：ERP 请求超时，默认 30 秒；
- `ERP_BILLING_CATALOG_TTL_SECONDS`：租户商品目录 TTL，默认 600 秒。

legacy Bearer/API Key 都由可信接入方逐请求传入，服务端不需要部署级 JWT 签名密钥；
原凭据由固定地址的 ERP API 做最终鉴权。WorkBuddy 还必须设置公网根地址、OAuth
数据库、Fernet 密钥和 connector source，详见专用文档。所有系统环境变量优先于
`config/production.env`。

## 身份与凭据边界

- `InvocationContext` 只保存租户、主体、账号、会话和 scope，不保存秘密；
- Bearer、API Key 和 WorkBuddy 绑定的 ERP Token 只存在于服务端凭据提供者；
- Tool Schema、参数、响应、模型上下文和普通日志不得出现凭据；
- ERP URL 只来自部署配置，工具参数和请求头不能覆盖；
- ERP 401/403 映射为重新授权，不让模型向用户索取账号密码。

legacy 按 `(tenant_id, account_id, session_id)` 隔离 ToolSet、预览与幂等结果；商品目录
按租户共享。legacy 从 JWT payload 取得的身份字段不等于本地验签，部署必须保证请求
来自已鉴权的可信平台；WorkBuddy 先验证 MCP access token，再按绑定主体解析 ERP
凭据，MCP token 不透传 ERP。

## 生命周期与扩展

内置应用在 ASGI shutdown 阶段关闭共享 HTTP 连接池。自定义 Resolver 持有数据库或
客户端时，也必须通过 `create_billing_mcp_service(..., shutdown=...)` 释放资源。

SQLite OAuth 状态和进程内会话只适合单实例。多 worker 或多副本部署前，必须将 OAuth
状态、预览和幂等数据迁移到共享存储；不要让多个实例并发写同一个网络盘 SQLite。

## 发布检查

- `uv run ruff check src tests`；
- `uv run pytest -q`；
- `GJP_ENV=production` 且敏感值不在仓库；
- Nginx 只代理到回环地址上的正确端口；
- legacy 入口已限制为可信 AI 平台可访问；
- 未授权 MCP 请求被拒绝，健康检查和 OAuth 元数据符合预期；
- 写工具必须有 `billing:write`、有效预览、明确确认和幂等键。

工具参数和销售单业务契约属于架构文档，不在部署文档重复维护，参见
[工具与 API 匹配](../architecture/ai-billing-tools-api-matching.md) 和
[业务数据流](../architecture/business-data-flow.md)。

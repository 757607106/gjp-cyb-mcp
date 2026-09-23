# ERP 开单能力安全约定

本文只记录接入方必须遵守的安全与隔离约定。安装、启动、Nginx 和运维命令不在此重复，
见 [部署契约](yuncyb-mcp-service-deployment.md)。

## 固定业务端点

`YUNCYB_BASE_URL` 在进程启动时校验并固定，必须为不含用户信息、query 或 fragment
的 HTTPS 地址。Adapter 只访问源码定义的相对路径，租户会话、MCP Header 和 Tool
参数均不能改变目标地址。

## 鉴权

| 入口 | MCP 凭据 | ERP 凭据 |
|---|---|---|
| 生产直连 Bearer | `Authorization: Bearer <ERP token>` | 同一 ERP Token |
| 生产直连 API Key | `X-API-Key: <key>` | 同一 API Key |
| WorkBuddy OAuth | WorkBuddy MCP access token | 服务端加密保存的 ERP AI Token |

生产三方可选择 WorkBuddy OAuth 或直连 ERP 长期凭据。OAuth MCP token 与 ERP Token
必须分离；直连 Bearer/API Key 逐请求动态接收，并由 ERP API 最终鉴权。服务端从
Bearer payload 读取 `tenantId`、`loginId` 只用于会话隔离，不把它当作独立验签。
任何入口都不得把凭据加入工具参数或模型消息。

两个入口的 `/mcp` 都在认证中间件外层执行固定窗口限流，默认同一凭据每 60 秒最多
120 个请求，超过后返回 HTTP 429 和 `Retry-After`。部署可通过
`GJP_MCP_RATE_LIMIT_REQUESTS`、`GJP_MCP_RATE_LIMIT_WINDOW_SECONDS` 调整正数阈值。

## 会话与权限

- 直连客户端建议传 `X-Conversation-Id`，避免同账号多对话共享预览；
- ToolSet、预览和幂等结果按会话隔离，商品目录按租户共享；
- 查询与预览要求 `yuncyb:read`；创建、修改和作废要求 `yuncyb:write`；
- 写操作还必须关联当前有效预览、用户明确确认和唯一幂等键；
- 多实例必须使用共享 OAuth、会话和幂等存储。

## 媒体边界

MCP 只接收文本和结构化参数，不处理附件、ASR 或 OCR。语音由客户端转文字；支持视觉
理解的模型可直接从图片提取内容并组装 `order_text`，但图片与识别凭据不进入 MCP。

## 日志

默认日志必须脱敏，不输出 Authorization、API Key、Cookie、ERP Token、OAuth code 或
refresh token。完整凭据转储只允许在隔离测试环境短时开启，结束后立即关闭并清理日志。

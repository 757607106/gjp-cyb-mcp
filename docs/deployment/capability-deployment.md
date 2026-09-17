# ERP 开单能力安全约定

本文只记录接入方必须遵守的安全与隔离约定。安装、启动、Nginx 和运维命令不在此重复，
见 [部署契约](billing-mcp-service-deployment.md)。

## 固定业务端点

`ERP_BILLING_BASE_URL` 在进程启动时校验并固定，必须为不含用户信息、query 或 fragment
的 HTTPS 地址。Adapter 只访问源码定义的相对路径，租户会话、MCP Header 和 Tool
参数均不能改变目标地址。

## 鉴权

| 入口 | MCP 凭据 | ERP 凭据 |
|---|---|---|
| legacy Bearer | `Authorization: Bearer <JWT>` | 同一 JWT |
| legacy API Key | `X-API-Key: <key>` | 同一 API Key |
| WorkBuddy OAuth | WorkBuddy MCP access token | 服务端加密保存的 ERP AI Token |

legacy Bearer 由可信 AI 平台完成前置鉴权，MCP 只解析 `tenantId`、`loginId` 做会话
隔离并把原 Token 交给 ERP API 最终鉴权；因此 legacy 入口必须通过私网、网关访问
控制或来源白名单限制，不能作为无访问控制的公网认证端点。WorkBuddy MCP token 与
ERP Token 必须分离，任何入口都不得把凭据加入工具参数或模型消息。

## 会话与权限

- legacy 客户端建议传 `X-Conversation-Id`，避免同账号多对话共享预览；
- ToolSet、预览和幂等结果按会话隔离，商品目录按租户共享；
- 查询与预览要求 `billing:read`；创建、修改和作废要求 `billing:write`；
- 写操作还必须关联当前有效预览、用户明确确认和唯一幂等键；
- 多实例必须使用共享 OAuth、会话和幂等存储。

## 媒体边界

MCP 只接收文本和结构化参数，不处理附件、ASR 或 OCR。语音由客户端转文字；支持视觉
理解的模型可直接从图片提取内容并组装 `order_text`，但图片与识别凭据不进入 MCP。

## 日志

默认日志必须脱敏，不输出 Authorization、API Key、Cookie、ERP Token、OAuth code 或
refresh token。完整凭据转储只允许在隔离测试环境短时开启，结束后立即关闭并清理日志。

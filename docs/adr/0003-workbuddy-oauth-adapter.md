# ADR 0003：WorkBuddy OAuth 适配边界

- 状态：已采纳
- 日期：2026-09-17
- 扩展：ADR 0002 的对接方鉴权边界

## 背景

WorkBuddy 的正式内置连接器使用 OAuth 2.1 + PKCE 连接远程 MCP，
而现有 `yuncyb.app:app` 由对接方直接携带 ERP JWT 或 AI Token。
两种对接方式需要隔离，不应为 WorkBuddy 改动已有工具或运行入口。

## 决策

1. 新增独立组合入口 `yuncyb.workbuddy_app:app`；原有
   `yuncyb.app:app`、`YunCybToolSet` 和 ERP Adapter 保持不变。
2. WorkBuddy 通过 OAuth 2.1 公共客户端、动态客户端注册和 PKCE S256
   获取短期 MCP access token；回调地址只允许 WorkBuddy 私有协议或
   `127.0.0.1` 动态端口。
3. 用户在服务端绑定页输入 ERP AI Token。ERP 凭据与 MCP Token 分开存储，
   不进入工具 Schema、模型上下文或普通日志，也不向 ERP 透传 MCP Token。
4. 单实例部署默认使用 Fernet 加密的 SQLite 状态存储；多副本部署前
   必须在组合根替换为共享存储。
5. API Key 联调连接器使用独立 `source=yuncyb-token`，
   不与正式 OAuth 连接器混用。

## 结果

- WorkBuddy 获得完整的发现、注册、授权、换票、刷新与撤销链路。
- 原有 MCP 客户端的 JWT / AI Token 调用方式不受影响。
- 工具继续使用 `yuncyb:read` / `yuncyb:write`、当前预览和用户确认
  作为写操作的最终安全边界。

## 非目标

- 不在 MCP 服务中构建模型或改变 Buddy 的对话 UI。
- 不新增账号密码登录、动态 ERP URL 或新的 ERP 业务接口。
- 不在本次改造中引入多租户共享数据库或分布式 Token 服务。

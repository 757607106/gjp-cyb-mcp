# ADR 0004：legacy Bearer 透传与可信接入边界

- 状态：已采纳
- 日期：2026-09-17
- 修正：ADR 0002 中 legacy Bearer 的验证责任

## 背景

legacy `yuncyb.app:app` 的既有对接契约是 AI 平台逐请求携带当前 ERP JWT，MCP
从 payload 取得 `tenantId`、`loginId` 建立隔离上下文，并把原 Token 交给固定地址的
ERP API。后续版本在 MCP 内新增 HS256 本地验签，要求部署
`YUNCYB_JWT_SECRET` 和标准 `exp` 字段。

真实 ERP Token 使用 `eff` 字段，ERP 也不向 MCP 分发 HS256 签名密钥，导致生产
legacy 请求在到达 ERP 前即被拒绝。WorkBuddy 使用独立 OAuth 入口和凭据绑定，不受
该问题影响。

## 决策

1. legacy MCP 恢复逐请求直接接收 ERP Bearer；不保存或要求部署级 JWT 签名密钥。
2. MCP 校验 JWT 可解析且包含有效 `tenantId`、`loginId`，用于会话与租户隔离；这一步
   不是签名认证。
3. 原 Bearer 仅保存在服务端凭据存储中，并原样注入固定地址的 ERP API，由 ERP 做
   最终签名、有效期和权限校验。
4. 直连 Bearer 与 `X-API-Key` 都是三方逐请求动态提供的 ERP 长期业务凭据，由固定
   ERP API 最终鉴权；它们不是 WorkBuddy MCP OAuth access token。
5. 生产三方平台可选择直连 ERP 长期凭据或 WorkBuddy OAuth 入口。

## 结果

- 现有 ERP `eff` Token 无需额外 Secret 即可恢复调用。
- MCP 不获得可用于签发 ERP Token 的对称密钥，减少密钥分发范围。
- 部署侧承担明确的可信入口约束；若未来需要把 legacy 直接暴露给不可信网络，应改用
  ERP Token introspection、非对称签名/JWKS 或独立 MCP OAuth，而不能继续依赖未验签
  payload。

## 非目标

- 不改变 ERP API 地址、工具 Schema、业务权限或写操作确认规则。
- 不改变 WorkBuddy OAuth、Token 绑定或加密存储。
- 不在 MCP 中新增 ERP 登录、账号密码或动态 Token 登记接口。

# 本地 ngrok 接入开单 MCP

## 启动 Token-only 验证服务

```zsh
cd /Users/pusonglin/gjp-cyb-mcp
export ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
uv run uvicorn gjp_cli.billing_validation:app --host 0.0.0.0 --port 8102
```

ERP URL 固定在服务端环境中。验证服务只提供 `/test-auth/token`、`/mcp` 和 `/sse`，
没有账号密码或验证码登录入口。

## 暴露 MCP

```zsh
cd /Users/pusonglin/gjp-cyb-mcp
ngrok http 8102
```

第三方平台连接：

```text
https://<ngrok-domain>/mcp
```

## 获取 CLI 验证 Bearer

推荐让本地 CLI 自动登记已有 ERP Token。启动后按提示粘贴，输入不回显，可
直接粘贴 `Bearer ...` 或裸 JWT，回车提交：

```zsh
cd /Users/pusonglin/gjp-cyb-mcp
uv run python -m gjp_cli mcp-chat \
  --url https://<ngrok-domain>/mcp
```

如需脚本自动化，可用 `--upstream-token` 或 `ERP_BILLING_UPSTREAM_TOKEN`
环境变量。也可用下面的完整命令隐藏输入 Token 并手工调用 `/test-auth/token`：

```zsh
cd /Users/pusonglin/gjp-cyb-mcp
read -r -s "ERP_BILLING_UPSTREAM_TOKEN?请粘贴 ERP Token（支持 Bearer 前缀）："
printf '\n'
export ERP_BILLING_UPSTREAM_TOKEN
curl -sS -X POST https://<ngrok-domain>/test-auth/token \
  -H 'Content-Type: application/json' \
  -d "{\"upstreamToken\":\"${ERP_BILLING_UPSTREAM_TOKEN}\"}"
unset ERP_BILLING_UPSTREAM_TOKEN
```

返回的 `accessToken` 是独立临时 MCP Bearer，不是上游 ERP Token。Token 请求不
接受动态 URL；传入 `baseUrl` 或 `appBaseUrl` 会被拒绝。

常见问题：

- 401：MCP Bearer 无效或过期，重新调用 `/test-auth/token`。
- 连接失败：确认 uvicorn 与 ngrok 仍在运行。
- ngrok 域名变化：更新 AI 平台的 MCP URL；ERP 固定 URL 不受影响。

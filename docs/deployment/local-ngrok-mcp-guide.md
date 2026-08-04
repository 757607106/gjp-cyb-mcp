# 本地 ngrok 接入开单 MCP

## 启动验证服务

```zsh
cd /Users/pusonglin/gjp-cyb-mcp
export ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
uv run uvicorn gjp_cli.billing_validation:app --host 0.0.0.0 --port 8102
```

ERP URL 固定在服务端环境中。验证服务提供 `/mcp` 和 `/sse`，MCP 客户端
直接用 ERP JWT 作为 Bearer Token，无需换票。

## 暴露 MCP

```zsh
cd /Users/pusonglin/gjp-cyb-mcp
ngrok http 8102
```

第三方平台连接：

```text
https://<ngrok-domain>/sse
```

Bearer Token 直接填 ERP JWT（如 `eyJ0eXAi...`）。

## 获取 CLI 验证 Bearer

推荐让本地 CLI 直接使用 ERP Token。启动后按提示粘贴，输入不回显，可
直接粘贴 `Bearer ...` 或裸 JWT，回车提交：

```zsh
cd /Users/pusonglin/gjp-cyb-mcp
uv run python -m gjp_cli mcp-chat \
  --url https://<ngrok-domain>/mcp
```

如需脚本自动化，可用 `--upstream-token` 或 `ERP_BILLING_UPSTREAM_TOKEN`
环境变量。CLI 直接把 ERP JWT 作为 MCP Bearer 使用，不再调用换票端点。

常见问题：

- 401：ERP JWT 无效或过期，重新获取 ERP Token。
- 连接失败：确认 uvicorn 与 ngrok 仍在运行。
- ngrok 域名变化：更新 AI 平台的 MCP URL；ERP 固定 URL 不受影响。

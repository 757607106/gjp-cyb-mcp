# 开单 MCP 服务 - 服务器运维操作手册

本文档覆盖服务器上 `erp-billing` MCP 服务的三项日常运维操作：停止服务、临时
DEBUG 调试、生产模式启用。所有命令在部署目录 `/root/gjp-cyb-mcp` 下执行。

## 服务信息

| 项目 | 值 |
|---|---|
| 服务名 | `erp-billing` |
| 端口 | `8102` |
| 启动入口 | `erp_billing.app:app` |
| 日志文件 | `/var/log/erp-billing-mcp.log` |
| 部署目录 | `/root/gjp-cyb-mcp` |
| systemd 服务名 | `erp-billing-mcp` |

---

## 一、停止当前服务

### 1.1 停止进程

```bash
pkill -f "uvicorn erp_billing.app"
sleep 1 && ss -ltnp | grep 8102 || echo "端口 8102 已释放，服务已停止"
```

### 1.2 如有残留进程（端口未释放）

```bash
kill -9 $(pgrep -f "uvicorn erp_billing.app")
```

### 1.3 停止 systemd 管理的服务（若使用方式 B 部署）

```bash
systemctl stop erp-billing-mcp
systemctl status erp-billing-mcp
```

---

## 二、临时 DEBUG 调试

> 临时开启 DEBUG 日志级别，用于排查 ERP 接口调用、商品匹配等问题。
> 调试完毕后重新启动切回 INFO 即可，不影响业务逻辑。

### 2.1 停止当前服务 + DEBUG 模式重启

```bash
pkill -f "uvicorn erp_billing.app"
cd /root/gjp-cyb-mcp
export ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
export GJP_LOG_LEVEL=DEBUG
nohup uv run uvicorn erp_billing.app:app --host 0.0.0.0 --port 8102 \
  >> /var/log/erp-billing-mcp.log 2>&1 &
sleep 2 && tail -n 10 /var/log/erp-billing-mcp.log
```

### 2.2 实时跟踪日志

```bash
tail -f /var/log/erp-billing-mcp.log
```

看完按 `Ctrl + C` 退出，服务继续在后台运行。

### 2.3 过滤关键信息

```bash
# 只看错误
grep -i "error\|exception\|500" /var/log/erp-billing-mcp.log | tail -n 30

# 只看 ERP 请求
grep "ERP 请求" /var/log/erp-billing-mcp.log | tail -n 30

# 只看工具调用
grep "MCP 调用" /var/log/erp-billing-mcp.log | tail -n 30
```

### 2.4 DEBUG 完毕切回 INFO

```bash
pkill -f "uvicorn erp_billing.app"
cd /root/gjp-cyb-mcp
export ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
nohup uv run uvicorn erp_billing.app:app --host 0.0.0.0 --port 8102 \
  >> /var/log/erp-billing-mcp.log 2>&1 &
```

---

## 三、生产模式启用

> 生产模式强制 HS256 验签，必须注入 `ERP_BILLING_JWT_SECRET`，
> 缺失会拒绝启动。

### 3.1 方式 A：nohup 临时常驻

```bash
pkill -f "uvicorn erp_billing.app"
cd /root/gjp-cyb-mcp
export GJP_ENV=production
export ERP_BILLING_BASE_URL=https://new.yuncyb.com/aicyberp-api
export ERP_BILLING_JWT_SECRET=<HS256 验签密钥>
export ERP_BILLING_TIMEOUT_SECONDS=30
nohup uv run uvicorn erp_billing.app:app --host 0.0.0.0 --port 8102 \
  >> /var/log/erp-billing-mcp.log 2>&1 &
sleep 2 && tail -n 10 /var/log/erp-billing-mcp.log && ss -ltnp | grep 8102
```

### 3.2 方式 B：systemd 服务化（生产推荐）

创建 `/etc/systemd/system/erp-billing-mcp.service`：

```ini
[Unit]
Description=ERP Billing MCP Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/gjp-cyb-mcp
Environment=GJP_ENV=production
Environment=ERP_BILLING_BASE_URL=https://new.yuncyb.com/aicyberp-api
Environment=ERP_BILLING_JWT_SECRET=<HS256 验签密钥>
Environment=ERP_BILLING_TIMEOUT_SECONDS=30
ExecStart=/usr/local/bin/uv run uvicorn erp_billing.app:app --host 0.0.0.0 --port 8102
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用并启动：

```bash
systemctl daemon-reload
systemctl enable --now erp-billing-mcp
systemctl status erp-billing-mcp
```

### 3.3 生产日志查看

```bash
# systemd 方式
journalctl -u erp-billing-mcp -f

# nohup 方式
tail -f /var/log/erp-billing-mcp.log
```

### 3.4 验证生产服务

```bash
# 端口监听
ss -ltnp | grep 8102

# 本地直连（预期 406，证明服务活着）
curl -i -X POST http://127.0.0.1:8102/mcp \
  -H "Content-Type: application/json" -d '{}'

# HTTPS 握手验证
curl -i -X POST https://test-mcp-server.yuncyb.com/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

---

## 环境变量速查

| 变量 | 说明 | 测试环境 | 生产环境 |
|---|---|---|---|
| `GJP_ENV` | 运行环境 | 不设（默认 local） | `production` |
| `ERP_BILLING_BASE_URL` | ERP 接口基地址 | `https://test-ai.yuncyb.com/aicyberp-api` | `https://new.yuncyb.com/aicyberp-api` |
| `ERP_BILLING_JWT_SECRET` | JWT 验签密钥 | 不需要（不验签） | **必填** |
| `ERP_BILLING_TIMEOUT_SECONDS` | ERP 超时秒数 | 30 | 30 |
| `GJP_LOG_LEVEL` | 日志级别 | `DEBUG` / `INFO` | `INFO` |
| `GJP_LOG_ENABLED` | 日志开关 | `true` | `true` |

> 安全提示：`ERP_BILLING_JWT_SECRET` 仅通过系统环境变量注入，不可写入
> `config/production.env` 文件或代码仓库。

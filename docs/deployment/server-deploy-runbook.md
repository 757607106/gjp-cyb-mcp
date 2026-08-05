# ERP 销售开单 MCP 服务 - 服务器部署手册

本文档记录在 Linux 服务器上从零部署 `erp-billing` MCP 服务的完整步骤，
包含代码拉取、运行时安装、wheel 构建、服务启动、Nginx 反代与 HTTPS 配置。
适用于运维人员对照执行。

## 部署信息概览

| 项目 | 值 |
|---|---|
| 代码仓库 | `https://github.com/757607106/gjp-cyb-mcp.git` |
| 生产分支 | `main` |
| 最新发布 tag | `v0.2.2` |
| MCP 服务名 | `erp-billing` |
| 服务端口 | `8102` |
| 对外域名 | `test-mcp-server.yuncyb.com` |
| MCP 端点 | `POST /mcp`（Streamable HTTP）、`GET /sse`（SSE 兼容） |
| 启动入口 | `erp_billing.app:app` |
| 部署目录 | `/root/cyb-mcp-server` |

## 前置条件

- Linux 服务器（本例为 CentOS 8 / Aliyun ECS），具备 root 权限。
- Nginx 已安装（本例 `nginx/1.28.2`）。
- `*.yuncyb.com` 通配符 SSL 证书可用（本例位于
  `/usr/local/vango/certificate/yuncyb.com.pem` 与 `yuncyb.com.key`）。
- 域名 `test-mcp-server.yuncyb.com` 的 DNS A 记录已指向本服务器公网 IP。

> 注意：系统自带 Python 通常版本较低（本例为 3.6.8），不满足项目
> `Python >= 3.11` 的要求。本手册通过 uv 管理独立 Python，不污染系统 Python。

---

## 步骤 1：拉取代码

生产部署必须基于 `main` 分支或 release tag，禁止用 `test`/`feature/*`
分支的产物上生产。

### 方式 A：git clone 指定 tag（推荐）

```bash
cd /root
git clone --branch v0.2.2 --depth 1 https://github.com/757607106/gjp-cyb-mcp.git
```

### 方式 B：下载 zip 解压（无 git 环境时）

从 GitHub 下载 `gjp-cyb-mcp-main.zip` 上传至服务器，解压到部署目录：

```bash
mkdir -p /root/cyb-mcp-server
unzip gjp-cyb-mcp-main.zip -d /root/cyb-mcp-server
# 解压后内容在 /root/cyb-mcp-server/gjp-cyb-mcp-main/ 下
```

后续命令均在项目根目录执行：

```bash
cd /root/cyb-mcp-server/gjp-cyb-mcp-main
```

---

## 步骤 2：安装 uv 包管理器

uv 是 Rust 编写的独立二进制，不依赖系统 Python。国内服务器访问
`astral.sh` 官方脚本可能超时，改用 GitHub 加速镜像下载二进制。

```bash
cd /root

# 通过 ghfast 加速下载 uv 预编译二进制
curl -L https://ghfast.top/https://github.com/astral-sh/uv/releases/download/0.12.1/uv-x86_64-unknown-linux-gnu.tar.gz -o uv.tar.gz

# 解压并安装到 /usr/local/bin
mkdir -p /tmp/uv-extract
tar -xzf uv.tar.gz -C /tmp/uv-extract
install -m 755 /tmp/uv-extract/uv-x86_64-unknown-linux-gnu/uv  /usr/local/bin/uv
install -m 755 /tmp/uv-extract/uv-x86_64-unknown-linux-gnu/uvx /usr/local/bin/uvx

# 验证
uv --version
```

> 备选镜像（任选其一，哪个通用哪个）：
> - `https://mirror.ghproxy.com/https://github.com/...`
> - `https://github.taoky.mirr.one/astral-sh/...`
>
> 若所有镜像都不通，可在本地下载后 `scp` 上传。

---

## 步骤 3：安装独立版 Python 3.11

系统自带 Python 版本过低（如 3.6.8），用 uv 安装独立管理的 Python 3.11，
不影响系统 Python：

```bash
cd /root/cyb-mcp-server/gjp-cyb-mcp-main
uv python install 3.11
```

> 安装后若提示 `/root/.local/bin` 不在 PATH，执行：
> ```bash
> export PATH="/root/.local/bin:$PATH"
> ```
> 并写入 `~/.bashrc` 永久生效。

---

## 步骤 4：同步项目依赖

```bash
uv sync --extra dev
```

`--extra dev` 安装含开发依赖（用于构建 wheel）。uv 会自动使用上一步装的
Python 3.11 创建虚拟环境并安装依赖。

---

## 步骤 5：构建生产 wheel 制品

```bash
uv build
```

产物位于 `dist/`：

- `gjp_erp_billing_mcp-0.1.0-py3-none-any.whl`
- `gjp_erp_billing_mcp-0.1.0.tar.gz`

生产制品只含 `src/erp_billing` 与 `src/gjp_common` 两个包，测试、文档与
`config/local.env` 天然不进入制品。

---

## 步骤 6：验证 wheel 制品纯净度

```bash
unzip -l dist/gjp_erp_billing_mcp-0.1.0-py3-none-any.whl
```

预期：26 个文件，全部为 `erp_billing/*` 与 `gjp_common/*` 源码，零测试、
零文档。若出现 `tests/` 或 `docs/`，说明构建配置异常，需排查
`pyproject.toml` 的包包含规则。

---

## 步骤 7：配置生产环境变量

生产环境变量由部署平台注入，系统环境变量优先于 `config/production.env`
文件值。在启动服务的 shell 或 systemd 配置中设置：

```bash
export GJP_ENV=production
export ERP_BILLING_BASE_URL=https://new.yuncyb.com/aicyberp-api
export ERP_BILLING_JWT_SECRET=<HS256 验签密钥>
export ERP_BILLING_TIMEOUT_SECONDS=30
```

| 变量 | 说明 | 是否必填 |
|---|---|---|
| `GJP_ENV` | 设为 `production` 启用 HS256 强制验签 | 是（生产） |
| `ERP_BILLING_BASE_URL` | ERP 接口基地址 | 是 |
| `ERP_BILLING_JWT_SECRET` | JWT HS256 验签密钥，缺失拒绝启动 | 是（生产） |
| `ERP_BILLING_TIMEOUT_SECONDS` | ERP API 超时秒数，默认 30 | 否 |

> 提示：若暂无 `ERP_BILLING_JWT_SECRET`，可先不设 `GJP_ENV=production`，
> 服务会以测试模式启动（走 `DirectJwtIdentityResolver` 不验签），仅用于
> 联调验证，不可作为正式生产配置。

---

## 步骤 8：启动服务（后台常驻）

### 方式 A：nohup 临时常驻（快速验证）

```bash
nohup uv run uvicorn erp_billing.app:app --host 0.0.0.0 --port 8102 \
  > /var/log/erp-billing-mcp.log 2>&1 &
```

验证进程与端口：

```bash
sleep 3 && tail -n 20 /var/log/erp-billing-mcp.log && echo "---" && ss -ltnp | grep 8102
```

预期日志含 `Uvicorn running on http://0.0.0.0:8102`，端口 8102 在监听。

### 方式 B：systemd 服务化（生产推荐）

创建 `/etc/systemd/system/erp-billing-mcp.service`：

```ini
[Unit]
Description=ERP Billing MCP Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/cyb-mcp-server/gjp-cyb-mcp-main
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

> 生产推荐方式 B：开机自启、崩溃自动拉起、环境变量持久化。

---

## 步骤 9：配置 Nginx 反向代理 + HTTPS

创建 `/etc/nginx/conf.d/test-mcp-server.yuncyb.com.conf`：

```nginx
server {
    listen 80;
    server_name test-mcp-server.yuncyb.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name test-mcp-server.yuncyb.com;

    ssl_certificate /usr/local/vango/certificate/yuncyb.com.pem;
    ssl_certificate_key /usr/local/vango/certificate/yuncyb.com.key;
    ssl_session_cache shared:SSL:1m;
    ssl_session_timeout 5m;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    client_max_body_size 100m;
    client_body_timeout 86400s;
    client_header_timeout 86400s;

    access_log /var/log/nginx/test-mcp-server-access.log;
    error_log /var/log/nginx/test-mcp-server-error.log;

    location / {
        proxy_pass http://127.0.0.1:8102;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # MCP Streamable HTTP / SSE 流式响应支持
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
}
```

测试并加载配置：

```bash
nginx -t
nginx -s reload
```

> 关键点：MCP 走 Streamable HTTP，需 `proxy_buffering off` 关闭缓冲以支持
> 流式推送，`proxy_read_timeout` 设长以支持 SSE 长连接。

---

## 步骤 10：验证 MCP 服务端点

### 10.1 本地直连验证（不带 Accept 头，预期 406）

```bash
curl -i -X POST http://127.0.0.1:8102/mcp \
  -H "Content-Type: application/json" -d '{}'
```

预期返回 `406 Not Acceptable` 与 JSON-RPC 错误，证明服务活着。

### 10.2 域名 HTTPS 验证（MCP initialize 握手）

```bash
curl -i -X POST https://test-mcp-server.yuncyb.com/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

预期返回 `200 OK`，body 含：

```json
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"experimental":{},"tools":{"listChanged":false}},"serverInfo":{"name":"erp-billing","version":"1.0.0"},"instructions":"业务身份由服务端认证，调用工具时不要传递账号、密码或访问令牌。"}}
```

返回上述结果即表示 HTTPS 域名 + Nginx 反代 + MCP 服务全链路打通。

---

## 步骤 11：MCP 客户端连接配置

服务端暴露三个端点（来自 `create_mcp_http_app`）：

| 端点 | 传输协议 | 方法 | 用途 |
|---|---|---|---|
| `/mcp` | Streamable HTTP | POST | 推荐，无状态 |
| `/sse` | SSE | GET | 兼容旧版 SSE 客户端 |
| `/messages/` | SSE 消息回传 | POST | SSE 配合使用 |

> 注意：`/mcp` 与 `/sse` 是两个独立端点，**不可拼接成 `/mcp/sse`**，
> 该路径在服务端不存在，会返回 404。

### 方案 A：Streamable HTTP（推荐）

服务端 `stateless=True, json_response=True`，无状态、更适合现代 MCP：

```json
{
  "mcpServers": {
    "yunprint-billing": {
      "type": "http",
      "url": "https://test-mcp-server.yuncyb.com/mcp",
      "headers": {
        "Authorization": "Bearer <ERP JWT>"
      }
    }
  }
}
```

### 方案 B：SSE 传输（客户端只支持 SSE 时用）

```json
{
  "mcpServers": {
    "yunprint-billing": {
      "type": "sse",
      "url": "https://test-mcp-server.yuncyb.com/sse",
      "headers": {
        "Authorization": "Bearer <ERP JWT>"
      }
    }
  }
}
```

> SSE 模式下客户端还会向 `/messages/` 回传消息，该端点服务端已挂载
> （`Mount("/messages/", ...)`），无需额外配置。

### 鉴权说明

- MCP 客户端把 ERP JWT 直接作为 Bearer Token 传入 `Authorization` 头。
- 生产模式（`GJP_ENV=production`）服务端 HS256 验签后从 payload 解析
  `tenantId`、`loginId` 构造 InvocationContext，并把同一个 JWT 注入
  ERP API 调用。
- 测试模式（未设 `GJP_ENV=production`）走 `DirectJwtIdentityResolver`，
  直接读 payload 不验签，仅用于联调。
- 业务凭据不进入工具参数、模型上下文或工具结果。

---

## 代码更新流程（git 管理后）

部署目录改为 git 克隆后（见步骤 1），后续 `main` 分支修复 bug，
更新只需两步：

```bash
# 1. 停服务 + 拉取最新代码 + 同步依赖
pkill -f "uvicorn erp_billing.app" ; cd /root/gjp-cyb-mcp && git pull && uv sync --extra dev

# 2. 重启服务（带环境变量）
export ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
nohup uv run uvicorn erp_billing.app:app --host 0.0.0.0 --port 8102 \
  > /var/log/erp-billing-mcp.log 2>&1 &
```

> 注意：每次重启需重新 `export ERP_BILLING_BASE_URL`，新进程不继承
> 旧 shell 的环境变量。若用 systemd（步骤 8 方式 B），环境变量写在
> service 文件里则无需重复 export，更新流程简化为：
> `git pull && uv sync --extra dev && systemctl restart erp-billing-mcp`。

旧 zip 解压目录（如 `/root/cyb-mcp-server`）确认不再使用后可删除
清理空间：

```bash
rm -rf /root/cyb-mcp-server
```

---

## 部署状态自检清单

| 检查项 | 命令 / 预期 |
|---|---|
| uv 版本 | `uv --version` → `uv 0.12.1` |
| Python 版本 | `uv run python --version` → `Python 3.11.x` |
| wheel 纯净 | `unzip -l dist/*.whl` → 仅 `src/` 代码 |
| 服务进程 | `ss -ltnp \| grep 8102` → 端口在监听 |
| 启动日志 | `tail /var/log/erp-billing-mcp.log` → 无异常 |
| Nginx 配置 | `nginx -t` → `test is successful` |
| HTTPS 握手 | curl initialize → `200` + `erp-billing` |
| 生产鉴权 | `GJP_ENV=production` 且已注入 `ERP_BILLING_JWT_SECRET` |

---

## 常见问题

### Q1：`uv: command not found`

服务器未装 uv。按步骤 2 安装二进制。

### Q2：curl 下载 uv 超时

国内服务器访问 `astral.sh`/`github.com` 受限，改用 `ghfast.top` 等
GitHub 加速镜像，或本地下载后 `scp` 上传。

### Q3：系统 Python 版本过低（如 3.6.8）

无需手动编译升级系统 Python。uv 会用 `uv python install 3.11` 安装
独立管理的 Python，与系统 Python 隔离。

### Q4：服务返回 `406 Not Acceptable`

MCP 协议要求客户端发送 `Accept: application/json` 头。curl 测试时需
带上该头，否则服务端拒绝。属正常协议行为，非故障。

### Q5：生产启动报"缺少 ERP_BILLING_JWT_SECRET"

生产模式（`GJP_ENV=production`）强制 HS256 验签，必须注入
`ERP_BILLING_JWT_SECRET`。缺失会拒绝构造 `VerifiedJwtIdentityResolver`。
该密钥由 ERP 平台对接方提供，仅经环境变量注入，不写入配置文件。

### Q6：nohup 重启后服务丢失

nohup 仅在进程存活期间常驻，服务器重启后不会自动恢复。生产环境改用
systemd（步骤 8 方式 B）实现开机自启与崩溃自动拉起。

### Q7：客户端报 "Invalid server type"

MCP 客户端配置缺少 `type` 字段。Streamable HTTP 填 `"type": "http"`，
SSE 填 `"type": "sse"`。仅写 `url` 与 `headers` 而不声明 `type`，客户端
无法识别服务器类型。

### Q8：客户端报 "SSE error: Non-200 status code (404)"

URL 路径拼错。常见误写为 `https://.../mcp/sse`，该路径在服务端不存在。
Streamable HTTP 用 `https://test-mcp-server.yuncyb.com/mcp`，SSE 用
`https://test-mcp-server.yuncyb.com/sse`，二者独立，不可拼接。详见
步骤 11 端点表。

### Q9：工具调用报 "未配置 ERP_BILLING_BASE_URL"

根因：服务启动时未设 `GJP_ENV`，默认走 `local` 模式，会加载
`config/local.env`；但 **main 分支已删除 `config/local.env`**（生产
分支只留 `production.env`），文件不存在导致配置未加载，
`ERP_BILLING_BASE_URL` 实际为空。虽然 `production.env` 里写了 URL，
但未设 `GJP_ENV=production` 时不会加载该文件。

配置优先级（`get_env_value`）：**系统环境变量 > 配置文件 > 默认值**。

解决（联调用，不验签）：用系统环境变量直接注入，优先级最高，
绕过配置文件，保持 local 模式（`DirectJwtIdentityResolver` 不验签）：

```bash
pkill -f "uvicorn erp_billing.app"
cd /root/cyb-mcp-server/gjp-cyb-mcp-main
export ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
nohup uv run uvicorn erp_billing.app:app --host 0.0.0.0 --port 8102 \
  > /var/log/erp-billing-mcp.log 2>&1 &
```

若要走生产模式（`GJP_ENV=production` 加载 `production.env`），
必须同时注入 `ERP_BILLING_JWT_SECRET`，否则构造
`VerifiedJwtIdentityResolver` 会拒绝启动。

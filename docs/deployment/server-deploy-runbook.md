# ERP 直连 MCP 服务器部署与更新

本文记录 `erp_billing.app:app` 的首次部署、Nginx 接入、验收和后续一键更新。
三方 MCP 客户端逐请求发送 `Authorization: Bearer <ERP token>` 或
`X-API-Key`，凭据不写入部署文件、工具参数或日志。

## 当前部署约定

| 项目 | 配置 |
|---|---|
| 公网 MCP 地址 | `https://new.yuncyb.com/mcp` |
| 健康检查 | `https://new.yuncyb.com/healthz` |
| 本机监听 | `127.0.0.1:8102` |
| systemd 服务 | `erp-billing-mcp` |
| 部署目录 | `/root/gjp-cyb-mcp` |
| 服务用户 | `erp-mcp` |
| 环境文件 | `/etc/erp-billing-mcp.env` |
| ASGI 入口 | `erp_billing.app:app` |

代码分支与运行配置是两件事：

| 一键部署参数 | Git 分支 | 用途 |
|---|---|---|
| `test` | `origin/test` | 测试、验收中的代码 |
| `production` | `origin/main` | 已验收的生产代码 |

服务器上的 `GJP_ENV=production` 表示启用服务器部署约束和
`config/production.env`，不代表当前代码一定来自 `main`。实际代码分支由一键部署参数决定。

默认脚本使用同一个目录、端口、域名和 systemd 服务，因此 `test` 与 `production`
是同一服务槽位的两种代码版本，不能同时运行。需要并行环境时，应分别配置目录、端口、
systemd 服务和域名。

## 一、首次部署

### 1. 安装运行环境

Alibaba Cloud Linux 3 保留系统 Python 3.6，项目并行使用 Python 3.11；不要替换
`/usr/bin/python3`。

```bash
yum install -y python3.11 git curl ca-certificates gcc gcc-c++ make

curl -LsSf https://astral.sh/uv/install.sh \
  | env UV_INSTALL_DIR=/usr/local/bin sh
```

配置阿里云 PyPI 加速：

```bash
cat >/etc/profile.d/gjp-mcp-uv.sh <<'EOF'
export UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
export UV_HTTP_TIMEOUT=60
EOF

chmod 0644 /etc/profile.d/gjp-mcp-uv.sh
source /etc/profile.d/gjp-mcp-uv.sh
```

### 2. 拉取代码并安装生产依赖

```bash
mkdir -p /root/gjp-cyb-mcp
git clone --branch test --single-branch \
  https://github.com/757607106/gjp-cyb-mcp.git \
  /root/gjp-cyb-mcp

cd /root/gjp-cyb-mcp
uv venv --python /usr/bin/python3.11
uv sync --frozen --no-dev
```

### 3. 创建运行配置

`ERP_BILLING_BASE_URL` 是 ERP API 地址，不是 MCP 工具参数。API Key 和 Bearer Token
由 MCP 客户端放在 HTTP Header 中动态发送，不能写入此文件。

```bash
cat >/etc/erp-billing-mcp.env <<'EOF'
GJP_ENV=production
ERP_BILLING_BASE_URL=https://new.yuncyb.com/aicyberp-api
GJP_MCP_ALLOWED_HOSTS=new.yuncyb.com
GJP_MCP_ALLOWED_ORIGINS=https://new.yuncyb.com
GJP_MCP_RATE_LIMIT_REQUESTS=120
GJP_MCP_RATE_LIMIT_WINDOW_SECONDS=60
GJP_LOG_LEVEL=INFO
EOF

chmod 0600 /etc/erp-billing-mcp.env
```

### 4. 创建最小权限服务用户

```bash
useradd --system --user-group --create-home \
  --home-dir /var/lib/erp-mcp \
  --shell /sbin/nologin \
  erp-mcp

setfacl -m u:erp-mcp:--x /root
setfacl -R -m u:erp-mcp:rX /root/gjp-cyb-mcp
```

`erp-mcp` 只能穿越 `/root` 并读取项目，不能修改代码。通过下面命令验证：

```bash
runuser -u erp-mcp -- /root/gjp-cyb-mcp/.venv/bin/python --version
```

### 5. 创建 systemd 服务

```ini
# /etc/systemd/system/erp-billing-mcp.service
[Unit]
Description=ERP Billing MCP Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=erp-mcp
Group=erp-mcp
WorkingDirectory=/root/gjp-cyb-mcp
EnvironmentFile=/etc/erp-billing-mcp.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/root/gjp-cyb-mcp/.venv/bin/uvicorn erp_billing.app:app --host 127.0.0.1 --port 8102
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
LimitNOFILE=8192

[Install]
WantedBy=multi-user.target
```

加载、启动并启用开机自启：

```bash
systemd-analyze verify /etc/systemd/system/erp-billing-mcp.service
systemctl daemon-reload
systemctl enable --now erp-billing-mcp
curl -i http://127.0.0.1:8102/healthz
```

### 6. 接入现有 Nginx

新建 `/etc/nginx/snippets/erp-billing-mcp.locations`：

```nginx
# MCP 服务：new.yuncyb.com/mcp -> 127.0.0.1:8102
# 保留 Authorization / X-API-Key，Cookie 不参与 MCP 鉴权
location = /healthz {
    proxy_pass http://127.0.0.1:8102/healthz;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Cookie "";
}

location = /mcp {
    proxy_pass http://127.0.0.1:8102;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-API-Key $http_x_api_key;
    proxy_set_header Cookie "";
    client_max_body_size 10m;
    proxy_connect_timeout 10s;
    proxy_send_timeout 60s;
    proxy_read_timeout 180s;
}
```

在 `new.yuncyb.com` 的 HTTPS `server` 块内、现有 `location /` 之前加入：

```nginx
include /etc/nginx/snippets/erp-billing-mcp.locations;
```

修改前备份原配置，修改后只在语法检查通过时平滑加载：

```bash
nginx -t
systemctl reload nginx
```

## 二、一键更新测试与生产代码

首次部署完成后，不需要重复配置 Python、systemd 或 Nginx。

部署测试代码：

```bash
cd /root/gjp-cyb-mcp
./scripts/deploy.sh test
```

部署生产代码：

```bash
cd /root/gjp-cyb-mcp
./scripts/deploy.sh production
```

一键脚本执行顺序：

| 顺序 | 操作 |
|---|---|
| 1 | 检查部署目录干净、systemd 服务存在、没有并发部署 |
| 2 | 拉取对应远端分支，只允许 fast-forward |
| 3 | 使用 `uv.lock`、`--no-dev` 和阿里云镜像同步生产依赖 |
| 4 | 导入应用，确认代码可以启动 |
| 5 | 只重启 `erp-billing-mcp` 并检查本机 `/healthz` |
| 重启或健康检查失败 | 自动切回部署前提交、恢复依赖并重启服务 |

脚本不会修改 Nginx、环境文件或其他 systemd 服务。部署目录存在未提交改动时直接退出，
不会使用 `git reset --hard` 覆盖文件。

## 三、部署验收

### 状态与健康检查

```bash
systemctl show erp-billing-mcp -p UnitFileState -p ActiveState -p SubState
curl -i https://new.yuncyb.com/healthz
```

预期状态为 `enabled`、`active`、`running`，健康检查返回 `200` 和 `{"ok":true}`。

### 无凭据请求

```bash
curl -i -X POST https://new.yuncyb.com/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

预期返回 `401 Unauthorized`。

### API Key 真实只读调用

以下命令隐藏输入，不把 API Key 写入命令历史：

```bash
read -r -s -p 'X-API-Key: ' key; printf '\n'; \
curl -sS -X POST https://new.yuncyb.com/mcp \
  -H "X-API-Key: $key" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"listProducts","arguments":{"page":1,"page_size":1}}}'; \
unset key
```

验收标准：HTTP 成功、`isError=false`、返回商品列表。Bearer Token 使用相同方式，
将请求头改为 `Authorization: Bearer $token`；两种凭据都由 ERP 最终鉴权。

## 四、日常运维

```bash
systemctl --no-pager --full status erp-billing-mcp
journalctl -u erp-billing-mcp -n 100 --no-pager
journalctl -u erp-billing-mcp -f
```

凭据不得写入仓库、环境文件、systemd 单元、Nginx 配置或普通日志。测试凭据如果进入
聊天记录、Shell 历史或工单，测试完成后必须立即轮换。

# 已部署服务完整重命名迁移

本文用于把已部署的历史服务一次性迁移为 `yuncyb`。这是一次有短暂停机的发布，公网
域名和端口不变。不要直接删除旧目录、旧 unit、旧环境文件或旧 OAuth 数据库。

## 迁移影响

- Python 包、ASGI 入口、发行包、systemd、环境变量、目录和 connector source 均变化；
- WorkBuddy `source` 和 OAuth scope 变化，现有连接需要重新安装或授权；
- 公网 `/mcp`、`/healthz`、8102 和 8103 不变，Nginx 通常无需修改；
- `searchBillingReferences` 更名为 `searchBusinessReferences`，缓存工具清单的客户端需
  重新连接。

## 迁移前准备

1. 确认新版代码已经通过全量测试，并准备一个明确的发布 commit 或 tag。
2. 通知 WorkBuddy 用户迁移后需要重新连接。
3. 记录旧服务状态，并为环境文件、unit 和 OAuth 数据库创建带日期的只读备份。
4. 保留旧代码目录，使用 `/root/yuncyb-mcp` 作为新 checkout，确保回滚不依赖重新下载。

OAuth SQLite 必须在旧 WorkBuddy 服务停止后复制，并继续使用原 Fernet 密钥。不要同时
让新旧进程写同一数据库文件。

## 切换顺序

1. 将目标分支或 tag 克隆到 `/root/yuncyb-mcp`，执行 `uv sync --frozen --no-dev`。
2. 新建 `/etc/yuncyb-mcp.env` 和 `/etc/yuncyb/workbuddy.env`，复制原配置值并把
   `ERP_BILLING_*` 键改为 `YUNCYB_*`，把 `WORKBUDDY_CONNECTOR_SOURCE` 改为
   `yuncyb`。
3. 停止旧 WorkBuddy 服务，复制 OAuth 数据库到 `/var/lib/yuncyb/`，保留原属主、权限
   和 `WORKBUDDY_OAUTH_ENCRYPTION_KEY`。
4. 按部署手册创建 `yuncyb-mcp.service` 与 `yuncyb-workbuddy-mcp.service`，先执行
   `systemd-analyze verify`，暂不启用开机启动。
5. 停止旧直连服务；启动两个新服务并依次检查本机 8102、8103 的 `/healthz`、OAuth
   元数据、未授权 `/mcp` 响应及工具列表。
6. 验证生产直连 Bearer、生产直连 `X-API-Key`、WorkBuddy OAuth 三条鉴权链路，再执行
   一个只读商品或库存查询。
7. 上传 `integrations/workbuddy/yuncyb/` 新连接器，重新连接并确认读工具；写工具必须
   使用专门测试数据、明确确认并在测试后作废。
8. 验收完成后启用新 unit、禁用旧 unit。旧目录、unit、环境文件和数据库至少保留一个
   发布观察周期，再按运维流程归档。

## 回滚

若新服务启动、鉴权或工具验证失败：

1. 停止并禁用两个新 unit；
2. 重新启动旧直连与旧 WorkBuddy unit；
3. 确认 8102、8103 和公网健康检查恢复；
4. 在 WorkBuddy 恢复旧连接器版本；
5. 保留新目录和日志用于排查，不覆盖旧数据库或旧环境文件。

回滚完成前不要删除任何旧资源，也不要让新旧 WorkBuddy 进程同时写同一 SQLite 文件。

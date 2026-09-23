# ADR 0007：云创业版技术标识完整统一为 yuncyb

- 状态：已采纳
- 日期：2026-09-23
- 扩展：ADR 0006 的对外产品命名决策

## 背景

ADR 0006 先将 MCP 对外名称改为 `yuncyb`，但继续保留了一批历史内部标识。项目已经
决定进行一次有迁移窗口的完整重命名，使代码、制品、部署和连接器使用同一产品标识。

## 决策

1. Python 包统一为 `yuncyb`，核心类型统一为 `YunCybToolSet`、`YunCybApiPort`、
   `YunCybSettings` 与 `YunCybSession`。
2. 发行包统一为 `yuncyb-mcp`，ASGI 入口统一为 `yuncyb.app:app` 与
   `yuncyb.workbuddy_app:app`。
3. systemd 服务统一为 `yuncyb-mcp`、`yuncyb-workbuddy-mcp`，部署目录、环境文件、
   日志及状态目录统一使用 `yuncyb`。
4. 服务配置变量统一使用 `YUNCYB_*`；授权 scope 统一为 `yuncyb:read` 与
   `yuncyb:write`。
5. WorkBuddy 正式连接器和测试连接器的 source 分别为 `yuncyb`、`yuncyb-token`；
   Skill 路径及名称统一为 `yuncyb`。
6. 基础资料工具更名为 `searchBusinessReferences`，其余业务工具名称保持不变。
7. 不保留旧 Python 导入、旧环境变量或旧服务名的运行时兼容分支；线上通过可回滚的
   双目录、双 unit 切换完成迁移。

## 结果

- 旧版本不能直接使用新版一键部署脚本，必须先执行重命名迁移手册。
- WorkBuddy source 与 scope 变化会使既有连接需要重新安装或授权。
- 公网 URL 和端口不变，Nginx 上游仍指向 `127.0.0.1:8102/8103`。
- 旧目录、旧 unit 和旧数据库备份在验收期内保留，可快速回滚。

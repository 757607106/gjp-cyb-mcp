#!/bin/bash
# WorkBuddy MCP 一键部署。
# 用法：
#   ./scripts/deploy-workbuddy.sh              # main
#   BRANCH=test ./scripts/deploy-workbuddy.sh  # test
#   journalctl -u erp-billing-workbuddy-mcp -f # 实时日志
#
# 测试/生产 ERP 只由 systemd EnvironmentFile 中的
# ERP_BILLING_BASE_URL 决定，部署分支不会改变业务环境。
# systemd 调试请在该 EnvironmentFile 设置 GJP_LOG_LEVEL=DEBUG 后重启服务。

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export APP_MODULE="erp_billing.workbuddy_app:app"
export GJP_ENV="${GJP_ENV:-production}"

exec "$SCRIPT_DIR/deploy.sh" "$@"

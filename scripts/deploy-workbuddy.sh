#!/bin/bash
# WorkBuddy MCP 一键部署入口。
#
# 用法：
#   ./scripts/deploy-workbuddy.sh              # 部署 main 分支
#   BRANCH=test ./scripts/deploy-workbuddy.sh  # 部署 test 分支
#
# 公网 WorkBuddy 服务始终启用生产级配置校验；实际连接测试还是生产 ERP，
# 由 systemd EnvironmentFile 中的 ERP_BILLING_BASE_URL 决定。

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export APP_MODULE="erp_billing.workbuddy_app:app"
export GJP_ENV="${GJP_ENV:-production}"

exec "$SCRIPT_DIR/deploy.sh" "$@"

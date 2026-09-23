#!/bin/bash
# WorkBuddy MCP 一键部署。
# 用法：
#   ./scripts/deploy-workbuddy.sh test
#   ./scripts/deploy-workbuddy.sh production
#
# 测试/生产 ERP 只由 systemd EnvironmentFile 中的
# YUNCYB_BASE_URL 决定，部署分支不会改变业务环境。

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export APP_MODULE="yuncyb.workbuddy_app:app"

exec "$SCRIPT_DIR/deploy.sh" "$@"

#!/bin/bash
# ERP 开单 MCP 一键部署：拉取代码、同步依赖、重启并验证服务。
#
# 用法：
#   ./scripts/deploy.sh                    # main
#   BRANCH=test ./scripts/deploy.sh        # test
#   ./scripts/deploy-workbuddy.sh          # WorkBuddy（见专用脚本）
#
# 常用覆盖项：BRANCH、DEPLOY_DIR、GJP_ENV、APP_MODULE、SERVICE_NAME、PORT。
# systemd 部署的业务配置只读取 service 指定的 EnvironmentFile。

set -euo pipefail

# 部署参数
DEPLOY_DIR="${DEPLOY_DIR:-/root/gjp-cyb-mcp}"
BRANCH="${BRANCH:-main}"
APP_MODULE="${APP_MODULE:-erp_billing.app:app}"
if [ "$APP_MODULE" = "erp_billing.workbuddy_app:app" ]; then
    LOG_FILE="${LOG_FILE:-/var/log/erp-billing-workbuddy-mcp.log}"
    PORT="${PORT:-8103}"
    SERVICE_NAME="${SERVICE_NAME:-erp-billing-workbuddy-mcp}"
else
    LOG_FILE="${LOG_FILE:-/var/log/erp-billing-mcp.log}"
    PORT="${PORT:-8102}"
    SERVICE_NAME="${SERVICE_NAME:-erp-billing-mcp}"
fi
PROCESS_PATTERN="uvicorn ${APP_MODULE}"
GJP_ENV="${GJP_ENV:-local}"
# ERP 地址默认值仅服务本地（测试）便利；生产禁止脚本注入默认域名，
# 避免 export 覆盖 config/production.env 里的真实生产地址
if [ "$GJP_ENV" = "production" ]; then
    ERP_BILLING_BASE_URL="${ERP_BILLING_BASE_URL:-}"
else
    ERP_BILLING_BASE_URL="${ERP_BILLING_BASE_URL:-https://test-ai.yuncyb.com/aicyberp-api}"
fi

# 调试参数仅用于 nohup 部署
LOG_LEVEL="INFO"
DUMP_CREDENTIALS=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --debug)
            LOG_LEVEL="DEBUG"
            shift
            ;;
        --debug-dump)
            LOG_LEVEL="DEBUG"
            DUMP_CREDENTIALS="true"
            shift
            ;;
        *)
            echo "未知参数：$1"
            echo "用法：$0 [--debug] [--debug-dump]"
            exit 1
            ;;
    esac
done

# 输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

stop_service() {
    info "1/5 停止当前服务..."
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        systemctl stop "$SERVICE_NAME"
        info "systemd 服务已停止"
    else
        pkill -f -- "$PROCESS_PATTERN" 2>/dev/null || true
        sleep 1
        if pgrep -f -- "$PROCESS_PATTERN" >/dev/null 2>&1; then
            warn "进程仍在运行，强制终止..."
            pkill -9 -f -- "$PROCESS_PATTERN" || true
            sleep 1
        fi
        info "nohup 进程已停止"
    fi
}

pull_code() {
    info "2/5 拉取最新 $BRANCH 分支代码..."
    cd "$DEPLOY_DIR"
    git fetch origin "$BRANCH:refs/remotes/origin/$BRANCH"
    git reset --hard "origin/$BRANCH"
    info "当前版本：$(git log --oneline -1)"
}

sync_deps() {
    info "3/5 同步项目依赖..."
    cd "$DEPLOY_DIR"
    uv sync --extra dev
    info "依赖同步完成"
}

start_service() {
    info "4/5 启动服务..."
    # 确保日志目录存在
    mkdir -p "$(dirname "$LOG_FILE")"

    if [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
        # systemd 方式：环境变量在 service 文件中配置
        if [ "$LOG_LEVEL" = "DEBUG" ]; then
            warn "检测到 systemd 方式，--debug 参数需通过 override 生效"
            warn "临时调试建议改用 nohup：先停用 systemd 再运行此脚本"
        fi
        systemctl daemon-reload
        systemctl start "$SERVICE_NAME"
        sleep 2
        info "systemd 服务已启动"
    else
        # nohup 方式：在此设置环境变量
        cd "$DEPLOY_DIR"
        export GJP_ENV
        # 生产模式下 ERP 地址优先级：环境变量 > config/production.env；
        # 只在非空时 export，空值 export 会覆盖 env 文件里配置的地址
        if [ -n "$ERP_BILLING_BASE_URL" ]; then
            export ERP_BILLING_BASE_URL
            info "ERP 地址来源：环境变量 $ERP_BILLING_BASE_URL"
        else
            info "ERP 地址来源：config/$GJP_ENV.env"
        fi
        export GJP_LOG_LEVEL="$LOG_LEVEL"
        [ -n "$DUMP_CREDENTIALS" ] && export GJP_DEBUG_DUMP_CREDENTIALS="$DUMP_CREDENTIALS"
        # 确保 uv 在 PATH 中
        export PATH="/usr/local/bin:$PATH"

        nohup uv run uvicorn "$APP_MODULE" \
            --host 0.0.0.0 --port "$PORT" \
            >> "$LOG_FILE" 2>&1 &
        sleep 2
        local pid
        pid=$(pgrep -f -- "$PROCESS_PATTERN" | head -1 || true)
        if [ -n "$pid" ]; then
            info "nohup 服务已启动 PID=$pid"
        else
            error "服务启动失败！最近日志："
            tail -n 20 "$LOG_FILE" 2>/dev/null
            exit 1
        fi
    fi
}

verify_service() {
    info "5/5 验证服务状态..."
    sleep 1

    # 检查进程
    if pgrep -f -- "$PROCESS_PATTERN" >/dev/null 2>&1; then
        info "进程运行中 ✓"
    else
        error "进程未运行！"
        tail -n 20 "$LOG_FILE" 2>/dev/null
        exit 1
    fi

    # 检查端口
    if ss -ltnp 2>/dev/null | grep -q ":$PORT"; then
        info "端口 $PORT 监听中 ✓"
    else
        error "端口 $PORT 未监听！"
        tail -n 20 "$LOG_FILE" 2>/dev/null
        exit 1
    fi

    # 显示最近日志
    info "最近日志："
    tail -n 5 "$LOG_FILE" 2>/dev/null || warn "日志文件为空"

    echo ""
    info "===== 部署完成 ====="
    info "分支=$BRANCH  环境=$GJP_ENV  入口=$APP_MODULE  日志级别=$LOG_LEVEL  端口=$PORT"
    if [ "$LOG_LEVEL" = "DEBUG" ]; then
        info "实时查看日志：tail -f $LOG_FILE"
    fi
    if [ "$GJP_ENV" = "production" ]; then
        local pid
        pid=$(pgrep -f -- "$PROCESS_PATTERN" | head -1)
        info "生产验证：PID=$pid 实际生效环境变量："
        cat "/proc/$pid/environ" | tr '\0' '\n' \
            | grep -E '^(GJP_ENV|ERP_BILLING_BASE_URL|WORKBUDDY_PUBLIC_BASE_URL|WORKBUDDY_OAUTH_DB_PATH|WORKBUDDY_CONNECTOR_SOURCE)=' \
            || true
    fi
}

# 部署流程
echo ""
info "===== ERP 开单 MCP 服务快速部署 ====="
info "部署目录：$DEPLOY_DIR"
info "目标分支：$BRANCH"
info "运行环境：$GJP_ENV"
info "ASGI 入口：$APP_MODULE"
info "服务名称：$SERVICE_NAME"
info "日志级别：$LOG_LEVEL"
[ -n "$DUMP_CREDENTIALS" ] && warn "已开启完整 token 转储（仅调试用）"
echo ""

# 生产环境启动前检查：ERP 地址既无环境变量也无 config/production.env 定义时，
# 在停止旧服务之前报错退出，避免服务下线后才发现配置缺失
if [ "$GJP_ENV" = "production" ] && [ -z "$ERP_BILLING_BASE_URL" ]; then
    if ! grep -qE '^ERP_BILLING_BASE_URL=.+' "$DEPLOY_DIR/config/production.env" 2>/dev/null; then
        error "生产环境缺少 ERP_BILLING_BASE_URL：请通过环境变量注入，或在 config/production.env 配置"
        exit 1
    fi
fi

stop_service
pull_code
sync_deps
start_service
verify_service

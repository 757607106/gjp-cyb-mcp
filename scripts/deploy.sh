#!/bin/bash
# ERP MCP 代码一键部署。
#
# 用法：
#   ./scripts/deploy.sh test        # 部署远端 test 分支
#   ./scripts/deploy.sh production  # 部署远端 main 分支
#
# 首次安装、systemd、Nginx 与环境文件配置见：
# docs/deployment/server-deploy-runbook.md

set -euo pipefail

usage() {
    cat <<'EOF'
用法：./scripts/deploy.sh <test|production>

  test        部署 origin/test
  production  部署 origin/main

可选环境变量：
  DEPLOY_DIR       部署目录，默认 /root/yuncyb-mcp
  SERVICE_NAME     systemd 服务，默认按 APP_MODULE 自动选择
  APP_MODULE       ASGI 入口，默认 yuncyb.app:app
  PORT             本机健康检查端口，默认按 APP_MODULE 自动选择
  UV_INDEX_URL     Python 包镜像，默认阿里云 PyPI
  UV_HTTP_TIMEOUT  依赖下载超时秒数，默认 60
EOF
}

PROFILE="${1:-}"
case "$PROFILE" in
    test)
        TARGET_BRANCH="test"
        ;;
    production)
        TARGET_BRANCH="main"
        ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

if [ "$#" -ne 1 ]; then
    usage >&2
    exit 2
fi

DEPLOY_DIR="${DEPLOY_DIR:-/root/yuncyb-mcp}"
APP_MODULE="${APP_MODULE:-yuncyb.app:app}"
UV_INDEX_URL="${UV_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-60}"

if [ "$APP_MODULE" = "yuncyb.workbuddy_app:app" ]; then
    SERVICE_NAME="${SERVICE_NAME:-yuncyb-workbuddy-mcp}"
    PORT="${PORT:-8103}"
else
    SERVICE_NAME="${SERVICE_NAME:-yuncyb-mcp}"
    PORT="${PORT:-8102}"
fi

HEALTH_URL="http://127.0.0.1:${PORT}/healthz"

info() {
    printf '[INFO] %s\n' "$1"
}

error() {
    printf '[ERROR] %s\n' "$1" >&2
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        error "缺少命令：$1"
        exit 1
    fi
}

wait_for_health() {
    local attempt
    for attempt in $(seq 1 20); do
        if curl --fail --silent --show-error "$HEALTH_URL" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

if [ "$(id -u)" -ne 0 ]; then
    error "请使用 root 执行服务器部署脚本"
    exit 1
fi

for command_name in git uv systemctl curl flock; do
    require_command "$command_name"
done

if [ ! -d "$DEPLOY_DIR/.git" ]; then
    error "部署目录不是 Git 仓库：$DEPLOY_DIR"
    exit 1
fi

if ! systemctl cat "$SERVICE_NAME" >/dev/null 2>&1; then
    error "systemd 服务不存在：$SERVICE_NAME；请先按部署文档完成首次安装"
    exit 1
fi

exec 9>"/var/lock/${SERVICE_NAME}-deploy.lock"
if ! flock -n 9; then
    error "已有部署任务正在运行：$SERVICE_NAME"
    exit 1
fi

cd "$DEPLOY_DIR"

if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
    error "部署目录存在未提交改动，请先处理后再部署"
    git status --short
    exit 1
fi

PREVIOUS_COMMIT="$(git rev-parse HEAD)"

info "部署配置：$PROFILE（origin/$TARGET_BRANCH）"
info "部署目录：$DEPLOY_DIR"
info "服务名称：$SERVICE_NAME"
info "依赖镜像：$UV_INDEX_URL"

info "1/5 拉取远端分支"
git fetch --prune origin "$TARGET_BRANCH:refs/remotes/origin/$TARGET_BRANCH"

info "2/5 切换并快进到远端提交"
if git show-ref --verify --quiet "refs/heads/$TARGET_BRANCH"; then
    git switch "$TARGET_BRANCH"
else
    git switch --track -c "$TARGET_BRANCH" "origin/$TARGET_BRANCH"
fi
git merge --ff-only "origin/$TARGET_BRANCH"

if [ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$TARGET_BRANCH")" ]; then
    error "本地分支与 origin/$TARGET_BRANCH 不一致，已停止部署"
    exit 1
fi

info "3/5 使用锁文件同步生产依赖"
export UV_INDEX_URL UV_HTTP_TIMEOUT
uv sync --frozen --no-dev

info "4/5 执行启动前导入检查"
"$DEPLOY_DIR/.venv/bin/python" -c 'import yuncyb.app'

info "5/5 重启并检查健康状态"
restart_ok=true
if ! systemctl restart "$SERVICE_NAME"; then
    restart_ok=false
elif ! wait_for_health; then
    restart_ok=false
fi

if ! "$restart_ok"; then
    error "新版本健康检查失败，开始回滚到 $PREVIOUS_COMMIT"
    journalctl -u "$SERVICE_NAME" -n 50 --no-pager >&2 || true
    if git switch --detach "$PREVIOUS_COMMIT" \
        && uv sync --frozen --no-dev \
        && systemctl restart "$SERVICE_NAME" \
        && wait_for_health; then
        error "已自动回滚，服务恢复；请检查新版本日志"
    else
        error "回滚后健康检查仍失败，请立即检查 systemd 日志"
    fi
    exit 1
fi

info "部署完成：$(git log -1 --oneline)"
info "服务状态：$(systemctl is-active "$SERVICE_NAME")"
info "健康检查：$HEALTH_URL"

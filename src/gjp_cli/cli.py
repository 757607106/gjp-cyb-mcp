"""ERP 开单本地参考客户端：命令注册与分发。"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from gjp_common.config import get_env_value, local_env_path
from gjp_common.errors import DomainError
from gjp_common.logging_config import configure_logging, logging_runtime_settings
from gjp_common.tracing_config import configure_tracing, tracing_runtime_settings
from gjp_common.paths import discover_project_root
from .model_runtime import LLMSettings, supported_model_providers


logger = logging.getLogger(__name__)


def _cmd_doctor(args: argparse.Namespace) -> int:
    import agentscope

    project_root = discover_project_root()
    env_path = local_env_path()
    model = LLMSettings.from_env()
    logging_settings = logging_runtime_settings()
    tracing_settings = tracing_runtime_settings()
    # 图片识别链路可选；未配置时只报告原因，不阻断诊断。
    try:
        vision = LLMSettings.vision_from_env()
        vision_summary = {
            "configured": True,
            "provider": vision.provider,
            "modelName": vision.model_name,
            "baseUrl": vision.base_url,
            "stream": vision.stream,
            "timeoutSeconds": vision.timeout_seconds,
            "apiKeyConfigured": bool(vision.api_key),
        }
    except DomainError as exc:
        vision_summary = {"configured": False, "reason": exc.message}
    summary = {
        "status": "ok",
        "currentWorkingDirectory": str(Path.cwd().resolve()),
        "projectRoot": str(project_root),
        "relativePathRule": "所有CLI相对输入/输出路径优先按projectRoot解析",
        "envFile": str(env_path) if env_path else None,
        "logging": logging_settings,
        "tracing": tracing_settings,
        "model": {
            "provider": model.provider,
            "baseUrl": model.base_url,
            "modelName": model.model_name,
            "stream": model.stream,
            "parameters": model.parameters,
            "maxRetries": model.max_retries,
            "contextSize": model.context_size,
            "apiKeyConfigured": bool(model.api_key),
            "supportedProviders": supported_model_providers(),
        },
        "visionModel": vision_summary,
        "agentRuntime": {
            "framework": "AgentScope",
            "version": agentscope.__version__,
            "conversation": "erp-billing-agent-console",
            "intentRouting": "model-tool-calling",
            "agentKeys": ["erp-billing"],
            "agentSelection": "billing-only",
        },
        "erpBilling": {
            "baseUrl": get_env_value("ERP_BILLING_BASE_URL"),
            "productEndpoint": "/product/page",
            "salesOrderEndpoint": "POST /sales/orders",
            "authentication": "server-side-bearer",
            "cliUpstreamTokenEnv": "ERP_BILLING_UPSTREAM_TOKEN",
        },
        "importantPaths": {
            "source": str(project_root / "src" / "erp_billing"),
            "documentation": str(project_root / "docs"),
            "tests": str(project_root / "tests"),
        },
        "generateFlow": [
            "discover-project-root",
            "load-.env",
            "build-erp-billing-agent",
            "natural-language-tool-selection",
            "sync-session-product-catalog",
            "browse-product-catalog",
            "parse-complete-order-text",
            "match-real-erp-products",
            "resolve-customer-warehouse-handler",
            "preview-sales-order",
            "confirm-and-submit-sales-order",
        ],
        "currentBoundaries": {
            "productCatalog": "session-isolated-memory",
            "mediaInput": "local-cli-only-preprocessing",
            "erpWrite": "billing:write + explicit-confirmation + idempotency-key",
            "persistentDraftFiles": False,
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    logger.info("项目诊断完成 project_root=%s env=%s", project_root, env_path)
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    from .interactive import build_agent_console

    return build_agent_console(
        agent_key="erp-billing",
        resume=getattr(args, "resume", False),
    ).run()


def _prompt_token(prompt: str) -> str:
    """交互式读取 Token：粘贴后回车，输入不回显；非终端或取消时返回空。"""
    if not sys.stdin.isatty():
        return ""
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _strip_bearer_prefix(value: str) -> str:
    """兼容 `Bearer <JWT>` 和裸 JWT 两种写法。"""
    token = value.strip()
    scheme, separator, credential = token.partition(" ")
    if separator and scheme.casefold() == "bearer":
        return credential.strip()
    return token


def _cmd_mcp_chat(args: argparse.Namespace) -> int:
    from .mcp_console import (
        build_mcp_agent_console,
        default_mcp_url,
    )

    agent_key = "erp-billing"
    mcp_url = str(getattr(args, "url", "") or "").strip() or default_mcp_url(agent_key)
    bearer_token = str(getattr(args, "token", "") or "").strip()
    if not bearer_token:
        bearer_token = str(getattr(args, "upstream_token", "") or "").strip()
    if not bearer_token:
        bearer_token = get_env_value("ERP_BILLING_UPSTREAM_TOKEN").strip()
    if not bearer_token:
        bearer_token = get_env_value("MCP_ACCESS_TOKEN").strip()
    if not bearer_token:
        raw_token = _prompt_token(
            "请粘贴 ERP Token（可为 `Bearer <JWT>` 原文，输入不回显）：",
        )
        if raw_token:
            bearer_token = _strip_bearer_prefix(raw_token)
    if not bearer_token:
        raise DomainError(
            "MCP_CHAT_AUTH_REQUIRED",
            "缺少 ERP Token；启动后可直接粘贴，"
            "或传 --upstream-token / ERP_BILLING_UPSTREAM_TOKEN；"
            "生产服务传 --token 或 MCP_ACCESS_TOKEN",
        )
    return build_mcp_agent_console(
        agent_key=agent_key,
        mcp_url=mcp_url,
        bearer_token=_strip_bearer_prefix(bearer_token),
    ).run()


def _cmd_demo(args: argparse.Namespace) -> int:
    from .demo_console import run_saas_demo

    upstream_token = str(getattr(args, "upstream_token", "") or "").strip()
    if not upstream_token:
        upstream_token = get_env_value("ERP_BILLING_UPSTREAM_TOKEN").strip()
    if not upstream_token:
        upstream_token = _prompt_token(
            "请粘贴 ERP Token（可为 `Bearer <JWT>` 原文，输入不回显）：",
        )
    return run_saas_demo(
        mcp_url=str(getattr(args, "url", "") or ""),
        upstream_token=upstream_token,
    )


def _cmd_erp_bill(args: argparse.Namespace) -> int:
    from .billing_cli import run_billing_cli

    return run_billing_cli(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gjp-cli",
        description="GJP ERP AI 开单自然语言 Agent 控制台",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser(
        "demo",
        help="用已有 ERP Token 连接开单 MCP",
        description=(
            "仅用于本地或 test/live 验证：用已有 ERP Token "
            "直接连接 MCP 服务进入文本对话。"
        ),
    )
    demo.add_argument(
        "--url",
        help="MCP Streamable HTTP 地址；默认 http://127.0.0.1:8102/mcp",
    )
    demo.add_argument(
        "--upstream-token",
        help="已有 ERP Token；未提供时启动后交互式粘贴（推荐）",
    )
    demo.set_defaults(handler=_cmd_demo)

    doctor = subparsers.add_parser("doctor", help="显示项目根目录、配置来源、运行链路和当前能力边界")
    doctor.set_defaults(handler=_cmd_doctor)

    chat = subparsers.add_parser(
        "chat",
        help="启动 ERP AI 开单 AgentScope 自然语言多轮会话",
    )
    chat.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="恢复上一轮会话上下文，保留对话历史",
    )
    chat.set_defaults(handler=_cmd_chat)

    mcp_chat = subparsers.add_parser(
        "mcp-chat",
        help="通过远程开单 MCP 服务启动 AgentScope 对话测试",
        description="通过 AgentScope MCPClient 连接 ERP 开单 MCP HTTP 服务。",
    )
    mcp_chat.add_argument(
        "--url",
        help="MCP Streamable HTTP 地址；默认 http://127.0.0.1:8102/mcp",
    )
    mcp_auth = mcp_chat.add_mutually_exclusive_group()
    mcp_auth.add_argument(
        "--token",
        help="已有 MCP Bearer token；生产服务使用",
    )
    mcp_auth.add_argument(
        "--upstream-token",
        help="已有 ERP Token；未提供时启动后交互式粘贴（推荐）",
    )
    mcp_chat.set_defaults(handler=_cmd_mcp_chat)

    erp_bill = subparsers.add_parser(
        "erp-bill",
        help="启动 ERP AI 开单 CLI 会话",
        description="从业务方预处理后的订单文本或下单图片生成销售单草稿，并匹配系统商品目录。",
    )
    erp_bill.add_argument("--text", help="直接传入客户下单文本")
    erp_bill.add_argument("--input-file", help="读取 UTF-8 文本文件作为客户下单内容")
    erp_bill.add_argument(
        "--image",
        help="读取下单图片，用 LLM_VISION_* 多模态模型识别为订单后开单",
    )
    erp_bill.add_argument("--customer", default="", help="草稿客户名称")
    erp_bill.add_argument("--warehouse", default="", help="草稿仓库名称")
    erp_bill.set_defaults(handler=_cmd_erp_bill)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    actual_argv = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(actual_argv or ["demo"])
    configure_logging()
    configure_tracing()
    logger.info(
        "命令开始 command=%s cwd=%s project_root=%s path_rule=project-root-relative",
        args.command,
        Path.cwd().resolve(),
        discover_project_root(),
    )
    try:
        exit_code = args.handler(args)
    except DomainError as exc:
        logger.error("命令失败 command=%s code=%s message=%s", args.command, exc.code, exc.message)
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
    logger.info("命令结束 command=%s exit_code=%d", args.command, exit_code)
    raise SystemExit(exit_code)

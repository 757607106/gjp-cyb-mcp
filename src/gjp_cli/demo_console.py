"""使用已有 ERP Token 的本地 SaaS 对话页模拟器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .mcp_console import (
    build_mcp_agent_console,
    default_mcp_url,
    normalize_agent_key,
    request_validation_bearer_from_upstream_token,
)
from gjp_common.config import get_env_value
from gjp_common.errors import DomainError


OutputFn = Callable[[str], None]


def run_saas_demo(
    *,
    mcp_url: str = "",
    upstream_token: str = "",
    token_url: str = "",
    output_fn: OutputFn = print,
    console_builder: Callable[..., Any] = build_mcp_agent_console,
) -> int:
    """登记已有 ERP Token，并启动远程 MCP 文本对话。"""
    agent_key = normalize_agent_key("erp-billing")
    target_mcp_url = mcp_url.strip() or default_mcp_url(agent_key)
    existing_token = upstream_token.strip() or get_env_value("ERP_BILLING_UPSTREAM_TOKEN").strip()
    if not existing_token:
        raise DomainError(
            "DEMO_TOKEN_REQUIRED",
            "缺少 ERP Token；请配置 ERP_BILLING_UPSTREAM_TOKEN 或传 --upstream-token",
        )
    bearer = request_validation_bearer_from_upstream_token(
        mcp_url=target_mcp_url,
        token_url=token_url,
        upstream_token=existing_token,
    )
    output_fn("已登记 ERP Token 并建立临时 CLI 会话。现在可以输入业务文本进行对话。")
    return console_builder(
        agent_key=agent_key,
        mcp_url=target_mcp_url,
        bearer_token=bearer,
    ).run()

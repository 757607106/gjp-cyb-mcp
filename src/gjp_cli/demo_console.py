"""使用已有 ERP Token 的本地 SaaS 对话页模拟器。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .mcp_console import (
    build_mcp_agent_console,
    default_mcp_url,
    normalize_agent_key,
)
from gjp_common.config import get_env_value
from gjp_common.errors import DomainError


OutputFn = Callable[[str], None]


def run_saas_demo(
    *,
    mcp_url: str = "",
    upstream_token: str = "",
    output_fn: OutputFn = print,
    console_builder: Callable[..., Any] = build_mcp_agent_console,
) -> int:
    """用已有 ERP Token 直接连接 MCP 服务，启动文本对话。"""
    agent_key = normalize_agent_key("erp-billing")
    target_mcp_url = mcp_url.strip() or default_mcp_url(agent_key)
    bearer_token = upstream_token.strip() or get_env_value("ERP_BILLING_UPSTREAM_TOKEN").strip()
    if not bearer_token:
        raise DomainError(
            "DEMO_TOKEN_REQUIRED",
            "缺少 ERP Token；请配置 ERP_BILLING_UPSTREAM_TOKEN 或传 --upstream-token",
        )
    output_fn("已使用 ERP Token 连接 MCP 服务。现在可以输入业务文本进行对话。")
    return console_builder(
        agent_key=agent_key,
        mcp_url=target_mcp_url,
        bearer_token=_strip_bearer_prefix(bearer_token),
    ).run()


def _strip_bearer_prefix(value: str) -> str:
    """兼容 `Bearer <JWT>` 和裸 JWT 两种写法。"""
    token = value.strip()
    scheme, separator, credential = token.partition(" ")
    if separator and scheme.casefold() == "bearer":
        return credential.strip()
    return token

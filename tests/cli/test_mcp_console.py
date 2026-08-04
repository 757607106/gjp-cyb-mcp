import argparse
import asyncio

import pytest
from agentscope.permission import PermissionBehavior, PermissionEngine
from agentscope.tool import MCPTool
from mcp.types import Tool

from gjp_cli.cli import _cmd_mcp_chat, build_parser
from gjp_cli.demo_console import run_saas_demo
from gjp_cli.mcp_console import (
    build_mcp_agent_console,
    build_product_mcp_agent_state,
    default_mcp_url,
)
from gjp_cli.interactive import erp_billing_user_input_transformer
from gjp_common.errors import DomainError


def test_mcp_chat_parser_accepts_cli_upstream_token_mode():
    parser = build_parser()
    args = parser.parse_args(["mcp-chat", "--upstream-token", "erp-token"])

    assert args.command == "mcp-chat"
    assert args.upstream_token == "erp-token"


def test_demo_parser_targets_billing_without_product_selection():
    parser = build_parser()
    args = parser.parse_args(["demo"])

    assert args.command == "demo"
    assert not hasattr(args, "product")


def test_demo_uses_erp_token_directly_as_bearer(monkeypatch):
    """ERP Token 不再换票，直接作为 MCP Bearer 使用。"""
    console_calls = []
    output = []

    class FakeConsole:
        @staticmethod
        def run():
            return 0

    result = run_saas_demo(
        upstream_token="Bearer erp-jwt-token",
        output_fn=output.append,
        console_builder=lambda **kwargs: console_calls.append(kwargs) or FakeConsole(),
    )

    assert result == 0
    assert console_calls[0]["bearer_token"] == "erp-jwt-token"
    assert "erp-jwt-token" not in "\n".join(output)


def test_mcp_chat_defaults_to_billing_service_port(monkeypatch):
    monkeypatch.delenv("BILLING_MCP_URL", raising=False)

    assert default_mcp_url("erp-billing") == "http://127.0.0.1:8102/mcp"


def test_mcp_chat_requires_token(monkeypatch):
    monkeypatch.delenv("MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ERP_BILLING_UPSTREAM_TOKEN", raising=False)
    args = argparse.Namespace(
        url="http://127.0.0.1:8102/mcp",
        token="",
        upstream_token="",
    )

    with pytest.raises(DomainError, match="缺少 ERP Token"):
        _cmd_mcp_chat(args)


@pytest.mark.parametrize(
    ("agent_key", "mcp_name", "tool_name"),
    [
        (
            "erp-billing",
            "erp-billing",
            "prepare_sales_order",
        ),
        ("erp-billing", "erp-billing", "sync_products"),
    ],
)
def test_mcp_chat_auto_allows_authenticated_product_tools(
    agent_key,
    mcp_name,
    tool_name,
):
    state = build_product_mcp_agent_state(agent_key, mcp_name)
    tool = MCPTool(
        mcp_name=mcp_name,
        tool=Tool(name=tool_name, inputSchema={"type": "object"}),
        session=object(),
    )

    decision = asyncio.run(
        PermissionEngine(state.permission_context).check_permission(tool, {})
    )

    assert decision.behavior == PermissionBehavior.ALLOW


def test_mcp_chat_does_not_auto_allow_unknown_mcp_tool():
    state = build_product_mcp_agent_state("erp-billing", "erp-billing")
    tool = MCPTool(
        mcp_name="erp-billing",
        tool=Tool(name="unknown_future_tool", inputSchema={"type": "object"}),
        session=object(),
    )

    decision = asyncio.run(
        PermissionEngine(state.permission_context).check_permission(tool, {})
    )

    assert decision.behavior == PermissionBehavior.ASK


def test_mcp_console_wires_billing_image_input_transformer(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "LLM_TEXT_PROVIDER=openai",
                "LLM_TEXT_MODEL_NAME=test-model",
                "LLM_TEXT_API_KEY=test-key",
            ],
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GJP_ENV_FILE", str(env_file))

    billing_console = build_mcp_agent_console(
        agent_key="erp-billing",
        mcp_url="http://127.0.0.1:8102/mcp",
        bearer_token="test-bearer",
    )
    assert billing_console.user_input_transformer is erp_billing_user_input_transformer

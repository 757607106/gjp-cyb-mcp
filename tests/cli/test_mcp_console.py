import argparse
import asyncio
import json
import urllib.error

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
    request_validation_bearer_from_upstream_token,
    validation_token_url_from_mcp_url,
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


def test_validation_upstream_token_posts_only_to_cli_token_endpoint(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"ok":true,"accessToken":"temporary-mcp-bearer"}'

    def fake_urlopen(request, timeout):
        captured.update(
            url=request.full_url,
            payload=json.loads(request.data.decode("utf-8")),
            timeout=timeout,
        )
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    token = request_validation_bearer_from_upstream_token(
        mcp_url="http://127.0.0.1:8102/mcp",
        upstream_token="Bearer browser-erp-token",
        metadata={"session_id": "cli-session"},
        timeout_seconds=12,
    )

    assert token == "temporary-mcp-bearer"
    assert captured == {
        "url": "http://127.0.0.1:8102/test-auth/token",
        "payload": {
            "session_id": "cli-session",
            "upstreamToken": "browser-erp-token",
        },
        "timeout": 12,
    }


def test_validation_token_unavailable_message_includes_local_start_command(monkeypatch):
    def fake_urlopen(_request, timeout):
        assert timeout == 1
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(DomainError) as caught:
        request_validation_bearer_from_upstream_token(
            mcp_url="http://127.0.0.1:8102/mcp",
            upstream_token="secret",
            timeout_seconds=1,
        )

    assert caught.value.code == "MCP_CHAT_LOGIN_FAILED"
    assert "http://127.0.0.1:8102/test-auth/token" in caught.value.message
    assert "uv run uvicorn gjp_cli.billing_validation:app" in caught.value.message
    assert "secret" not in caught.value.message


def test_demo_upstream_token_mode_skips_captcha_and_password_prompts(monkeypatch):
    auth_calls = []
    console_calls = []
    output = []

    class FakeConsole:
        @staticmethod
        def run():
            return 0

    monkeypatch.setattr(
        "gjp_cli.demo_console.request_validation_bearer_from_upstream_token",
        lambda **kwargs: auth_calls.append(kwargs) or "mcp-bearer",
    )
    result = run_saas_demo(
        upstream_token="browser-erp-token",
        output_fn=output.append,
        console_builder=lambda **kwargs: console_calls.append(kwargs) or FakeConsole(),
    )

    assert result == 0
    assert auth_calls[0]["upstream_token"] == "browser-erp-token"
    assert console_calls[0]["bearer_token"] == "mcp-bearer"
    assert "browser-erp-token" not in "\n".join(output)


def test_mcp_chat_defaults_to_billing_service_port(monkeypatch):
    monkeypatch.delenv("BILLING_MCP_URL", raising=False)

    assert default_mcp_url("erp-billing") == "http://127.0.0.1:8102/mcp"


def test_mcp_chat_derives_validation_token_url_from_mcp_url():
    assert (
        validation_token_url_from_mcp_url("https://example.test/billing/mcp?x=1")
        == "https://example.test/billing/test-auth/token"
    )


def test_mcp_chat_requires_mcp_or_upstream_token(monkeypatch):
    monkeypatch.delenv("MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ERP_BILLING_UPSTREAM_TOKEN", raising=False)
    args = argparse.Namespace(
        url="http://127.0.0.1:8102/mcp",
        token="",
        upstream_token="",
        token_url="",
    )

    with pytest.raises(DomainError, match="缺少 MCP Bearer"):
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
        PermissionEngine(state.permission_context).check_permission(tool, {}),
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
        PermissionEngine(state.permission_context).check_permission(tool, {}),
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

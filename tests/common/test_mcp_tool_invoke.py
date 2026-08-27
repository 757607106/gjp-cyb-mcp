"""验证 MCP harness 的工具调用容错：参数错误转结构化错误与探活路由。"""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.routing import Route
from starlette.testclient import TestClient

from gjp_common.context import InvocationContext, InvocationContextStore
from gjp_common.mcp import (
    McpIdentityResolver,
    McpToolSetResolver,
    _invoke_tool,
    create_mcp_http_app,
    create_mcp_server,
)
from gjp_common.tools import SessionFunctionTool


def _context() -> InvocationContext:
    return InvocationContext(
        tenant_id="tenant-test",
        subject_id="user-test",
        account_id="billing-test",
        session_id="session-test",
        scopes=frozenset({"billing:read"}),
    )


async def _sample_tool(limit: int = 10) -> dict[str, Any]:
    """示例工具：签名只接受 limit 整数参数。"""
    return {"ok": True, "limit": limit}


def _tool() -> SessionFunctionTool:
    return SessionFunctionTool(_sample_tool)


def test_invoke_tool_translates_binding_error_to_structured_error() -> None:
    """参数名不匹配时应返回 ok=false 的结构化错误，模型可据此纠正重试。"""
    result = asyncio.run(
        _invoke_tool(_tool(), {"limitValue": 5}, tenant_id="tenant-test"),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "tool_arguments_invalid"
    assert "limitValue" in result["error"]["message"]


def test_invoke_tool_returns_normal_result() -> None:
    """正常参数路径不受转译影响。"""
    result = asyncio.run(
        _invoke_tool(_tool(), {"limit": 5}, tenant_id="tenant-test"),
    )

    assert result == {"ok": True, "limit": 5}


class _StaticIdentity(McpIdentityResolver):
    def resolve(self, _mcp_request_context: Any) -> InvocationContext:
        return _context()


class _StaticToolSet(McpToolSetResolver):
    def __init__(self) -> None:
        self._store = InvocationContextStore(default=_context())
        self._tool = SessionFunctionTool(_sample_tool)

    def resolve(self, _context: InvocationContext) -> Any:
        from gjp_common.toolset import AgentScopeToolSet

        return AgentScopeToolSet([self._tool], contexts=self._store)


def _make_app() -> Any:
    return create_mcp_http_app(
        create_mcp_server(
            "test-service",
            _StaticToolSet().resolve(_context()),
            _StaticIdentity(),
            _StaticToolSet(),
        ),
    )


def test_http_app_exposes_healthz() -> None:
    """/healthz 无需鉴权即可探活，供负载均衡使用。"""
    app = _make_app()
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_http_app_keeps_mcp_route() -> None:
    """新增探活路由不影响既有 /mcp 端点。"""
    app = _make_app()
    paths = {
        route.path
        for route in app.routes
        if isinstance(route, Route)
    }

    assert "/healthz" in paths
    assert "/mcp" in paths

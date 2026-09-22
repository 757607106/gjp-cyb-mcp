"""验证 MCPServer 发布层的工具调用行为与 HTTP 装配。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server import ServerRequestContext
from mcp.server.mcpserver import Context
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS
from starlette.routing import Route
from starlette.testclient import TestClient

from gjp_common.context import InvocationContext, InvocationContextStore
from gjp_common.mcp import (
    McpRateLimitMiddleware,
    McpIdentityResolver,
    McpToolSetResolver,
    create_mcp_http_app,
    create_mcp_server,
)
from gjp_common.tools import SessionFunctionTool
from gjp_common.toolset import SessionToolSet

_CALLS: list[str] = []


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
    _CALLS.append("invoked")
    return {"ok": True, "limit": limit}


class _DictHeaders:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def get(self, name: str, default: str = "") -> str:
        return self._data.get(name, default)


def _fake_request_context() -> SimpleNamespace:
    return ServerRequestContext(
        session=SimpleNamespace(),
        lifespan_context=None,
        protocol_version="2026-07-28",
        method="tools/call",
        request_id=1,
        request=SimpleNamespace(
            headers=_DictHeaders({"authorization": "Bearer test-token"}),
        ),
    )


class _RecordingIdentity(McpIdentityResolver):
    def __init__(self) -> None:
        self.seen_authorization: list[str] = []

    def resolve(self, mcp_request_context: Any) -> InvocationContext:
        headers = getattr(mcp_request_context.request, "headers")
        self.seen_authorization.append(headers.get("authorization"))
        return _context()


class _StaticToolSet(McpToolSetResolver):
    def __init__(self, toolset: SessionToolSet) -> None:
        self._toolset = toolset

    def resolve(self, _context: InvocationContext) -> SessionToolSet:
        return self._toolset


def _make_server(identity: McpIdentityResolver | None = None) -> Any:
    toolset = SessionToolSet(
        [SessionFunctionTool(_sample_tool)],
        contexts=InvocationContextStore(default=_context()),
    )
    return create_mcp_server(
        "test-service",
        toolset,
        identity or _RecordingIdentity(),
        _StaticToolSet(toolset),
    )


def _make_enum_server() -> Any:
    async def typed_tool(level: str = "info") -> dict[str, Any]:
        """示例工具：level 只允许 info 或 warn。"""
        _CALLS.append("typed")
        return {"ok": True, "level": level}

    toolset = SessionToolSet(
        [
            SessionFunctionTool(
                typed_tool,
                input_schema_override={
                    "type": "object",
                    "properties": {
                        "level": {"type": "string", "enum": ["info", "warn"]},
                    },
                },
            ),
        ],
        contexts=InvocationContextStore(default=_context()),
    )
    return create_mcp_server(
        "test-service",
        toolset,
        _RecordingIdentity(),
        _StaticToolSet(toolset),
    )


def _call(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(
        server.call_tool(
            name,
            arguments,
            Context(request_context=_fake_request_context(), mcp_server=server),
        ),
    )


def _call_protocol(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    """走 MCPServer 公共调用入口，覆盖参数契约与结果映射。"""
    return _call(server, name, arguments)


def test_call_tool_dispatches_to_session_toolset() -> None:
    """正常参数经身份解析与会话绑定后返回结构化结果。"""
    _CALLS.clear()
    identity = _RecordingIdentity()
    server = _make_server(identity)
    result = _call(server, "sampleTool", {"limit": 5})

    assert result.structured_content == {"ok": True, "limit": 5}
    assert _CALLS == ["invoked"]
    assert identity.seen_authorization == ["Bearer test-token"]


def test_call_tool_type_error_becomes_tool_error() -> None:
    """参数类型非法时由 JSON Schema 校验拒绝，转为协议级工具错误。"""
    server = _make_server()
    result = _call(server, "sampleTool", {"limit": "not-a-number"})
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "tool_arguments_invalid"


def test_protocol_call_passes_through_guard() -> None:
    """合法参数经守卫回调正常到达业务工具。"""
    _CALLS.clear()
    server = _make_enum_server()
    result = _call_protocol(server, "typedTool", {"level": "warn"})

    assert result.is_error is False
    assert result.structured_content == {"ok": True, "level": "warn"}
    assert _CALLS == ["typed"]


def test_protocol_call_projects_both_content_and_structured_result() -> None:
    toolset = SessionToolSet(
        [SessionFunctionTool(_sample_tool)],
        contexts=InvocationContextStore(default=_context()),
    )
    server = create_mcp_server(
        "test-service",
        toolset,
        _RecordingIdentity(),
        _StaticToolSet(toolset),
        result_presenter=lambda _name, result: ("业务展示：仅保留业务信息", {"ok": result["ok"]}),
    )

    result = _call_protocol(server, "sampleTool", {"limit": 5})

    assert result.structured_content == {"ok": True}
    assert [block.text for block in result.content] == [
        "业务展示：仅保留业务信息",
    ]


def test_arguments_guard_dictionary_result_uses_both_projections() -> None:
    _CALLS.clear()
    toolset = SessionToolSet(
        [SessionFunctionTool(_sample_tool)],
        contexts=InvocationContextStore(default=_context()),
    )
    server = create_mcp_server(
        "test-service",
        toolset,
        _RecordingIdentity(),
        _StaticToolSet(toolset),
        result_presenter=lambda _name, result: (
            "请核对业务信息。",
            {"ok": result["ok"], "error": {"code": result["error"]["code"], "message": "请核对业务信息。"}},
        ),
    )

    result = _call_protocol(server, "sampleTool", {"unknownField": "internal-value"})

    assert result.is_error is True
    assert result.structured_content == {
        "ok": False,
        "error": {"code": "tool_arguments_invalid", "message": "请核对业务信息。"},
    }
    assert [block.text for block in result.content] == ["请核对业务信息。"]
    assert _CALLS == []


def test_protocol_schema_violation_is_protocol_error() -> None:
    """枚举等 Schema 约束违规得到 isError=True，文案与官方校验一致。"""
    _CALLS.clear()
    server = _make_enum_server()
    result = _call_protocol(server, "typedTool", {"level": "urgent"})

    assert result.is_error is True
    text = " ".join(block.text for block in result.content)
    assert "Input validation error" in text
    assert "urgent" in text
    assert _CALLS == []


def test_protocol_schema_type_violation_is_protocol_error() -> None:
    """参数类型与 Schema 不符时同样在协议层拦截。"""
    _CALLS.clear()
    server = _make_enum_server()
    result = _call_protocol(server, "typedTool", {"level": 3})

    assert result.is_error is True
    text = " ".join(block.text for block in result.content)
    assert "Input validation error" in text
    assert _CALLS == []


def test_protocol_unknown_argument_returns_structured_error() -> None:
    """未知参数名返回结构化 tool_arguments_invalid，模型可自行纠正。"""
    _CALLS.clear()
    server = _make_server()
    result = _call_protocol(server, "sampleTool", {"pageValue": 1})

    assert result.is_error is True
    assert result.structured_content["ok"] is False
    assert result.structured_content["error"]["code"] == "tool_arguments_invalid"
    assert "pageValue" in result.structured_content["error"]["message"]
    assert _CALLS == []


def test_unknown_tool_raises_protocol_invalid_params() -> None:
    """未知工具属于协议错误，不伪装成已完成的工具执行结果。"""
    server = _make_server()

    with pytest.raises(MCPError) as excinfo:
        _call_protocol(server, "missingTool", {})

    assert excinfo.value.error.code == INVALID_PARAMS
    assert excinfo.value.error.data == {"name": "missingTool"}


def test_protocol_missing_required_argument_is_protocol_error() -> None:
    """缺失必填参数属于 Schema 违规，得到协议级错误。"""

    async def required_tool(order_id: str) -> dict[str, Any]:
        """示例工具：order_id 必填。"""
        return {"ok": True, "order_id": order_id}

    toolset = SessionToolSet(
        [SessionFunctionTool(required_tool)],
        contexts=InvocationContextStore(default=_context()),
    )
    server = create_mcp_server(
        "test-service",
        toolset,
        _RecordingIdentity(),
        _StaticToolSet(toolset),
    )
    result = _call_protocol(server, "requiredTool", {})

    assert result.is_error is True
    text = " ".join(block.text for block in result.content)
    assert "Input validation error" in text
    assert "order_id" in text


def test_call_tool_identity_failure_propagates() -> None:
    """身份解析失败时调用中断，不触及业务工具。"""

    class _RejectingIdentity(McpIdentityResolver):
        def resolve(self, _mcp_request_context: Any) -> InvocationContext:
            raise PermissionError("unauthorized")

    _CALLS.clear()
    server = _make_server(_RejectingIdentity())
    with pytest.raises(PermissionError):
        _call(server, "sampleTool", {"limit": 5})
    assert _CALLS == []


def test_call_tool_rejects_schema_mismatch() -> None:
    """运行时工具 schema 与发布版本不一致时拒绝执行。"""
    schema_toolset = SessionToolSet(
        [SessionFunctionTool(_sample_tool)],
        contexts=InvocationContextStore(default=_context()),
    )
    runtime_toolset = SessionToolSet(
        [
            SessionFunctionTool(
                _sample_tool,
                input_schema_override={
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "maximum": 3}},
                },
            ),
        ],
        contexts=InvocationContextStore(default=_context()),
    )

    class _RuntimeResolver(McpToolSetResolver):
        def resolve(self, _context: InvocationContext) -> SessionToolSet:
            return runtime_toolset

    server = create_mcp_server(
        "test-service",
        schema_toolset,
        _RecordingIdentity(),
        _RuntimeResolver(),
    )
    with pytest.raises(ValueError):
        _call(server, "sampleTool", {"limit": 5})


def test_http_app_exposes_healthz() -> None:
    """/healthz 无需鉴权即可探活，供负载均衡使用。"""
    app = create_mcp_http_app(_make_server())
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_http_app_keeps_mcp_route() -> None:
    """新增探活路由不影响既有 /mcp 端点。"""
    app = create_mcp_http_app(_make_server())
    paths = {
        route.path
        for route in app.routes
        if isinstance(route, Route)
    }

    assert "/healthz" in paths
    assert "/mcp" in paths


def test_http_app_serves_2026_tools_list_on_public_host() -> None:
    """2026-07-28 使用逐请求元数据，无需 initialize 握手。"""
    app = create_mcp_http_app(
        _make_server(),
        allowed_hosts=["mcp.example.com"],
        allowed_origins=["https://app.example.com"],
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
                "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
            },
        },
    }
    headers = {
        "Host": "mcp.example.com",
        "Origin": "https://app.example.com",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/list",
        "Accept": "application/json",
    }

    with TestClient(app) as client:
        response = client.post("/mcp", json=request, headers=headers)

    assert response.status_code == 200
    assert response.json()["result"]["tools"][0]["name"] == "sampleTool"


def test_http_app_rejects_unconfigured_host_and_origin() -> None:
    app = create_mcp_http_app(
        _make_server(),
        allowed_hosts=["mcp.example.com"],
        allowed_origins=["https://app.example.com"],
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    headers = {
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/list",
        "Accept": "application/json",
    }

    with TestClient(app) as client:
        bad_host = client.post(
            "/mcp",
            json=request,
            headers={**headers, "Host": "evil.example.com"},
        )
        bad_origin = client.post(
            "/mcp",
            json=request,
            headers={
                **headers,
                "Host": "mcp.example.com",
                "Origin": "https://evil.example.com",
            },
        )

    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403


def test_http_app_2026_business_failure_sets_is_error() -> None:
    async def failing_tool() -> dict[str, Any]:
        return {"ok": False, "error": {"code": "business_rejected", "message": "业务拒绝"}}

    toolset = SessionToolSet(
        [SessionFunctionTool(failing_tool)],
        contexts=InvocationContextStore(default=_context()),
    )
    server = create_mcp_server(
        "test-service",
        toolset,
        _RecordingIdentity(),
        _StaticToolSet(toolset),
    )
    app = create_mcp_http_app(server)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "failingTool",
            "arguments": {},
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    headers = {
        "Host": "localhost",
        "Authorization": "Bearer test-token",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call",
        "Mcp-Name": "failingTool",
        "Accept": "application/json",
    }

    with TestClient(app) as client:
        response = client.post("/mcp", json=request, headers=headers)

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert response.json()["result"]["structuredContent"]["error"]["code"] == "business_rejected"


def test_http_app_unknown_tool_returns_json_rpc_invalid_params() -> None:
    app = create_mcp_http_app(_make_server())
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "missingTool",
            "arguments": {},
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    headers = {
        "Host": "localhost",
        "Authorization": "Bearer test-token",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call",
        "Mcp-Name": "missingTool",
        "Accept": "application/json",
    }

    with TestClient(app) as client:
        response = client.post("/mcp", json=request, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": INVALID_PARAMS,
        "message": "未知工具：missingTool",
        "data": {"name": "missingTool"},
    }


def test_mcp_rate_limit_is_per_credential_and_excludes_healthz() -> None:
    app = McpRateLimitMiddleware(
        create_mcp_http_app(_make_server()),
        requests_per_window=2,
        window_seconds=60,
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    headers = {
        "Host": "localhost",
        "Authorization": "Bearer token-a",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/list",
        "Accept": "application/json",
    }

    with TestClient(app) as client:
        assert client.post("/mcp", json=request, headers=headers).status_code == 200
        assert client.post("/mcp", json=request, headers=headers).status_code == 200
        limited = client.post("/mcp", json=request, headers=headers)
        other_credential = client.post(
            "/mcp",
            json=request,
            headers={**headers, "Authorization": "Bearer token-b"},
        )
        health = client.get("/healthz")

    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limit_exceeded"
    assert int(limited.headers["retry-after"]) >= 1
    assert other_credential.status_code == 200
    assert health.status_code == 200


def test_http_app_runs_shutdown_callback() -> None:
    """ASGI 生命周期结束时应释放 MCP 服务持有的共享资源。"""
    closed = []

    async def shutdown() -> None:
        closed.append(True)

    app = create_mcp_http_app(_make_server(), shutdown=shutdown)
    with TestClient(app):
        assert closed == []

    assert closed == [True]


def test_args_descriptions_reach_published_parameter_schema() -> None:
    async def described_tool(limit: int = 10, keyword: str = "") -> dict[str, Any]:
        """搜索业务记录。

        Args:
            limit: 每页返回数量。
                范围由参数契约限定，不推断额外数据。
            keyword: 用户提供的商品关键词。

        Returns:
            查询结果。
        """
        return {"ok": True}

    tool = SessionFunctionTool(described_tool)
    toolset = SessionToolSet([tool], contexts=InvocationContextStore(default=_context()))
    server = create_mcp_server("test", toolset, _RecordingIdentity(), _StaticToolSet(toolset))
    published, = asyncio.run(server.list_tools())
    properties = published.input_schema["properties"]
    assert properties["limit"] == {
        "title": "Limit", "default": 10, "type": "integer",
        "description": "每页返回数量。 范围由参数契约限定，不推断额外数据。",
    }
    assert properties["keyword"]["description"] == "用户提供的商品关键词。"
    assert "Returns" not in properties["keyword"]["description"]
    assert "查询结果" not in properties["keyword"]["description"]


def test_parameter_description_fill_preserves_override_and_shared_schema() -> None:
    async def described_tool(limit: int = 10, keyword: str = "") -> dict[str, Any]:
        """搜索业务记录。

        Args:
            limit: 文档中的数量说明。
            keyword: 用户提供的关键词。
        """
        return {"ok": True}

    schema = {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10,
                      "description": "显式范围说明优先。"},
            "keyword": {"type": "string", "enum": ["甲", "乙"]},
        },
        "required": ["keyword"],
        "additionalProperties": False,
    }
    tool = SessionFunctionTool(described_tool, input_schema_override=schema)
    assert tool.input_schema["properties"]["limit"] == schema["properties"]["limit"]
    assert tool.input_schema["properties"]["keyword"] == {
        "type": "string", "enum": ["甲", "乙"], "description": "用户提供的关键词。",
    }
    assert "description" not in schema["properties"]["keyword"]
    assert tool.input_schema["required"] == ["keyword"]
    assert tool.input_schema["additionalProperties"] is False
    tool.input_schema["properties"]["keyword"]["enum"].append("丙")
    assert schema["properties"]["keyword"]["enum"] == ["甲", "乙"]


def test_no_args_section_does_not_invent_parameter_descriptions() -> None:
    tool = SessionFunctionTool(_sample_tool)
    assert "description" not in tool.input_schema["properties"]["limit"]

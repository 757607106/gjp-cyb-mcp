"""把 AgentScope ToolBase 原样导出为 MCP Tool。

AgentScope 与 MCP 共用工具名称、描述和 input_schema，避免维护两套协议定义。
MCP 请求进入时先调用 IdentityResolver；对接方应在 Resolver 中校验 JWT/OAuth
访问令牌，再返回不含敏感凭据的 InvocationContext。
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

from agentscope.message import TextBlock
from agentscope.tool import ToolBase, ToolChunk
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import request_ctx
from mcp.server.sse import SseServerTransport
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount
from starlette.routing import BaseRoute
from starlette.routing import Route

from .context import InvocationContext
from .logging_config import (
    clip_log_text,
    credential_dump_enabled,
    elapsed_ms,
    error_text,
)
from .toolset import AgentScopeToolSet

logger = logging.getLogger(__name__)

_TOOL_RESULT_WARN_BYTES = 512 * 1024


class McpIdentityResolver(Protocol):
    """MCP 宿主实现的认证入口。"""

    def resolve(
        self,
        mcp_request_context: Any,
    ) -> InvocationContext | Awaitable[InvocationContext]:
        ...


class McpToolSetResolver(Protocol):
    """按租户、账号和会话解析隔离的 ToolSet。"""

    def resolve(
        self,
        context: InvocationContext,
    ) -> AgentScopeToolSet | Awaitable[AgentScopeToolSet]:
        ...


class StaticIdentityResolver:
    """仅用于受信任的本地 STDIO 或测试，不用于多租户 HTTP 服务。"""

    def __init__(self, context: InvocationContext) -> None:
        self._context = context

    def resolve(self, _mcp_request_context: Any) -> InvocationContext:
        return self._context


class StaticToolSetResolver:
    """仅用于单用户 STDIO 或测试，多租户 HTTP 必须提供隔离实现。"""

    def __init__(self, toolset: AgentScopeToolSet) -> None:
        self._toolset = toolset

    def resolve(self, _context: InvocationContext) -> AgentScopeToolSet:
        return self._toolset


def create_mcp_server(
    name: str,
    schema_toolset: AgentScopeToolSet,
    identity_resolver: McpIdentityResolver,
    toolset_resolver: McpToolSetResolver,
) -> Server:
    """创建产品 MCP Server；身份、会话和业务 API 鉴权均由对接层注入。"""
    server = Server(
        name,
        version="1.0.0",
        instructions="业务身份由服务端认证，调用工具时不要传递账号、密码或访问令牌。",
    )
    exported_tools = {
        tool.name: tool
        for tool in schema_toolset.executable_tools()
    }

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        logger.info("MCP 工具列表 tools=%d", len(exported_tools))
        return [
            types.Tool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.input_schema,
                outputSchema=getattr(tool, "output_schema", None),
                annotations=types.ToolAnnotations(
                    readOnlyHint=tool.is_read_only,
                    destructiveHint=not tool.is_read_only,
                ),
            )
            for tool in exported_tools.values()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        request_context = request_ctx.get()
        try:
            tool = exported_tools[name]
        except KeyError as exc:
            raise ValueError("未知工具：%s" % name) from exc
        logger.info("MCP 调用开始 tool=%s", name)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "MCP 请求详情 tool=%s auth=%s args=%s",
                name,
                _masked_authorization(request_context),
                clip_log_text(json.dumps(arguments, ensure_ascii=False)),
            )
            if credential_dump_enabled():
                logger.debug(
                    "MCP 请求头 tool=%s headers=%s",
                    name,
                    json.dumps(_request_headers(request_context), ensure_ascii=False),
                )
        try:
            resolved = identity_resolver.resolve(request_context)
            context = await resolved if inspect.isawaitable(resolved) else resolved
        except Exception as exc:
            logger.warning(
                "MCP 鉴权失败 tool=%s auth=%s error=%s",
                name,
                _masked_authorization(request_context),
                error_text(exc),
            )
            raise
        resolved_toolset = toolset_resolver.resolve(context)
        runtime_toolset = (
            await resolved_toolset
            if inspect.isawaitable(resolved_toolset)
            else resolved_toolset
        )
        if not isinstance(runtime_toolset, type(schema_toolset)):
            raise TypeError(
                "运行时 ToolSet 与服务产品不一致：期望 %s，实际 %s"
                % (
                    type(schema_toolset).__name__,
                    type(runtime_toolset).__name__,
                ),
            )
        tool = runtime_toolset.get(name)
        if tool.is_external_tool:
            raise ValueError("MCP 不支持执行外部 HITL 工具：%s" % name)
        if tool.input_schema != exported_tools[name].input_schema:
            raise ValueError("运行时工具 schema 与 MCP 发布版本不一致：%s" % name)
        with runtime_toolset.bind_context(context):
            try:
                result = await _invoke_tool(
                    tool, arguments, tenant_id=context.tenant_id
                )
            except Exception as exc:
                logger.warning(
                    "MCP 调用失败 tool=%s tenant=%s session=%s elapsed=%dms error=%s",
                    name,
                    context.tenant_id,
                    context.session_id,
                    elapsed_ms(started),
                    error_text(exc),
                )
                raise
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "MCP 调用返回 tool=%s result=%s",
                name,
                clip_log_text(json.dumps(result, ensure_ascii=False)),
            )
        logger.info(
            "MCP 调用成功 tool=%s tenant=%s account=%s session=%s request=%s elapsed=%dms",
            name,
            context.tenant_id,
            context.account_id,
            context.session_id,
            context.request_id,
            elapsed_ms(started),
        )
        return result

    return server


def _masked_authorization(mcp_request_context: Any) -> str:
    """脱敏输出 Authorization 头；开启 GJP_DEBUG_DUMP_CREDENTIALS 时输出完整 token。"""
    request = getattr(mcp_request_context, "request", None)
    headers = getattr(request, "headers", None)
    value = headers.get("authorization", "") if headers is not None else ""
    if not value:
        return "<缺失>"
    scheme, _, token = value.partition(" ")
    token = token.strip()
    if not token:
        return scheme + " <空>"
    # 防御性剥离客户端可能误传的多余 Bearer 前缀
    if token[:7].casefold() == "bearer ":
        token = token[7:].strip()
    if not token:
        return scheme + " <空>"
    if credential_dump_enabled():
        return "%s %s" % (scheme, token)
    return "%s …(len=%d)" % (scheme, len(token))


def _request_headers(mcp_request_context: Any) -> dict[str, str]:
    """读取当前 MCP HTTP 请求的全部请求头，仅供开启凭据转储后的 DEBUG 日志使用。"""
    request = getattr(mcp_request_context, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return {}
    return dict(headers)


async def _invoke_tool(
    tool: ToolBase,
    arguments: dict[str, Any],
    *,
    tenant_id: str = "",
) -> dict[str, Any]:
    """执行 AgentScope 工具并把文本 JSON 还原为 MCP structuredContent。"""
    result = await tool(**arguments)
    chunks: list[ToolChunk] = []
    if isinstance(result, AsyncGenerator):
        async for chunk in result:
            chunks.append(chunk)
    else:
        chunks.append(result)
    texts = [
        block.text
        for chunk in chunks
        for block in chunk.content
        if isinstance(block, TextBlock)
    ]
    text = "".join(texts).strip()
    if not text:
        return {"ok": True}
    if len(text.encode("utf-8")) > _TOOL_RESULT_WARN_BYTES:
        logger.warning(
            "MCP 结果较大 tool=%s tenant=%s size=%dKB，可能影响模型上下文",
            getattr(tool, "name", "unknown"),
            tenant_id or "unknown",
            len(text.encode("utf-8")) // 1024,
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": True, "result": text}
    return payload if isinstance(payload, dict) else {"ok": True, "result": payload}


async def run_stdio_server(server: Server) -> None:
    """以标准 MCP STDIO 传输运行。"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def create_mcp_http_app(
    server: Server,
    streamable_path: str = "/mcp",
    sse_path: str = "/sse",
    sse_messages_path: str = "/messages/",
    extra_routes: Sequence[BaseRoute] = (),
) -> Starlette:
    """创建 MCP HTTP ASGI 应用。

    - `/mcp`：Streamable HTTP，适用于支持新版 MCP HTTP 传输的客户端。
    - `/sse`：SSE，适用于当前只支持 SSE 传输的 MCP 客户端。

    认证中间件应由部署方包在该应用外层；IdentityResolver 再把认证结果映射为
    InvocationContext，从而确保每次工具调用都使用当前账号的数据。
    """
    manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,
        json_response=True,
    )
    sse = SseServerTransport(sse_messages_path)

    class McpAsgiEndpoint:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await manager.handle_request(scope, receive, send)

    async def handle_sse(request: Request) -> Response:
        async with sse.connect_sse(
            request.scope,
            request.receive,
            request._send,  # noqa: SLF001 - MCP SDK SSE 示例要求使用 Starlette 底层 send。
        ) as streams:
            await server.run(
                streams[0],
                streams[1],
                server.create_initialization_options(),
            )
        return Response()

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with manager.run():
            yield

    return Starlette(
        routes=[
            *extra_routes,
            Route(streamable_path, endpoint=McpAsgiEndpoint()),
            Route(sse_path, endpoint=handle_sse, methods=["GET"]),
            Mount(sse_messages_path, app=sse.handle_post_message),
        ],
        lifespan=lifespan,
    )

"""把会话工具集发布为 MCP Python SDK 2.x 服务。

客户端看到稳定的 schema 工具集；每次调用再按已认证身份解析隔离的运行时
ToolSet。发布层只负责 MCP 协议、参数校验和结果映射，不改变 ERP 业务逻辑。
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Awaitable, AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

from jsonschema import ValidationError
from jsonschema.validators import validator_for
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from .context import InvocationContext
from .logging_config import clip_log_text, elapsed_ms, error_text
from .tools import SessionFunctionTool
from .toolset import SessionToolSet

logger = logging.getLogger(__name__)

_TOOL_RESULT_WARN_BYTES = 512 * 1024


def _warn_large_result(result: dict[str, Any], tool_name: str, tenant_id: str) -> None:
    """结果过大时输出警告，避免长文本淹没模型上下文。"""
    text = json.dumps(result, ensure_ascii=False)
    if len(text.encode("utf-8")) > _TOOL_RESULT_WARN_BYTES:
        logger.warning(
            "MCP 结果较大 tool=%s tenant=%s size=%dKB，可能影响模型上下文",
            tool_name,
            tenant_id or "unknown",
            len(text.encode("utf-8")) // 1024,
        )


def _snake_to_camel(name: str) -> str:
    """把 snake_case 工具名转为对外稳定的 camelCase。"""
    parts = [part for part in name.split("_") if part]
    if len(parts) <= 1:
        return name
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


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
    ) -> SessionToolSet | Awaitable[SessionToolSet]:
        ...


ResultPresenter = Callable[[str, dict[str, Any]], tuple[str, dict[str, Any]]]


class SessionMCPServer(MCPServer):
    """使用 SDK 2.x 公共覆写点发布动态会话工具。"""

    def __init__(
        self,
        name: str,
        schema_toolset: SessionToolSet,
        identity_resolver: McpIdentityResolver,
        toolset_resolver: McpToolSetResolver,
        *,
        instructions: str,
        result_presenter: ResultPresenter | None,
    ) -> None:
        super().__init__(name, instructions=instructions, version="0.1.0")
        self._schema_toolset = schema_toolset
        self._identity_resolver = identity_resolver
        self._toolset_resolver = toolset_resolver
        self._result_presenter = result_presenter
        self._exported_tools = {
            _snake_to_camel(tool.name): tool
            for tool in schema_toolset.executable_tools()
        }

    async def list_tools(self) -> list[Tool]:
        """返回领域层声明的稳定工具契约。"""
        return [
            Tool(
                name=name,
                description=tool.description,
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
                annotations=ToolAnnotations(
                    read_only_hint=tool.is_read_only,
                    destructive_hint=not tool.is_read_only,
                ),
            )
            for name, tool in self._exported_tools.items()
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context | None = None,
    ) -> CallToolResult:
        """校验协议参数、分发到会话工具并映射为标准工具结果。"""
        schema_tool = self._exported_tools.get(name)
        if schema_tool is None:
            return _error_result(
                "未知工具：%s" % name,
                {"ok": False, "error": {"code": "tool_not_found", "message": "未知工具：%s" % name}},
            )
        invalid = _validate_arguments(name, arguments, schema_tool.input_schema)
        if invalid is not None:
            return self._present(name, invalid, is_error=True)
        if context is None:
            raise RuntimeError("MCP 工具调用缺少请求上下文")
        result = await _dispatch_tool(
            name,
            schema_tool,
            schema_toolset=self._schema_toolset,
            identity_resolver=self._identity_resolver,
            toolset_resolver=self._toolset_resolver,
            arguments=arguments,
            request_context=context.request_context,
        )
        if schema_tool.output_schema is not None:
            try:
                _validate_schema(result, schema_tool.output_schema)
            except ValidationError as exc:
                logger.exception("MCP 工具输出不符合契约 tool=%s", name)
                raise RuntimeError("MCP 工具输出不符合声明的 outputSchema") from exc
        return self._present(
            name,
            result,
            is_error=isinstance(result, dict) and result.get("ok") is False,
        )

    def _present(
        self,
        tool_name: str,
        result: dict[str, Any],
        *,
        is_error: bool,
    ) -> CallToolResult:
        if self._result_presenter is None:
            text = json.dumps(result, ensure_ascii=False)
            projected = result
        else:
            text, projected = self._result_presenter(tool_name, result)
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=projected,
            is_error=is_error,
        )


def _validate_schema(value: Any, schema: dict[str, Any]) -> None:
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    validator_class(schema).validate(value)


def _validate_arguments(
    tool_name: str,
    arguments: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any] | None:
    unknown = sorted(set(arguments) - set(schema.get("properties", {})))
    if unknown:
        logger.info("MCP 参数不匹配 tool=%s unknown=%s", tool_name, unknown)
        return {
            "ok": False,
            "error": {
                "code": "tool_arguments_invalid",
                "message": "工具参数不匹配：未知参数 %s" % "、".join(unknown),
            },
        }
    try:
        _validate_schema(arguments, schema)
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path)
        location = "（%s）" % path if path else ""
        return {
            "ok": False,
            "error": {
                "code": "tool_arguments_invalid",
                "message": "Input validation error%s: %s" % (location, exc.message),
            },
        }
    return None


def _error_result(message: str, structured: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        structured_content=structured,
        is_error=True,
    )


def create_mcp_server(
    name: str,
    schema_toolset: SessionToolSet,
    identity_resolver: McpIdentityResolver,
    toolset_resolver: McpToolSetResolver,
    instructions: str = "",
    result_presenter: ResultPresenter | None = None,
) -> SessionMCPServer:
    """创建 MCP 2026-07-28 服务，业务身份与凭据仍由组合根注入。"""
    exported_tools = schema_toolset.executable_tools()
    server = SessionMCPServer(
        name,
        schema_toolset,
        identity_resolver,
        toolset_resolver,
        instructions=instructions.strip()
        or "业务身份由服务端认证，调用工具时不要传递账号、密码或访问令牌。",
        result_presenter=result_presenter,
    )
    logger.info("MCP 工具列表 tools=%d", len(exported_tools))
    return server


async def _dispatch_tool(
    exported_name: str,
    schema_tool: SessionFunctionTool,
    *,
    schema_toolset: SessionToolSet,
    identity_resolver: McpIdentityResolver,
    toolset_resolver: McpToolSetResolver,
    arguments: dict[str, Any],
    request_context: Any,
) -> dict[str, Any]:
    started = time.perf_counter()
    logger.info("MCP 调用开始 tool=%s", exported_name)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "MCP 请求详情 tool=%s auth=%s args=%s",
            exported_name,
            _masked_authorization(request_context),
            clip_log_text(json.dumps(arguments, ensure_ascii=False)),
        )
    try:
        resolved = identity_resolver.resolve(request_context)
        context = await resolved if inspect.isawaitable(resolved) else resolved
    except Exception as exc:
        logger.warning(
            "MCP 鉴权失败 tool=%s auth=%s error=%s",
            exported_name,
            _masked_authorization(request_context),
            error_text(exc),
        )
        raise
    resolved_toolset = toolset_resolver.resolve(context)
    runtime_toolset = await resolved_toolset if inspect.isawaitable(resolved_toolset) else resolved_toolset
    if not isinstance(runtime_toolset, type(schema_toolset)):
        raise TypeError(
            "运行时 ToolSet 与服务产品不一致：期望 %s，实际 %s"
            % (type(schema_toolset).__name__, type(runtime_toolset).__name__),
        )
    runtime_tool = runtime_toolset.get(schema_tool.name)
    if runtime_tool.input_schema != schema_tool.input_schema:
        raise ValueError("运行时工具 schema 与 MCP 发布版本不一致：%s" % exported_name)
    with runtime_toolset.bind_context(context):
        try:
            result = await runtime_tool.invoke_raw(**arguments)
        except Exception as exc:
            logger.warning(
                "MCP 调用失败 tool=%s tenant=%s session=%s elapsed=%dms error=%s",
                exported_name,
                context.tenant_id,
                context.session_id,
                elapsed_ms(started),
                error_text(exc),
            )
            raise
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "MCP 调用返回 tool=%s result=%s",
            exported_name,
            clip_log_text(json.dumps(result, ensure_ascii=False)),
        )
    if isinstance(result, dict):
        _warn_large_result(result, exported_name, context.tenant_id)
    logger.info(
        "MCP 调用完成 ok=%s tool=%s tenant=%s account=%s session=%s request=%s elapsed=%dms",
        result.get("ok", True) if isinstance(result, dict) else True,
        exported_name,
        context.tenant_id,
        context.account_id,
        context.session_id,
        context.request_id,
        elapsed_ms(started),
    )
    return result


def _masked_authorization(mcp_request_context: Any) -> str:
    """只输出凭据类型和长度，任何配置下都不记录凭据原文。"""
    request = getattr(mcp_request_context, "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        value = headers.get("authorization", "")
        if value:
            scheme, _, token = value.partition(" ")
            token = token.strip()
            if token[:7].casefold() == "bearer ":
                token = token[7:].strip()
            if not token:
                return scheme + " <空>"
            return "%s …(len=%d)" % (scheme, len(token))
        api_key = headers.get("x-api-key", "")
        if api_key:
            return "X-API-Key …(len=%d)" % len(api_key)
    return "<缺失>"


def create_mcp_http_app(
    server: MCPServer,
    extra_routes: Sequence[BaseRoute] = (),
    shutdown: Callable[[], Awaitable[None]] | None = None,
    *,
    allowed_hosts: Sequence[str] = (),
    allowed_origins: Sequence[str] = (),
) -> Starlette:
    """创建同时支持 2026-07-28 与旧版回退的 Streamable HTTP 应用。"""
    hosts = list(dict.fromkeys(["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", *allowed_hosts]))
    origins = list(
        dict.fromkeys(
            ["http://127.0.0.1:*", "http://localhost:*", *allowed_origins],
        )
    )
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
    )

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    app.router.routes.append(Route("/healthz", endpoint=healthz, methods=["GET"]))
    if extra_routes:
        app.router.routes.extend(extra_routes)
    if shutdown is not None:
        inner = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(starlette_app: Starlette) -> AsyncIterator[None]:
            async with inner(starlette_app):
                try:
                    yield
                finally:
                    await shutdown()

        app.router.lifespan_context = lifespan
    return app

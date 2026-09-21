"""把 SessionFunctionTool 发布为 MCP Python SDK (FastMCP) 工具。

工具按「schema 工具集 + 会话工具集」两层发布：客户端 list_tools 只见
schema 集合（用于展示契约），每次调用先经 IdentityResolver 解析
Bearer/X-API-Key 身份，再按 (tenant, account, session) 解析隔离的
ToolSet，绑定 InvocationContext 后执行同名工具，预览与幂等状态随会话
实例隔离。
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import time
from collections.abc import Awaitable, AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import request_ctx
from mcp.types import TextContent, ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from .context import InvocationContext
from .logging_config import (
    clip_log_text,
    credential_dump_enabled,
    elapsed_ms,
    error_text,
)
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
    """把 snake_case 工具名转为 camelCase 后对外发布。

    MCP 客户端（如云印 AI 平台）会把工具名规范化为 camelCase 暴露给模型，
    若服务端仍下发 snake_case，模型会在 prompt 的 snake_case 与工具列表的
    camelCase 之间混淆而调用失败。在导出层统一转为 camelCase，让两端一致。
    """
    parts = [part for part in name.split("_") if part]
    if len(parts) <= 1:
        return name
    return parts[0] + "".join(
        part[:1].upper() + part[1:] for part in parts[1:]
    )


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


def create_mcp_server(
    name: str,
    schema_toolset: SessionToolSet,
    identity_resolver: McpIdentityResolver,
    toolset_resolver: McpToolSetResolver,
    instructions: str = "",
    result_presenter: Callable[[str, dict[str, Any]], tuple[str, dict[str, Any]]] | None = None,
) -> FastMCP:
    """创建产品 MCP Server；身份、会话和业务 API 鉴权均由对接层注入。

    instructions 由业务层传入精简使用契约，随 MCP initialize 下发，
    让未单独配置 System Prompt 的客户端也能获得最低限度的使用约束。
    """
    server = FastMCP(
        name,
        instructions=instructions.strip()
        or "业务身份由服务端认证，调用工具时不要传递账号、密码或访问令牌。",
        stateless_http=True,
        json_response=True,
    )
    exported_tools = schema_toolset.executable_tools()
    for tool in exported_tools:
        _register_session_tool(
            server,
            tool,
            schema_toolset=schema_toolset,
            identity_resolver=identity_resolver,
            toolset_resolver=toolset_resolver,
        )
    _install_arguments_guard(server, exported_tools, result_presenter)
    logger.info("MCP 工具列表 tools=%d", len(exported_tools))
    return server


def _register_session_tool(
    server: FastMCP,
    tool: SessionFunctionTool,
    *,
    schema_toolset: SessionToolSet,
    identity_resolver: McpIdentityResolver,
    toolset_resolver: McpToolSetResolver,
) -> None:
    """注册薄壳工具：签名与原函数一致，运行时分发到会话工具集。

    薄壳经 functools.wraps 继承原函数签名与 docstring，FastMCP 据此生成
    参数模型；发布层用业务声明的 input/output schema 覆盖，使枚举与
    范围约束直接进入 MCP 协议。
    """
    exported_name = _snake_to_camel(tool.name)

    @functools.wraps(tool.func)
    async def shell(**kwargs: Any) -> dict[str, Any]:
        return await _dispatch_tool(
            exported_name,
            tool,
            schema_toolset=schema_toolset,
            identity_resolver=identity_resolver,
            toolset_resolver=toolset_resolver,
            arguments=kwargs,
        )

    registered = server._tool_manager.add_tool(  # noqa: SLF001 - SDK 未公开按 Tool 注册入口
        shell,
        name=exported_name,
        description=tool.description,
        annotations=ToolAnnotations(
            readOnlyHint=tool.is_read_only,
            destructiveHint=not tool.is_read_only,
        ),
    )
    registered.parameters = tool.input_schema
    if tool.output_schema is not None:
        registered.fn_metadata = registered.fn_metadata.model_copy(
            update={"output_schema": tool.output_schema},
        )


def _install_arguments_guard(
    server: FastMCP,
    tools: Sequence[SessionFunctionTool],
    result_presenter: Callable[[str, dict[str, Any]], tuple[str, dict[str, Any]]] | None = None,
) -> None:
    """重注册 lowlevel call_tool 回调，恢复参数契约校验。

    FastMCP 以 validate_input=False 注册回调，其参数模型又会静默丢弃
    未知参数：枚举、范围、必填与未知参数在协议层都不再被拦截。这里按
    SDK 默认的 validate_input=True 重新注册守卫回调——lowlevel 先按
    发布 inputSchema 执行官方 jsonschema 校验（违规返回 isError=True
    的 "Input validation error: ..."），守卫再拦截未知参数名，返回
    结构化 tool_arguments_invalid 供模型自行纠正，其余交回 FastMCP
    原回调执行。
    """
    schemas = {_snake_to_camel(tool.name): tool.input_schema for tool in tools}
    fastmcp_call_tool = server.call_tool

    @server._mcp_server.call_tool()  # noqa: SLF001 - SDK 未公开覆盖回调注册入口
    async def guarded_call_tool(name: str, arguments: dict[str, Any]) -> Any:
        schema = schemas.get(name)
        if schema is not None:
            unknown = sorted(set(arguments) - set(schema.get("properties", {})))
            if unknown:
                logger.info("MCP 参数不匹配 tool=%s unknown=%s", name, unknown)
                invalid_result = {
                    "ok": False,
                    "error": {
                        "code": "tool_arguments_invalid",
                        "message": "工具参数不匹配：未知参数 %s" % "、".join(unknown),
                    },
                }
                return _present_tool_result(
                    name,
                    invalid_result,
                    result_presenter,
                )
        result = await fastmcp_call_tool(name, arguments)
        return _present_tool_result(name, result, result_presenter)


def _present_tool_result(
    tool_name: str,
    result: Any,
    presenter: Callable[[str, dict[str, Any]], tuple[str, dict[str, Any]]] | None,
) -> Any:
    if presenter is None:
        return result

    structured = result
    if isinstance(result, tuple) and len(result) == 2:
        structured = result[1]
    if not isinstance(structured, dict):
        return result

    text, projected = presenter(tool_name, structured)
    return [TextContent(type="text", text=text)], projected


async def _dispatch_tool(
    exported_name: str,
    schema_tool: SessionFunctionTool,
    *,
    schema_toolset: SessionToolSet,
    identity_resolver: McpIdentityResolver,
    toolset_resolver: McpToolSetResolver,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    request_context = request_ctx.get()
    logger.info("MCP 调用开始 tool=%s", exported_name)
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "MCP 请求详情 tool=%s auth=%s args=%s",
            exported_name,
            _masked_authorization(request_context),
            clip_log_text(json.dumps(arguments, ensure_ascii=False)),
        )
        if credential_dump_enabled():
            logger.debug(
                "MCP 请求头 tool=%s headers=%s",
                exported_name,
                json.dumps(_request_headers(request_context), ensure_ascii=False),
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
    runtime_tool = runtime_toolset.get(schema_tool.name)
    if runtime_tool.input_schema != schema_tool.input_schema:
        raise ValueError(
            "运行时工具 schema 与 MCP 发布版本不一致：%s" % exported_name,
        )
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
    """脱敏输出 Authorization 或 X-API-Key 头。

    开启 GJP_DEBUG_DUMP_CREDENTIALS 时输出完整凭据；否则只输出前缀与长度。
    """
    request = getattr(mcp_request_context, "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        value = headers.get("authorization", "")
        if value:
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
        api_key = headers.get("x-api-key", "")
        if api_key:
            if credential_dump_enabled():
                return "X-API-Key %s" % api_key
            return "X-API-Key …(len=%d)" % len(api_key)
    return "<缺失>"


def _request_headers(mcp_request_context: Any) -> dict[str, str]:
    """读取当前 MCP HTTP 请求的全部请求头，仅供开启凭据转储后的 DEBUG 日志使用。"""
    request = getattr(mcp_request_context, "request", None)
    headers = getattr(request, "headers", None)
    if headers is None:
        return {}
    return dict(headers)


def create_mcp_http_app(
    server: FastMCP,
    extra_routes: Sequence[BaseRoute] = (),
    shutdown: Callable[[], Awaitable[None]] | None = None,
) -> Starlette:
    """创建 MCP Streamable HTTP ASGI 应用。

    - `/mcp`：Streamable HTTP，适用于支持新版 MCP HTTP 传输的客户端。
    - `/healthz`：无鉴权探活端点，供负载均衡和监控使用。
    - extra_routes：业务附加路由（如 WorkBuddy OAuth 回调）。

    认证中间件应由部署方包在该应用外层；IdentityResolver 再把认证结果映射为
    InvocationContext，从而确保每次工具调用都使用当前账号的数据。
    """
    app = server.streamable_http_app()

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

"""本地 CLI Agent 链路的调试日志中间件。

仅在 GJP_LOG_LEVEL=DEBUG 时输出工具调用名称、耗时和状态；
当 GJP_LOG_CONTEXT=true 时额外记录工具入参 JSON 和返回值内容。
生产 MCP 服务不装配 Agent，本中间件只随 gjp_cli 本地验证链路使用。
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable, Union

from agentscope.middleware import MiddlewareBase
from agentscope.message import ToolCallBlock

from gjp_common.logging_config import context_logging_enabled

if TYPE_CHECKING:
    from agentscope.agent import Agent
    from agentscope.model import ChatResponse

logger = logging.getLogger(__name__)

_LOG_TRUNCATE_LIMIT = 2000


def should_register_debug_middleware() -> bool:
    """仅在 DEBUG 级别时返回 True，用于条件化注册调试中间件。

    生产环境默认 INFO 级别，此函数返回 False，中间件不会被实例化。
    """
    return logging.getLogger("gjp_common").isEnabledFor(logging.DEBUG)


def _truncate(text: str, limit: int = _LOG_TRUNCATE_LIMIT) -> str:
    """截断过长文本，保留前后部分并用省略号标记。"""
    if len(text) <= limit:
        return text
    half = limit // 2
    return (
        text[:half]
        + "…（已截断 %d 字符）…" % (len(text) - limit)
        + text[-half:]
    )


def _extract_tool_output(result: Any) -> str:
    """从 ToolResponse 或 ToolChunk 中提取可读的输出文本。"""
    content = getattr(result, "content", None)
    if not isinstance(content, list):
        return str(result) if result is not None else ""
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(str(block))
    return "\n".join(parts)


class ToolLoggingMiddleware(MiddlewareBase):
    """工具调用与模型调用调试日志中间件。

    日志级别策略：
    - DEBUG 级别记录工具调用名称、耗时和状态（需 GJP_LOG_LEVEL=DEBUG）
    - 当 context_logging_enabled() 为 True 时（GJP_LOG_CONTEXT=true），
      额外记录工具入参 JSON 和返回值内容
    - 生产环境默认 INFO 级别，此中间件无任何输出
    """

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        tool_call = input_kwargs.get("tool_call")
        if not isinstance(tool_call, ToolCallBlock):
            async for item in next_handler(**input_kwargs):
                yield item
            return

        tool_name = tool_call.name
        tool_input = tool_call.input
        started = time.perf_counter()

        logger.debug("工具调用开始 tool=%s id=%s", tool_name, tool_call.id)
        if context_logging_enabled() and tool_input:
            logger.debug(
                "工具入参 tool=%s args=%s",
                tool_name,
                _truncate(tool_input),
            )

        last_item = None
        try:
            async for item in next_handler(**input_kwargs):
                last_item = item
                yield item
        except BaseException as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.debug(
                "工具调用异常 tool=%s duration_ms=%d error=%s",
                tool_name,
                duration_ms,
                _truncate(str(exc)),
            )
            raise

        duration_ms = int((time.perf_counter() - started) * 1000)
        state = getattr(last_item, "state", "unknown") if last_item else "unknown"
        logger.debug(
            "工具调用完成 tool=%s state=%s duration_ms=%d",
            tool_name,
            state,
            duration_ms,
        )
        if context_logging_enabled() and last_item is not None:
            output_text = _extract_tool_output(last_item)
            if output_text:
                logger.debug(
                    "工具返回 tool=%s result=%s",
                    tool_name,
                    _truncate(output_text),
                )

    async def on_model_call(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[
            ...,
            Awaitable[
                Union["ChatResponse", AsyncGenerator["ChatResponse", None]]
            ],
        ],
    ) -> Union["ChatResponse", AsyncGenerator["ChatResponse", None]]:
        from agentscope.model import ChatModelBase

        model = input_kwargs.get("current_model")
        model_name = (
            getattr(model, "model", "unknown")
            if isinstance(model, ChatModelBase)
            else "unknown"
        )

        started = time.perf_counter()
        logger.debug("模型调用开始 model=%s", model_name)

        result = await next_handler(**input_kwargs)

        if inspect.isasyncgen(result):
            return self._wrap_model_stream(result, model_name, started)

        duration_ms = int((time.perf_counter() - started) * 1000)
        self._log_model_response(model_name, duration_ms, result)
        return result

    async def _wrap_model_stream(
        self,
        gen: AsyncGenerator["ChatResponse", None],
        model_name: str,
        started: float,
    ) -> AsyncGenerator["ChatResponse", None]:
        """包装流式模型调用，在结束时记录耗时和 token 用量。"""
        last_chunk = None
        try:
            async for chunk in gen:
                last_chunk = chunk
                yield chunk
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            self._log_model_response(model_name, duration_ms, last_chunk)

    @staticmethod
    def _log_model_response(
        model_name: str,
        duration_ms: int,
        response: Any,
    ) -> None:
        """记录模型调用的耗时和 token 用量。"""
        usage = getattr(response, "usage", None)
        if usage:
            logger.debug(
                "模型调用完成 model=%s duration_ms=%d input_tokens=%s output_tokens=%s",
                model_name,
                duration_ms,
                getattr(usage, "input_tokens", "?"),
                getattr(usage, "output_tokens", "?"),
            )
        else:
            logger.debug(
                "模型调用完成 model=%s duration_ms=%d",
                model_name,
                duration_ms,
            )

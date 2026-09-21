"""会话工具集合：以 SessionFunctionTool 为唯一能力描述。"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Any

from .context import InvocationContext, InvocationContextStore
from .errors import DomainError
from .tools import SessionFunctionTool


class SessionToolSet:
    """工具集合与 MCP 发布白名单；调用前绑定当前调用上下文。"""

    def __init__(
        self,
        tools: Iterable[SessionFunctionTool],
        contexts: InvocationContextStore,
        mcp_tool_names: Iterable[str] | None = None,
    ) -> None:
        self._tools = tuple(tools)
        self._contexts = contexts
        names = [tool.name for tool in self._tools]
        if len(names) != len(set(names)):
            raise ValueError("工具名称不能重复")
        self._by_name = {tool.name: tool for tool in self._tools}
        self._mcp_tool_names = (
            frozenset(mcp_tool_names)
            if mcp_tool_names is not None
            else frozenset(names)
        )
        unknown = self._mcp_tool_names.difference(names)
        if unknown:
            raise ValueError("工具白名单包含未知工具：%s" % "、".join(sorted(unknown)))

    def tools(self) -> list[SessionFunctionTool]:
        """全部工具，按注册顺序返回。"""
        return list(self._tools)

    def executable_tools(self) -> list[SessionFunctionTool]:
        """MCP 只导出白名单工具。"""
        return [
            tool for tool in self._tools if tool.name in self._mcp_tool_names
        ]

    def get(self, name: str) -> SessionFunctionTool:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise KeyError("未知工具：%s" % name) from exc

    def bind_context(
        self,
        context: InvocationContext,
    ) -> AbstractContextManager[None]:
        """把已认证身份只绑定到本次 ToolSet 调用。"""
        return self._contexts.bind(context)

    @staticmethod
    def ok_response(**payload: Any) -> dict[str, Any]:
        """构造工具成功响应。"""
        return {"ok": True, **payload}

    @staticmethod
    def error_response(error: DomainError) -> dict[str, Any]:
        """构造工具错误响应。"""
        return {"ok": False, "error": error.as_dict()}

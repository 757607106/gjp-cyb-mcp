"""AgentScope 2.0.5 标准工具集合。"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Any

from agentscope.tool import ToolBase

from .context import InvocationContext, InvocationContextStore
from .errors import DomainError


class AgentScopeToolSet:
    """以 ToolBase 为唯一能力描述，Agent 和 MCP 共用同一份 schema。"""

    def __init__(
        self,
        tools: Iterable[ToolBase],
        contexts: InvocationContextStore,
        agent_tool_names: Iterable[str] | None = None,
        mcp_tool_names: Iterable[str] | None = None,
    ) -> None:
        self._tools = tuple(tools)
        self._contexts = contexts
        names = [tool.name for tool in self._tools]
        if len(names) != len(set(names)):
            raise ValueError("工具名称不能重复")
        self._by_name = {tool.name: tool for tool in self._tools}
        self._agent_tool_names = (
            frozenset(agent_tool_names)
            if agent_tool_names is not None
            else frozenset(names)
        )
        self._mcp_tool_names = (
            frozenset(mcp_tool_names)
            if mcp_tool_names is not None
            else frozenset(names)
        )
        unknown = (self._agent_tool_names | self._mcp_tool_names).difference(names)
        if unknown:
            raise ValueError("工具白名单包含未知工具：%s" % "、".join(sorted(unknown)))

    def tools(self) -> list[ToolBase]:
        """供生产 AgentScope Agent 绑定，不包含宿主文件工具。"""
        return [
            tool
            for tool in self._tools
            if tool.name in self._agent_tool_names
        ]

    def local_tools(self) -> list[ToolBase]:
        """完整工具集，包含未对 MCP 发布的宿主工具。"""
        return list(self._tools)

    def executable_tools(self) -> list[ToolBase]:
        """MCP 只导出白名单工具，HITL 与主机文件工具留给 Agent 宿主。"""
        return [
            tool
            for tool in self._tools
            if not tool.is_external_tool and tool.name in self._mcp_tool_names
        ]

    def get(self, name: str) -> ToolBase:
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
        return {"ok": False, "error": {"code": error.code, "message": error.message}}

"""通用工具基类：AgentScope 工具包装器。

本文件只包含所有业务 Agent 共用的工具基础设施，不包含任何业务特定逻辑。
业务特定的工具入参 schema 放在各自业务模块的 tools.py 中。
"""

import inspect
from collections.abc import Callable
from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool


class SessionFunctionTool(FunctionTool):
    """领域工具基类：本地会话内免二次确认。

    Agent 只能修改配置的内存草稿和输出路径，用户意图已在触发工具调用的
    消息中表达，因此不需要 AgentScope 通用函数工具的二次审批。
    output_schema 仅供 MCP 发布工具输出契约，不进入 AgentScope 运行时。
    """

    def __init__(
        self,
        func: Callable,
        *,
        output_schema: dict[str, Any] | None = None,
        input_schema_override: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(func, **kwargs)
        self.output_schema = output_schema
        if input_schema_override is not None:
            self.input_schema = input_schema_override

    async def check_permissions(self, *_args: Any, **_kwargs: Any) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Allowed within the local session.",
        )

    async def invoke_raw(self, **kwargs: Any) -> Any:
        """直接执行被包装函数并返回原始结果，跳过 ToolChunk 序列化。

        MCP 层用此方法拿到原始 dict，避免 dict→JSON text→TextBlock→json.loads
        的脆弱往返。AgentScope Agent 仍走标准 ``__call__`` 路径。
        """
        if inspect.iscoroutinefunction(self._func):
            return await self._func(**kwargs)
        return self._func(**kwargs)

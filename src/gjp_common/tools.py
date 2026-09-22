"""通用工具基类：基于函数签名与 JSON Schema 的会话工具。

不依赖任何 Agent 框架；MCP 发布层（gjp_common.mcp）直接消费此类，
同一份函数签名生成输入 Schema，同一份函数体服务运行时调用。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from mcp.server.mcpserver.utilities.func_metadata import func_metadata


def _parameter_descriptions(docstring: str) -> dict[str, str]:
    # SDK 不会把 Google 风格 Args 自动写入参数 Schema。
    _, _, arguments = docstring.partition("\nArgs:\n")
    descriptions: dict[str, str] = {}
    current = ""
    for line in arguments.splitlines():
        if not line.strip():
            continue
        if not line.startswith("    "):
            break
        if not line.startswith("        "):
            name, separator, description = line.strip().partition(":")
            current = name if separator and name.isidentifier() else ""
            if current:
                descriptions[current] = description.strip()
        elif current:
            descriptions[current] += " " + line.strip()
    return descriptions


def _close_input_objects(schema: dict[str, Any], *, root: bool = False) -> None:
    """关闭输入对象的未知字段，同时保留显式声明的开放对象。"""
    is_object = schema.get("type") == "object"
    if is_object and (root or "properties" in schema):
        schema.setdefault("additionalProperties", False)
    for value in schema.values():
        if isinstance(value, dict):
            _close_input_objects(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _close_input_objects(item)


class SessionFunctionTool:
    """领域工具：函数 + 可覆盖的输入/输出 JSON Schema 与读写注解。

    output_schema 仅供 MCP 发布工具输出契约；input_schema 缺省从函数
    签名与 docstring 生成，需要枚举和范围约束时用
    input_schema_override 整体替换。确认由对接平台和业务工具契约承接：
    工具可能写入真实 ERP，平台负责用户确认，工具校验业务确认参数。
    """

    def __init__(
        self,
        func: Callable,
        *,
        output_schema: dict[str, Any] | None = None,
        input_schema_override: dict[str, Any] | None = None,
        is_read_only: bool = False,
        name: str | None = None,
        title: str | None = None,
        destructive_hint: bool | None = None,
        idempotent_hint: bool | None = None,
        open_world_hint: bool | None = None,
    ) -> None:
        self._func = func
        self.name = name or func.__name__
        docstring = inspect.getdoc(func) or ""
        # Args 已进入 inputSchema，不在 description 中重复占用模型上下文。
        self.description = docstring.partition("\nArgs:\n")[0].strip()
        self.title = title
        self.output_schema = output_schema
        self.is_read_only = is_read_only
        self.destructive_hint = destructive_hint
        self.idempotent_hint = idempotent_hint
        self.open_world_hint = open_world_hint
        metadata = func_metadata(func)
        self.input_schema = (
            deepcopy(input_schema_override)
            if input_schema_override is not None
            else metadata.arg_model.model_json_schema(by_alias=True)
        )
        _close_input_objects(self.input_schema, root=True)
        descriptions = _parameter_descriptions(docstring)
        for name, parameter in self.input_schema.get("properties", {}).items():
            if descriptions.get(name) and not parameter.get("description"):
                parameter["description"] = descriptions[name]

    @property
    def func(self) -> Callable:
        """被包装的领域函数，供 MCP 发布层生成签名一致的薄壳。"""
        return self._func

    async def invoke_raw(self, **kwargs: Any) -> Any:
        """直接执行被包装函数并返回原始结果。

        MCP 层用此方法拿到原始 dict，避免 dict→JSON text→ContentBlock→
        json.loads 的脆弱往返。
        """
        if inspect.iscoroutinefunction(self._func):
            return await self._func(**kwargs)
        return self._func(**kwargs)

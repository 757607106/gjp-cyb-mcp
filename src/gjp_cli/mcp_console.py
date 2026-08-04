"""通过 AgentScope MCPClient 对话式测试远程 MCP 服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentscope.mcp import HttpMCPConfig, MCPClient
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionRule,
)
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from erp_billing.toolset import BILLING_MCP_TOOL_NAMES
from gjp_common.config import get_env_value
from .agent import build_agent_from_toolkit
from .model_runtime import LLMSettings
from .interactive import (
    InteractiveAgentConsole,
    resolve_agent_console_registration,
)


_DEFAULT_MCP_URLS = {
    "erp-billing": "http://127.0.0.1:8102/mcp",
}


@dataclass
class RemoteMcpSession:
    """只负责驱动 CLI 对话循环，业务会话状态在 MCP 服务端。"""

    finished: bool = False
    current_draft: Any = None
    user_turns: int = 0

    def record_user_turn(self, _text: str) -> None:
        self.user_turns += 1


def build_mcp_agent_console(
    *,
    agent_key: str,
    mcp_url: str,
    bearer_token: str,
) -> InteractiveAgentConsole:
    """构建通过远程 MCP 工具工作的 AgentScope 对话控制台。

    Agent 规格、展示 profile、输入转换器和 MCP 服务名均来自统一注册表，
    装配参数与本地 chat 链路共用 agent.build_agent_from_toolkit。
    """
    registration = resolve_agent_console_registration(agent_key)
    settings = LLMSettings.from_env()
    mcp_client = MCPClient(
        name=registration.mcp_name,
        is_stateful=False,
        mcp_config=HttpMCPConfig(
            url=mcp_url,
            headers={"Authorization": "Bearer " + bearer_token},
        ),
    )
    agent = build_agent_from_toolkit(
        Toolkit(mcps=[mcp_client]),
        settings,
        registration.agent_spec,
        state=build_product_mcp_agent_state(registration.key, registration.mcp_name),
    )
    return InteractiveAgentConsole(
        agent=agent,
        session=RemoteMcpSession(),
        model_label="%s / %s" % (settings.provider, settings.model_name),
        stream_enabled=settings.stream,
        profile=registration.profile,
        user_input_transformer=registration.user_input_transformer,
    )


def build_product_mcp_agent_state(
    agent_key: str,
    mcp_name: str,
) -> AgentState:
    """只对当前产品公开的已认证 MCP 工具关闭客户端二次确认。

    MCP 请求仍携带 Bearer，服务端仍负责校验用户身份、scope 和会话隔离。
    这里仅替代 AgentScope 对非只读 MCP 工具的默认 ASK 行为，不开启全局
    BYPASS，避免未来新增的未知工具被自动放行。
    """
    normalize_agent_key(agent_key)
    allow_rules: dict[str, list[PermissionRule]] = {}
    for tool_name in BILLING_MCP_TOOL_NAMES:
        model_tool_name = "mcp__%s__%s" % (mcp_name, tool_name)
        allow_rules[model_tool_name] = [
            PermissionRule(
                tool_name=model_tool_name,
                rule_content=None,
                behavior=PermissionBehavior.ALLOW,
                source="erp-billing-authenticated-mcp",
            ),
        ]
    return AgentState(
        permission_context=PermissionContext(allow_rules=allow_rules),
    )


def normalize_agent_key(agent_key: str) -> str:
    """把产品别名归一化为注册表 key；未知 agent 报 AGENT_NOT_FOUND。"""
    return resolve_agent_console_registration(agent_key).key


def default_mcp_url(agent_key: str) -> str:
    normalized = normalize_agent_key(agent_key)
    return get_env_value("BILLING_MCP_URL", _DEFAULT_MCP_URLS[normalized]).strip()

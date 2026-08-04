"""本地 CLI 的 AgentScope Agent 装配工厂与产品 Agent 规格。

生产链路由各 AI 平台自带模型并通过 MCP 绑定工具；本模块仅供 gjp_cli
在本地模拟 SaaS 对话时装配 Agent，不进入生产部署。
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.middleware import TracingMiddleware
from agentscope.state import AgentState
from agentscope.tool import Toolkit

from erp_billing.prompt import ERP_BILLING_SYSTEM_PROMPT
from .middleware import should_register_debug_middleware, ToolLoggingMiddleware
from .model_runtime import LLMSettings, build_chat_model

logger = logging.getLogger(__name__)


class ToolProvider(Protocol):
    """Agent 只依赖标准工具提供者，不再要求工具必须定义在 Session 上。"""

    def tools(self) -> list[Any]:
        ...


@dataclass(frozen=True)
class AgentSpec:
    """声明一个业务 Agent 的最小元信息。"""

    name: str
    system_prompt: str
    max_iters: int = 12


ERP_BILLING_AGENT_SPEC = AgentSpec(
    name="ErpBillingAgent",
    system_prompt=ERP_BILLING_SYSTEM_PROMPT,
    max_iters=15,
)


def build_agentscope_model(settings: LLMSettings):
    """通过 AgentScope 模型注册表创建当前配置的模型。"""
    return build_chat_model(settings)


def load_agent_state(state_path: Path) -> Optional[AgentState]:
    """从 JSON 文件恢复 AgentState；文件不存在或解析失败时返回 None。"""
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return AgentState.model_validate(data)
    except Exception as exc:
        logger.warning("会话状态文件解析失败，将以全新会话启动: %s", exc)
        return None


def save_agent_state(agent: Agent, state_path: Path) -> None:
    """保存当前 AgentState，供下次 --resume 恢复上下文。"""
    try:
        state_dict = agent.state.model_dump()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(state_dict, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        logger.info("会话状态已保存 path=%s", state_path)
    except Exception as exc:
        logger.warning("会话状态保存失败: %s", exc)


def build_agent_from_toolkit(
    toolkit: Toolkit,
    settings: LLMSettings,
    spec: AgentSpec,
    state: Optional[AgentState] = None,
) -> Agent:
    """统一的 Agent 装配入口：本地 ToolSet 与远程 MCP 工具共用同一份装配参数。"""
    model = build_chat_model(settings)
    effective_context = getattr(model, "context_size", None) or settings.context_size
    tool_result_limit = (
        max(2000, int(effective_context * 0.08))
        if effective_context
        else 4000
    )
    middlewares: list = [TracingMiddleware()]
    if should_register_debug_middleware():
        middlewares.append(ToolLoggingMiddleware())
    return Agent(
        name=spec.name,
        system_prompt=spec.system_prompt,
        model=model,
        toolkit=toolkit,
        middlewares=middlewares,
        state=state,
        context_config=ContextConfig(
            trigger_ratio=0.8,
            reserve_ratio=0.2,
            tool_result_limit=tool_result_limit,
        ),
        react_config=ReActConfig(max_iters=spec.max_iters),
    )


def build_agent(
    tool_provider: ToolProvider,
    settings: LLMSettings,
    spec: AgentSpec,
    state: Optional[AgentState] = None,
) -> Agent:
    """根据 AgentSpec 和标准 ToolSet 构建一个 AgentScope Agent。"""
    return build_agent_from_toolkit(
        Toolkit(tools=tool_provider.tools()),
        settings,
        spec,
        state=state,
    )

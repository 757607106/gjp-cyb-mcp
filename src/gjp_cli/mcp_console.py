"""通过 AgentScope MCPClient 对话式测试远程 MCP 服务。"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
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
from gjp_common.errors import DomainError
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


def request_validation_bearer_from_upstream_token(
    *,
    mcp_url: str,
    upstream_token: str,
    token_url: str = "",
    metadata: dict[str, Any] | None = None,
    timeout_seconds: float = 30,
) -> str:
    """仅供 CLI 测试：把已有 ERP Token 登记为临时 MCP Bearer。"""
    token = _raw_bearer_token(upstream_token)
    if not token:
        raise DomainError("MCP_CHAT_AUTH_REQUIRED", "缺少 ERP 上游 Token")
    target = token_url.strip() or validation_token_url_from_mcp_url(mcp_url)
    payload = dict(metadata or {})
    payload["upstreamToken"] = token
    return _post_validation_auth(target, payload, timeout_seconds)


def _raw_bearer_token(value: str) -> str:
    """兼容环境变量中的纯 JWT 或 `Bearer <JWT>` 写法。"""
    token = value.strip()
    scheme, separator, credential = token.partition(" ")
    if separator and scheme.casefold() == "bearer":
        token = credential.strip()
    return token


def _post_validation_auth(
    target: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> str:
    """向 CLI 测试鉴权端点提交凭据并读取独立 MCP Bearer。"""
    request = urllib.request.Request(
        target,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = response.status
            body = response.read().decode("utf-8-sig")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise DomainError(
            "MCP_CHAT_LOGIN_FAILED",
            "测试鉴权失败 HTTP %s：%s" % (exc.code, _error_message(body)),
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DomainError(
            "MCP_CHAT_LOGIN_FAILED",
            _unavailable_token_message(target, exc),
        ) from exc
    if status < 200 or status >= 300:
        raise DomainError("MCP_CHAT_LOGIN_FAILED", "测试鉴权失败 HTTP %s" % status)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DomainError("MCP_CHAT_LOGIN_FAILED", "Token 登记接口返回的不是 JSON") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise DomainError("MCP_CHAT_LOGIN_FAILED", _error_message(body))
    token = payload.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        raise DomainError("MCP_CHAT_LOGIN_FAILED", "Token 登记成功但未返回 accessToken")
    return token.strip()


def validation_token_url_from_mcp_url(mcp_url: str) -> str:
    """从 MCP URL 推导仅供 CLI 使用的上游 Token 登记地址。"""
    return _validation_auth_url_from_mcp_url(mcp_url, "token")


def _validation_auth_url_from_mcp_url(mcp_url: str, endpoint: str) -> str:
    parsed = urllib.parse.urlparse(mcp_url)
    if not parsed.scheme or not parsed.netloc:
        raise DomainError("MCP_CHAT_CONFIG_INVALID", "MCP URL 必须是 http:// 或 https:// 地址")
    path = parsed.path.rstrip("/")
    if path.endswith("/mcp"):
        path = path[: -len("/mcp")]
    auth_path = (path.rstrip("/") + "/test-auth/" + endpoint) if path else "/test-auth/" + endpoint
    return urllib.parse.urlunparse(parsed._replace(path=auth_path, query="", fragment=""))


def _unavailable_token_message(target: str, exc: BaseException) -> str:
    message = "Token 登记接口不可用或超时：%s" % _safe_url_for_message(target)
    reason = _connection_error_reason(exc)
    if reason:
        message += "；原因：%s" % reason
    hint = _validation_service_hint(target)
    if hint:
        message += "；请先启动测试服务：%s" % hint
    else:
        message += "；请确认测试验证服务已部署并暴露 /test-auth/token，或通过 --token-url 指定地址"
    return message


def _safe_url_for_message(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url.split("?", 1)[0].split("#", 1)[0]
    host = parsed.hostname or ""
    if parsed.port:
        host = "%s:%s" % (host, parsed.port)
    return urllib.parse.urlunparse(
        parsed._replace(netloc=host, query="", fragment=""),
    )


def _connection_error_reason(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    return str(reason or exc).strip()


def _validation_service_hint(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return ""
    if parsed.port == 8102:
        return "uv run uvicorn gjp_cli.billing_validation:app --host 0.0.0.0 --port 8102"
    return ""


def _error_message(body: str) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body[:300] or "Token 登记失败"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "Token 登记失败")
    return str(payload.get("message") or "Token 登记失败") if isinstance(payload, dict) else "Token 登记失败"

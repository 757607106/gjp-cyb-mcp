"""开单能力独立 MCP 服务，只发布 BillingToolSet。"""

from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.routing import BaseRoute

from gjp_common.config import get_env_value
from gjp_common.mcp import (
    McpIdentityResolver,
    McpToolSetResolver,
    create_mcp_http_app,
    create_mcp_server,
)
from .prompt import ERP_BILLING_MCP_INSTRUCTIONS
from .presentation import present_billing_result
from .toolset import BillingToolSet


def mcp_transport_allowlists(public_base_url: str = "") -> tuple[list[str], list[str]]:
    """读取显式传输白名单，并补入已配置的公网服务地址。"""
    hosts = [
        item.strip()
        for item in get_env_value("GJP_MCP_ALLOWED_HOSTS").split(",")
        if item.strip()
    ]
    origins = [
        item.strip()
        for item in get_env_value("GJP_MCP_ALLOWED_ORIGINS").split(",")
        if item.strip()
    ]
    if public_base_url.strip():
        parsed = urlsplit(public_base_url.strip())
        if parsed.scheme and parsed.netloc:
            hosts.append(parsed.netloc)
            origins.append("%s://%s" % (parsed.scheme, parsed.netloc))
    return list(dict.fromkeys(hosts)), list(dict.fromkeys(origins))


def create_billing_mcp_service(
    schema_toolset: BillingToolSet,
    identity_resolver: McpIdentityResolver,
    toolset_resolver: McpToolSetResolver,
    extra_routes: Sequence[BaseRoute] = (),
    shutdown: Callable[[], Awaitable[None]] | None = None,
    allowed_hosts: Sequence[str] = (),
    allowed_origins: Sequence[str] = (),
) -> Starlette:
    """创建开单 MCP 服务；部署时使用独立域名、进程和认证配置。"""
    if not isinstance(schema_toolset, BillingToolSet):
        raise TypeError("开单服务只能发布 BillingToolSet")
    server = create_mcp_server(
        "erp-billing",
        schema_toolset,
        identity_resolver,
        toolset_resolver,
        instructions=ERP_BILLING_MCP_INSTRUCTIONS,
        result_presenter=present_billing_result,
    )
    return create_mcp_http_app(
        server,
        extra_routes=extra_routes,
        shutdown=shutdown,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )

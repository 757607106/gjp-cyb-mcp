"""开单能力独立 MCP 服务，只发布 BillingToolSet。"""

from collections.abc import Sequence

from starlette.applications import Starlette
from starlette.routing import BaseRoute

from gjp_common.mcp import (
    McpIdentityResolver,
    McpToolSetResolver,
    create_mcp_http_app,
    create_mcp_server,
)
from .prompt import ERP_BILLING_MCP_INSTRUCTIONS
from .toolset import BillingToolSet


def create_billing_mcp_service(
    schema_toolset: BillingToolSet,
    identity_resolver: McpIdentityResolver,
    toolset_resolver: McpToolSetResolver,
    extra_routes: Sequence[BaseRoute] = (),
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
    )
    return create_mcp_http_app(server, extra_routes=extra_routes)

"""验证开单 MCP 对外发布的工具名是 camelCase，防止 snake_case 回退。

gjp_common.mcp 在导出层把 snake_case 工具名统一转为 camelCase；
BILLING_MCP_TOOL_NAMES 是开单工具的完整白名单，经转换后必须等于
模型在 prompt 与客户端工具列表中看到的 camelCase 集合。最后通过
create_mcp_server 的 ListToolsRequest handler 端到端验证导出层行为。
"""

from __future__ import annotations

import asyncio
import json

from mcp import types

from erp_billing.adapters import UnavailableBillingApi
from erp_billing.config import ErpBillingSettings
from erp_billing.session import ErpBillingSession
from erp_billing.toolset import BILLING_MCP_TOOL_NAMES, BillingToolSet
from gjp_common.context import InvocationContext, InvocationContextStore
from gjp_common.mcp import (
    StaticIdentityResolver,
    StaticToolSetResolver,
    _snake_to_camel,
    create_mcp_server,
)

_EXPECTED_CAMEL = frozenset(
    {
        "syncProducts",
        "listProducts",
        "searchProducts",
        "searchBillingReferences",
        "previewSalesOrder",
        "submitSalesOrder",
        "getSalesOrder",
        "listSalesOrders",
        "voidSalesOrder",
        "updateSalesOrder",
    }
)


def test_billing_mcp_tool_names_all_map_to_camelcase() -> None:
    """开单白名单内每个工具名都能被导出层转为 camelCase。"""
    actual = {_snake_to_camel(name) for name in BILLING_MCP_TOOL_NAMES}
    assert actual == _EXPECTED_CAMEL


def test_billing_mcp_tool_names_remain_snake_case_internally() -> None:
    """Python 函数名（tool.name）保持 snake_case，转换只在导出层发生。"""
    for name in BILLING_MCP_TOOL_NAMES:
        assert "_" in name, "内部工具名应保持 snake_case：%s" % name


def _make_billing_toolset(tmp_path) -> BillingToolSet:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {"products": [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session = ErpBillingSession.from_settings(
        ErpBillingSettings(
            product_catalog_path=catalog,
            alias_path=None,
            recommendation_score=0.60,
            use_default_fresh_aliases=True,
            category_path=None,
            use_default_categories=True,
        ),
    )
    context = InvocationContext(
        tenant_id="tenant-test",
        subject_id="user-test",
        account_id="billing-test",
        session_id="session-test",
        scopes=frozenset({"billing:read", "billing:write"}),
    )
    return BillingToolSet(
        session,
        UnavailableBillingApi(),
        InvocationContextStore(default=context),
    )


def test_create_mcp_server_lists_tools_in_camelcase(tmp_path) -> None:
    """create_mcp_server 的 list_tools 必须下发 camelCase 工具名。

    直接调用 MCP Server 注册的 ListToolsRequest handler，端到端验证
    导出层不再下发 snake_case，避免模型在 prompt 与工具列表间混淆。
    """
    toolset = _make_billing_toolset(tmp_path)
    context = InvocationContext(
        tenant_id="tenant-test",
        subject_id="user-test",
        account_id="billing-test",
        session_id="session-test",
        scopes=frozenset({"billing:read", "billing:write"}),
    )
    server = create_mcp_server(
        "erp-billing",
        toolset,
        StaticIdentityResolver(context),
        StaticToolSetResolver(toolset),
    )
    handler = server.request_handlers[types.ListToolsRequest]
    result = asyncio.run(handler(None))
    names = {tool.name for tool in result.root.tools}
    assert names == _EXPECTED_CAMEL

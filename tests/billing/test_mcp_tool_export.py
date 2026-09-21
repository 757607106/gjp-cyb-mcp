"""验证开单 MCP 对外发布的工具名是 camelCase，防止 snake_case 回退。

gjp_common.mcp 在导出层把 snake_case 工具名统一转为 camelCase；
BILLING_MCP_TOOL_NAMES 是开单工具的完整白名单，经转换后必须等于
模型在 prompt 与客户端工具列表中看到的 camelCase 集合。最后通过
create_mcp_server 的 ListToolsRequest handler 端到端验证导出层行为。
"""

from __future__ import annotations

import asyncio
import json

import jsonschema


from erp_billing.adapters import UnavailableBillingApi
from erp_billing.config import ErpBillingSettings
from erp_billing.session import ErpBillingSession
from erp_billing.toolset import BILLING_MCP_TOOL_NAMES, BillingToolSet
from gjp_common.context import InvocationContext, InvocationContextStore
from gjp_common.mcp import (
    _snake_to_camel,
    create_mcp_server,
)

_EXPECTED_CAMEL = frozenset(
    {
        # 商品目录与基础资料
        "syncProducts",
        "listProducts",
        "searchProducts",
        "searchBillingReferences",
        # 销售单
        "previewSalesOrder",
        "submitSalesOrder",
        "getSalesOrder",
        "listSalesOrders",
        "voidSalesOrder",
        "updateSalesOrder",
        # 采购单
        "previewPurchaseOrder",
        "submitPurchaseOrder",
        "getPurchaseOrder",
        "listPurchaseOrders",
        "voidPurchaseOrder",
        "updatePurchaseOrder",
        # 采购退货单
        "previewPurchaseReturn",
        "submitPurchaseReturn",
        "getPurchaseReturn",
        "listPurchaseReturns",
        "voidPurchaseReturn",
        # 销售退货单
        "previewSalesReturn",
        "submitSalesReturn",
        "getSalesReturn",
        "listSalesReturns",
        "voidSalesReturn",
        # 销售单继续收款 / 采购单继续付款
        "previewSalesReceipt",
        "submitSalesReceipt",
        "previewPurchasePayment",
        "submitPurchasePayment",
        # 库存调拨与其他出入库
        "previewStockTransfer",
        "submitStockTransfer",
        "previewOtherStockDoc",
        "submitOtherStockDoc",
        # 收款单与付款单
        "previewReceiptOrder",
        "submitReceiptOrder",
        "getReceiptOrder",
        "listReceiptOrders",
        "voidReceiptOrder",
        "previewPaymentOrder",
        "submitPaymentOrder",
        "getPaymentOrder",
        "listPaymentOrders",
        "voidPaymentOrder",
        # 库存与往来查询
        "queryStock",
        "getStockByProduct",
        "getStockSummary",
        "queryStockLogs",
        "listStockAlerts",
        "getPurchaseSuggestions",
        "listStockDocTypes",
        "listReceivables",
        "listPayables",
        "getFinancialStatus",
        # 报表分析
        "querySalesReport",
        "queryPurchaseReport",
        "queryProfitReport",
        "querySettlementReport",
        "queryReconciliation",
    }
)


class _StaticIdentityResolver:
    def __init__(self, context: InvocationContext) -> None:
        self._context = context

    def resolve(self, _mcp_request_context) -> InvocationContext:
        return self._context


class _StaticToolSetResolver:
    def __init__(self, toolset: BillingToolSet) -> None:
        self._toolset = toolset

    def resolve(self, _context: InvocationContext) -> BillingToolSet:
        return self._toolset


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

    通过 FastMCP 的 list_tools 端到端验证导出层不再下发 snake_case，
    且业务声明的 input/output schema 已进入协议层工具定义。
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
        _StaticIdentityResolver(context),
        _StaticToolSetResolver(toolset),
    )
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert names == _EXPECTED_CAMEL
    by_name = {tool.name: tool for tool in tools}
    assert by_name["previewSalesOrder"].inputSchema == toolset.get(
        "preview_sales_order",
    ).input_schema
    assert by_name["previewSalesOrder"].outputSchema == toolset.get(
        "preview_sales_order",
    ).output_schema
    assert by_name["previewSalesOrder"].annotations.readOnlyHint is True
    assert by_name["submitSalesOrder"].annotations.destructiveHint is True


def test_output_schemas_accept_arguments_guard_rejection(tmp_path) -> None:
    """参数守卫的 tool_arguments_invalid 载荷能通过全部工具 outputSchema。

    lowlevel 会按 outputSchema 校验结构化输出；未知参数拒绝走 dict
    返回路径，若某个 outputSchema 不容纳 error 字段，拒绝结果会被
    升级成协议级错误。逐一验证 59 个工具均接受该载荷。
    """
    toolset = _make_billing_toolset(tmp_path)
    payload = {
        "ok": False,
        "error": {
            "code": "tool_arguments_invalid",
            "message": "工具参数不匹配：未知参数 demo",
        },
    }
    for name in BILLING_MCP_TOOL_NAMES:
        tool = toolset.get(name)
        assert tool.output_schema is not None, name
        jsonschema.validate(instance=payload, schema=tool.output_schema)

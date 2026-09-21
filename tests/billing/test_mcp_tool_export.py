"""验证开单 MCP 对外发布的工具名是 camelCase，防止 snake_case 回退。

gjp_common.mcp 在导出层把 snake_case 工具名统一转为 camelCase；
BILLING_MCP_TOOL_NAMES 是开单工具的完整白名单，经转换后必须等于
模型在 prompt 与客户端工具列表中看到的 camelCase 集合。最后通过
create_mcp_server 的 ListToolsRequest handler 端到端验证导出层行为。
"""

from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy

import jsonschema
import pytest


from erp_billing.adapters import UnavailableBillingApi
from erp_billing.config import ErpBillingSettings
from erp_billing.presentation import filter_billing_result, present_billing_result
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

    通过 MCPServer 的 list_tools 端到端验证导出层不再下发 snake_case，
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
    assert by_name["previewSalesOrder"].input_schema == toolset.get(
        "preview_sales_order",
    ).input_schema
    assert by_name["previewSalesOrder"].output_schema == toolset.get(
        "preview_sales_order",
    ).output_schema
    assert by_name["previewSalesOrder"].annotations.read_only_hint is True
    assert by_name["submitSalesOrder"].annotations.destructive_hint is True


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


def _filtered_success_payload(name):
    """按工具场景给出非空业务样例，不从 schema 反向生成以免空对象掩盖漏字段。"""
    item = {
        "productName": "土豆", "quantity": 0, "unitPrice": "0.00", "amount": None,
        "isDefault": False, "units": [],
    }
    page = {"page": 1, "page_size": 20, "total": 0, "has_more": False}
    if name == "sync_products":
        fields = {"product_count": 0, "sample_products": [item]}
    elif name == "list_products":
        fields = {**page, "products": [item]}
    elif name == "search_products":
        fields = {"results": [
            {"query": "土豆", "status": "matched", "product": {"product_id": "P001", **item},
             "recommendations": []},
            {"query": "未找到的商品", "status": "unmatched", "product": None,
             "recommendations": []},
        ]}
    elif name == "search_billing_references":
        fields = {
            **page, "reference_type": "customer", "keyword": "客户甲",
            "options": [{"id": "CUS-1", "code": "C001", "name": "客户甲", "is_default": False}],
        }
    elif name.startswith("preview_"):
        fields = {
            "ready_to_submit": True, "preview_id": "preview-private-id",
            "required_actions": ["confirm_submit"],
            "preview": {"items": [item], "totalAmount": "0.00"},
            "confirmed_products": [{"line_id": "L001", "product_id": "P001", **item}],
            "unit_warnings": [], "missing_required_fields": [],
            "reference_resolutions": {"customer": {
                "query": "客户甲", "status": "ambiguous", "selected": None,
                "candidates": [{"id": "CUS-1", "code": "C001", "name": "客户甲"}],
            }},
        }
    elif name.startswith("submit_"):
        fields = {"submitted": True, "idempotent_replay": False}
        if name == "submit_sales_order":
            fields["order_no"] = "XS20260804001"
        else:
            fields.update(document_id="document-private-id", document_no="DJ20260804001")
    elif name.startswith(("get_", "list_")) and name.endswith(("_order", "_orders", "_return", "_returns")):
        document = {
            "id": "123", "orderNo": "DJ20260804001", "status": 0,
            "items": [{"id": 21, "orderItemId": "21", "productId": "P001", **item}],
        }
        if name.startswith("get_"):
            fields = {"order" if name == "get_sales_order" else "document": document}
        else:
            fields = {**page, "orders" if name == "list_sales_orders" else "documents": [document]}
    elif name.startswith(("void_", "update_")):
        fields = {
            "voided" if name.startswith("void_") else "modified": True,
            "order_no" if name.endswith("sales_order") else "document_no": "DJ20260804001",
        }
    elif name == "list_stock_doc_types":
        fields = {"types": [{"id": "TYPE-1", "name": "报损", "is_system": False}]}
    else:
        fields = {**page, "data": {"items": [item], "totalAmount": "0.00"}}
    return {"ok": True, **fields}


def _with_private_fields(value):
    """在每层对象注入诊断及未知分支，防止仅过滤顶层或按黑名单放行未知字段。"""
    if isinstance(value, list):
        return [_with_private_fields(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        **{key: _with_private_fields(child) for key, child in value.items()},
        "raw": {"productName": "private-raw"},
        "details": {"message": "private-details"},
        "trace": "private-trace", "traceId": "private-trace-id",
        "catalog": {"name": "private-catalog"}, "catalog_version": "private-version",
        "unknown": {"quantity": 999}, "unknownField": "private-unknown",
    }


@pytest.mark.parametrize("name", sorted(BILLING_MCP_TOOL_NAMES))
@pytest.mark.parametrize("outcome", ["success", "error"])
def test_filtered_output_schemas_accept_all_59_tools(tmp_path, name, outcome):
    """原业务 schema 不变；过滤后 success/error 的执行字段、嵌套必填项仍兼容。"""
    assert len(BILLING_MCP_TOOL_NAMES) == 59
    tools = _make_billing_toolset(tmp_path)
    schema = tools.get(name).output_schema
    assert schema is not None
    expected = _filtered_success_payload(name) if outcome == "success" else {
        "ok": False, "error": {"code": "erp_live_request_failed", "message": "操作失败"},
    }
    payload = _with_private_fields(expected)
    original = deepcopy(payload)
    jsonschema.validate(instance=payload, schema=schema)

    # 既覆盖内部 snake_case，也覆盖真实 MCP 导出的 camelCase 名称。
    for tool_name in (name, _snake_to_camel(name)):
        filtered = filter_billing_result(tool_name, payload)
        jsonschema.validate(instance=filtered, schema=schema)
        assert filtered == expected
        # 字典相等不足以区分 False/0、0/0.0；序列化还可验证原键名和原值类型。
        assert json.dumps(filtered, sort_keys=True) == json.dumps(expected, sort_keys=True)
        text, structured = present_billing_result(tool_name, payload)
        assert structured == filtered
        assert "private-" not in text
        assert payload == original
        if outcome == "error":
            assert filtered["ok"] is False
            assert set(filtered["error"]) == {"code", "message"}
            assert filtered["error"]["code"] not in text
            assert "操作失败" in text
        elif name == "search_products":
            # results 的 query/status 是原 schema 的嵌套 required，不能只校验顶层 ok。
            assert [(row["query"], row["status"]) for row in filtered["results"]] == [
                ("土豆", "matched"), ("未找到的商品", "unmatched"),
            ]
            assert filtered["results"][1]["product"] is None
            assert filtered["results"][1]["recommendations"] == []


def _schema_properties(schema, path=""):
    if isinstance(schema, dict):
        for name, child in schema.get("properties", {}).items():
            yield f"{path}.{name}", child
        for name, child in schema.items():
            yield from _schema_properties(child, f"{path}.{name}")
    elif isinstance(schema, list):
        for index, child in enumerate(schema):
            yield from _schema_properties(child, f"{path}[{index}]")


def test_all_published_tools_are_self_describing_without_business_instructions(tmp_path):
    tools = _make_billing_toolset(tmp_path)
    context = InvocationContext("tenant-test", "user-test", "billing-test")
    server = create_mcp_server(
        "erp-billing", tools, _StaticIdentityResolver(context), _StaticToolSetResolver(tools),
    )
    published = asyncio.run(server.list_tools())
    assert len(published) == 59
    for tool in published:
        assert tool.description
        assert "仅展示业务信息" in tool.description, tool.name
        assert "内部 ID" in tool.description, tool.name
        assert "控制字段" in tool.description, tool.name
        for path, parameter in _schema_properties(tool.input_schema):
            assert parameter.get("description", "").strip(), f"{tool.name}{path} 缺少参数说明"
        metadata = tool.description + json.dumps(tool.input_schema, ensure_ascii=False)
        for internal_name in BILLING_MCP_TOOL_NAMES:
            assert not re.search(r"(?<!\w)" + re.escape(internal_name) + r"(?!\w)", metadata), (
                f"{tool.name} 仍引用未发布的 {internal_name}"
            )


def test_document_tool_descriptions_explain_dependencies_and_confirmation(tmp_path):
    tools = _make_billing_toolset(tmp_path)
    for name in BILLING_MCP_TOOL_NAMES:
        tool = tools.get(name)
        if name.startswith("preview_"):
            assert _snake_to_camel(name.replace("preview_", "submit_", 1)) in tool.description, name
            assert "确认" in tool.description, name
            assert "preview_id" in tool.description, name
        elif name.startswith("submit_"):
            assert _snake_to_camel(name.replace("submit_", "preview_", 1)) in tool.description, name
            assert "用户" in tool.input_schema["properties"]["confirmed_by_user"]["description"], name
            assert "preview_id" in tool.description, name
        elif name.startswith(("update_", "void_")):
            rest = name.split("_", 1)[1]
            assert _snake_to_camel("get_" + rest) in tool.description, name
            assert "确认" in tool.description, name
            confirmation = tool.input_schema["properties"]["confirmed_by_user"]
            if "default" in confirmation:
                assert confirmation["default"] is False
            else:
                assert "confirmed_by_user" in tool.input_schema["required"]


def test_read_tool_descriptions_distinguish_inventory_orders_and_reports(tmp_path):
    tools = _make_billing_toolset(tmp_path)
    distinctions = {
        "query_stock": "queryStockLogs",
        "get_stock_by_product": "searchProducts",
        "query_stock_logs": "queryStock",
        "list_sales_orders": "querySalesReport",
        "query_sales_report": "listSalesOrders",
        "list_purchase_orders": "queryPurchaseReport",
        "query_purchase_report": "listPurchaseOrders",
        "preview_sales_receipt": "previewReceiptOrder",
        "preview_purchase_payment": "previewPaymentOrder",
        "preview_receipt_order": "previewSalesReceipt",
        "preview_payment_order": "previewPurchasePayment",
    }
    for name, alternative in distinctions.items():
        assert alternative in tools.get(name).description, name

"""验证 MCP 双通道白名单及纯业务展示。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from yuncyb.presentation import filter_yuncyb_result, present_yuncyb_result, render_yuncyb_result
from tests.yuncyb.test_document_tools import _toolset as document_toolset


def test_render_yuncyb_result_removes_control_and_technical_fields() -> None:
    text = render_yuncyb_result(
        "preview_sales_order",
        {
            "ok": True,
            "ready_to_submit": True,
            "preview_id": "sales-order-preview-secret",
            "reference_resolutions": {
                "customer": {
                    "status": "matched",
                    "selected": {
                        "id": "CUSTOMER-SECRET",
                        "name": "张三",
                        "is_default": True,
                    },
                },
            },
            "preview": {
                "customer": "张三",
                "items": [
                    {
                        "line_id": "L001",
                        "productId": "PRODUCT-SECRET",
                        "product_name": "土豆",
                        "quantity": 10,
                        "unit": "斤",
                        "amount": 50,
                    },
                ],
            },
        },
    )

    assert "preview_id" not in text
    assert "CUSTOMER-SECRET" not in text
    assert "PRODUCT-SECRET" not in text
    assert "line_id" not in text
    assert "土豆" in text
    assert "10" in text
    assert "50" in text
    assert "提交状态" not in text
    assert "matched" not in text


def test_render_yuncyb_result_keeps_business_error_without_internal_details() -> None:
    text = render_yuncyb_result(
        "get_sales_order",
        {
            "ok": False,
            "error": {
                "code": "erp_sales_order_not_found",
                "message": "销售单不存在：2065262048677449729",
                "details": {"trace_id": "trace-secret"},
            },
        },
    )

    assert text == "业务处理未完成：销售单不存在"
    assert "2065262048677449729" not in text
    assert "trace-secret" not in text


def test_render_yuncyb_result_hides_tool_argument_details() -> None:
    text = render_yuncyb_result(
        "preview_sales_order",
        {
            "ok": False,
            "error": {
                "code": "tool_arguments_invalid",
                "message": "工具参数不匹配：未知参数 preview_id",
            },
        },
    )

    assert text == "业务处理未完成：请求格式不正确，请按当前业务场景重新提交。"
    assert "preview_id" not in text


def test_render_yuncyb_result_is_markdown_table() -> None:
    text = render_yuncyb_result(
        "list_products",
        {
            "ok": True,
            "page": 1,
            "page_size": 20,
            "has_more": True,
            "products": [
                {
                    "product_name": "土豆",
                    "unit": "斤",
                    "product_id": "P001",
                    "stock": 12,
                },
            ],
        },
    )

    assert text.startswith("## 业务结果\n")
    assert "### 商品列表" in text
    assert "| 商品名称 | 单位 | 库存 |" in text
    assert "| 土豆 | 斤 | 12 |" in text
    assert "P001" not in text
    assert "| 提示 | 还有更多结果 |" in text


def test_markdown_table_escapes_cell_delimiters_and_flattens_line_breaks() -> None:
    text = render_yuncyb_result(
        "list_products",
        {
            "ok": True,
            "products": [{"product_name": "土豆|精品\n大包装", "unit": "箱"}],
        },
    )

    assert "| 土豆\\|精品、大包装 | 箱 |" in text
    assert "<br" not in text


def test_projection_normalizes_numeric_text_on_numeric_fields() -> None:
    text = render_yuncyb_result(
        "get_stock_summary",
        {
            "ok": True,
            "data": {
                "totalQuantity": 53412.1415,
                "skuCount": "546",
                "negativeStockCount": "170",
            },
        },
    )

    assert "### 查询结果" in text
    assert "| 商品数 | 546 |" in text
    assert "| 负库存商品数 | 170 |" in text
    assert "| 合计数量 | 53412.1415 |" in text


def test_projection_keeps_identifier_like_numeric_text_as_string() -> None:
    text = render_yuncyb_result(
        "list_receivables",
        {
            "ok": True,
            "data": [
                {
                    "counterpartyName": "西湖区好再来超市二店",
                    "contactPhone": "13837628701",
                    "receivableAmount": "60.0",
                },
            ],
        },
    )

    assert "| 往来单位 | 联系电话 | 应收金额 |" in text
    assert "| 西湖区好再来超市二店 | 13837628701 | 60.0 |" in text


def test_projection_prefers_status_name_over_status_code() -> None:
    text = render_yuncyb_result(
        "list_purchase_orders",
        {
            "ok": True,
            "documents": [
                {
                    "orderNo": "CG202607130528",
                    "status": 2,
                    "statusName": "已生效",
                    "paymentStatus": 0,
                    "paymentStatusName": "未付款",
                },
            ],
        },
    )

    assert "| 单据编号 | 状态 | 付款状态 |" in text
    assert "| CG202607130528 | 已生效 | 未付款 |" in text


def test_projection_outputs_chinese_keys_for_query_tool_results() -> None:
    """真实查询结果形状的投影不得再出现英文键。"""
    text = render_yuncyb_result(
        "list_stock_alerts",
        {
            "ok": True,
            "data": [
                {
                    "productId": "2069723419687178242",
                    "productCode": "SPMQRWORTR",
                    "productName": "蓝月亮深层洁净洗衣液3kg",
                    "warehouseName": "宁波分仓",
                    "currentQuantity": -1.0,
                    "minStock": 20.0,
                    "alertType": 3,
                    "severity": 3,
                    "alertTypeName": "负库存",
                    "severityName": "严重",
                },
            ],
        },
    )

    assert "| 商品编号 | 商品名称 | 仓库 | 当前库存 | 最低库存 | 预警类型 | 预警级别 |" in text
    assert "| SPMQRWORTR | 蓝月亮深层洁净洗衣液3kg | 宁波分仓 | -1.0 | 20.0 | 负库存 | 严重 |" in text
    assert "2069723419687178242" not in text


@pytest.mark.parametrize("field", [
    "debugInfo", "apiPath", "requestUrl", "traceId", "accessToken", "authorization", "cookie",
    "catalogVersion", "raw", "payload", "details", "imageUrls", "createdBy", "createTime", "updateTime",
    "inboundType", "outboundType", "unrecognizedField", "unrecognized_field", "未知技术字段",
])
def test_both_channels_reject_unknown_and_technical_fields_at_every_depth(field):
    source = {
        "ok": True,
        field: {"name": "技术内容不得通过业务子键透出"},
        "data": [{
            "productName": "土豆",
            "quantity": 0,
            field: "TECHNICAL-SENTINEL",
            "warehouseStocks": [{"warehouseName": "一号仓", field: "NESTED-SENTINEL"}],
        }],
    }
    text, structured = present_yuncyb_result("queryStock", source)

    assert structured == {
        "ok": True,
        "data": [{"productName": "土豆", "quantity": 0, "warehouseStocks": [{"warehouseName": "一号仓"}]}],
    }
    assert "SENTINEL" not in text
    assert "技术内容" not in text
    assert "土豆" in text
    assert "一号仓" in text


@pytest.mark.parametrize("tool_name,field", [
    ("searchProducts", "product_id"),
    ("searchBusinessReferences", "id"),
    ("previewSalesOrder", "preview_id"),
    ("previewPurchaseOrder", "line_id"),
    ("previewStockTransfer", "product_id"),
    ("previewReceiptOrder", "biz_id"),
    ("getSalesOrder", "orderItemId"),
    ("getPurchaseOrder", "id"),
    ("submitPurchaseOrder", "document_id"),
    ("queryStock", "productId"),
    ("listReceivables", "customerId"),
    ("listPayables", "supplierId"),
    ("listStockDocTypes", "id"),
])
def test_execution_identifiers_are_scoped_to_tools_and_never_in_business_text(tool_name, field):
    result = {"ok": True, "data": [{field: "EXECUTION-ONLY", "name": "业务名称"}]}
    text, structured = present_yuncyb_result(tool_name, result)

    assert structured["data"][0][field] == "EXECUTION-ONLY"
    assert "EXECUTION-ONLY" not in text
    assert filter_yuncyb_result("getStockSummary", result) == {"ok": True, "data": [{"name": "业务名称"}]}


def test_structured_projection_preserves_empty_values_and_original_types_without_mutation():
    result = {
        "ok": True,
        "ready_to_submit": False,
        "preview_id": None,
        "required_actions": [],
        "confirmed_products": [],
        "preview": None,
        "save_type": "final",
        "page": 1,
        "page_size": 20,
        "total": 0,
        "has_more": False,
        "data": [{"quantity": "0", "amount": 0, "remark": "", "items": [], "productId": "P001"}],
    }
    original = deepcopy(result)
    text, structured = present_yuncyb_result("previewSalesOrder", result)

    assert structured == result
    assert structured["ready_to_submit"] is False
    assert structured["preview_id"] is None
    structured["data"][0]["items"].append({"quantity": 1})
    assert result == original
    assert "P001" not in text
    assert "提交状态" not in text
    assert "page" not in text
    assert "required_actions" not in text


def test_business_projection_excludes_control_metadata_even_when_it_has_chinese_labels():
    result = {
        "ok": True,
        "ready_to_submit": True,
        "required_actions": ["confirm_submit"],
        "kind": "outbound",
        "type": "query",
        "view": "details",
        "is_system": True,
        "page": 1,
        "page_size": 20,
        "total": 2,
        "catalog_version": "catalog-secret",
        "reference_resolutions": {
            "customer": {"status": "matched", "selected": {"id": "CUS-1", "name": "客户甲"}},
        },
        "preview": {"customer": "客户甲", "save_type": "final", "status": 2},
    }
    text, structured = present_yuncyb_result("previewSalesOrder", result)
    assert "### 业务资料" in text
    assert "| 名称 | 客户甲 |" in text
    assert "### 单据预览" in text
    assert "| 客户 | 客户甲 |" in text
    assert "> 请核对以上业务信息；确认无误后方可提交。" in text
    assert structured["ready_to_submit"] is True
    assert structured["reference_resolutions"]["customer"]["status"] == "matched"
    assert "catalog_version" not in structured


@pytest.mark.parametrize("warning,parameter", [("unit_warnings", "confirmed_units"), ("price_warnings", "confirmed_prices")])
def test_confirmation_prompts_do_not_repeat_internal_parameter_names(warning, parameter):
    result = {"ok": True, warning: [{"product": "土豆", "prompt": f"通过 {parameter} 回传，保留 order_text"}]}
    text, structured = present_yuncyb_result("previewPurchaseOrder", result)

    assert "土豆" in text
    assert parameter not in text
    assert "order_text" not in text
    assert parameter in structured[warning][0]["prompt"]


def test_structured_error_removes_diagnostics_and_internal_identifier_from_message():
    result = {
        "ok": False,
        "error": {
            "code": "erp_sales_order_not_found",
            "message": "销售单不存在：INTERNAL-ORDER-ID",
            "details": {"trace_id": "TRACE-SECRET", "upstream_code": "A12345", "raw": {"name": "secret"}},
            "debug": "DEBUG-SECRET",
        },
    }
    text, structured = present_yuncyb_result("getSalesOrder", result)

    assert structured == {"ok": False, "error": {"code": "erp_sales_order_not_found", "message": "销售单不存在"}}
    assert text == "业务处理未完成：销售单不存在"
    assert "INTERNAL-ORDER-ID" not in json.dumps(structured)


def test_document_records_and_nested_report_sections_keep_business_fields():
    result = {
        "ok": True,
        "document": {
            "orderNo": "SK20260921001",
            "客户": "客户甲",
            "receiptRecords": [{"receiptNo": "SK001", "receiptDate": "2026-09-21", "realAmount": "12.50"}],
            "writeoffDetails": [{"bizId": "BIZ-1", "bizNo": "XS001", "writeoffAmount": "12.50"}],
        },
        "data": {
            "assets": [{"name": "流动资产", "children": [{"name": "现金", "amount": "12.50"}]}],
            "recentLogs": [{"bizNo": "RK001", "changeQty": 2}],
            "rangeOccurrenceAmount": "12.50",
            "list": [{"orderNo": "XS001", "amount": "12.50"}],
        },
    }
    text, structured = present_yuncyb_result("getReceiptOrder", result)
    assert structured == result
    assert "### 单据" in text
    assert "| 收款单号 | 收款日期 | 实账金额 |" in text
    assert "| SK001 | 2026-09-21 | 12.5 |" in text
    assert "| 业务单号 | 核销金额 |" in text
    assert "| XS001 | 12.5 |" in text
    assert "| 名称 | 金额 |" in text
    assert "| 现金 | 12.5 |" in text
    assert "| RK001 | 2 |" in text
    assert "BIZ-1" not in text


def test_report_and_unknown_tool_do_not_receive_execution_fields():
    result = {
        "ok": True,
        "preview_id": "PREVIEW-SECRET",
        "document_id": "DOCUMENT-SECRET",
        "data": [{"id": "ROW-SECRET", "productId": "PRODUCT-SECRET", "profitAmount": 20}],
    }
    for tool_name in ("queryProfitReport", "getFinancialStatus", "unknownTool"):
        assert filter_yuncyb_result(tool_name, result) == {"ok": True, "data": [{"profitAmount": 20}]}


@pytest.mark.parametrize("tool_name,order_id,counterparty", [
    ("preview_sales_return", "SO-1", "客户"),
    ("preview_purchase_return", "PO-1", "供应商"),
])
def test_real_return_preview_retains_refund_amount_and_counterparty(tmp_path, tool_name, order_id, counterparty):
    tools = document_toolset(tmp_path)
    result = asyncio.run(getattr(tools, tool_name)(
        order_id=order_id, refund_amount=4.0, refund_account_id="ACC-1",
        discount_amount=1.0, discount_account_id="ACC-2",
    ))
    text, structured = present_yuncyb_result(tool_name, result)
    assert structured["ready_to_submit"] is True
    assert structured["preview"]["退款金额"] == 4.0
    assert structured["preview"][counterparty] == result["preview"][counterparty]
    assert "### 单据预览" in text
    assert "| 退款金额 | 4.0 |" in text
    assert "| 退款账户 | 现金账户 |" in text
    assert "| 优惠账户 | 微信账户 |" in text
    assert "| 折扣金额 | 1.0 |" in text
    assert f"| {counterparty} | {result['preview'][counterparty]} |" in text
    assert result["preview_id"] not in text
    assert order_id not in text


def test_real_purchase_preview_retains_line_amount(tmp_path):
    result = asyncio.run(document_toolset(tmp_path).preview_purchase_order(
        order_text="土豆2斤", supplier="鑫达供货", warehouse="一号仓", handler="张三", order_date="2026-09-01",
    ))
    text, structured = present_yuncyb_result("previewPurchaseOrder", result)
    assert structured["ready_to_submit"] is True
    assert structured["preview"]["items"][0]["line_amount"] == 6.0
    assert "### 单据预览" in text
    assert "| 合计金额 | 6.0 |" in text
    assert "| 名称 | 数量 | 单位 | 单价 | 金额 |" in text
    assert "| 土豆 | 2.0 | 斤 | 3.0 | 6.0 |" in text

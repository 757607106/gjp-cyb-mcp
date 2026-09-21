"""验证 MCP 文本展示通道只保留 ERP 业务信息。"""

from __future__ import annotations

import json

from erp_billing.presentation import render_billing_result


def test_render_billing_result_removes_control_and_technical_fields() -> None:
    text = render_billing_result(
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
    assert "待确认提交" in text


def test_render_billing_result_keeps_business_error_without_internal_details() -> None:
    text = render_billing_result(
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


def test_render_billing_result_hides_tool_argument_details() -> None:
    text = render_billing_result(
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


def test_render_billing_result_is_valid_json_after_business_prefix() -> None:
    text = render_billing_result(
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

    payload = json.loads(text.split("：\n", 1)[1])
    assert payload["商品列表"][0]["商品名称"] == "土豆"
    assert payload["商品列表"][0]["库存"] == 12
    assert "P001" not in text
    assert payload["提示"] == "还有更多结果"


def test_projection_normalizes_numeric_text_on_numeric_fields() -> None:
    text = render_billing_result(
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

    payload = json.loads(text.split("：\n", 1)[1])["查询结果"]
    assert payload["商品数"] == 546
    assert payload["负库存商品数"] == 170
    assert payload["合计数量"] == 53412.1415


def test_projection_keeps_identifier_like_numeric_text_as_string() -> None:
    text = render_billing_result(
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

    payload = json.loads(text.split("：\n", 1)[1])["查询结果"][0]
    assert payload["联系电话"] == "13837628701"
    assert payload["应收金额"] == 60.0
    assert isinstance(payload["联系电话"], str)


def test_projection_prefers_status_name_over_status_code() -> None:
    text = render_billing_result(
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

    payload = json.loads(text.split("：\n", 1)[1])["单据列表"][0]
    assert payload["状态"] == "已生效"
    assert payload["付款状态"] == "未付款"


def test_projection_outputs_chinese_keys_for_query_tool_results() -> None:
    """真实查询结果形状的投影不得再出现英文键。"""
    text = render_billing_result(
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

    payload = json.loads(text.split("：\n", 1)[1])["查询结果"][0]
    assert set(payload) == {
        "商品编号",
        "商品名称",
        "仓库",
        "当前库存",
        "最低库存",
        "预警类型",
        "预警级别",
    }
    assert "2069723419687178242" not in text

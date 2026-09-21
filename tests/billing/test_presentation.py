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

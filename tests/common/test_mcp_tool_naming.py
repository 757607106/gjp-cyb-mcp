"""验证 MCP 导出层把 snake_case 工具名统一转为 camelCase。

历史问题：服务端导出 snake_case，对接客户端把工具名规范化为 camelCase
暴露给模型，而系统提示词仍是 snake_case，导致模型按 prompt 的 snake_case
调用却命中不到客户端的 camelCase 工具列表。修复后在 gjp_common.mcp 导出层
统一转为 camelCase，本测试锁定该行为，防止回退。
"""

from __future__ import annotations

from gjp_common.mcp import _snake_to_camel


def test_snake_to_camel_converts_billing_tools() -> None:
    """开单十个工具名必须全部映射为 camelCase。"""
    cases = {
        "sync_products": "syncProducts",
        "list_products": "listProducts",
        "search_products": "searchProducts",
        "search_billing_references": "searchBillingReferences",
        "preview_sales_order": "previewSalesOrder",
        "submit_sales_order": "submitSalesOrder",
        "get_sales_order": "getSalesOrder",
        "list_sales_orders": "listSalesOrders",
        "void_sales_order": "voidSalesOrder",
        "update_sales_order": "updateSalesOrder",
    }
    for snake, camel in cases.items():
        assert _snake_to_camel(snake) == camel


def test_snake_to_camel_single_segment_unchanged() -> None:
    """无下划线的名字原样返回。"""
    assert _snake_to_camel("ping") == "ping"
    assert _snake_to_camel("health") == "health"


def test_snake_to_camel_empty_string() -> None:
    assert _snake_to_camel("") == ""

"""库存、往来与报表查询工具的单元测试。"""

from __future__ import annotations

import asyncio

from yuncyb.ports import (
    YunCybDataResult,
    YunCybReferenceSnapshot,
)
from tests.yuncyb.test_yuncyb import _yuncyb_toolset, _session


def _page(rows, total=None):
    return {
        "list": rows,
        "pageNum": 1,
        "pageSize": 20,
        "total": len(rows) if total is None else total,
    }


class QueryApi:
    """记录调用参数并返回确定性数据的查询端口。"""

    def __init__(self):
        self.calls = []

    async def search_warehouses(self, context, keyword, limit=10, page=1):
        return YunCybReferenceSnapshot(
            options=(
                {"id": "WH-1", "code": "W001", "name": "一号仓", "isDefault": True},
                {"id": "WH-2", "code": "W002", "name": "二号仓", "isDefault": False},
            ),
            total=2, page_num=page, page_size=limit,
        )

    async def search_customers(self, context, keyword, limit=10, page=1):
        return YunCybReferenceSnapshot(
            options=({"id": "CUS-1", "name": "客户甲"},),
            total=1, page_num=page, page_size=limit,
        )

    async def search_suppliers(self, context, keyword, limit=10, page=1):
        return YunCybReferenceSnapshot(
            options=({"id": "SUP-1", "name": "供应商乙"},),
            total=1, page_num=page, page_size=limit,
        )

    async def query_stock_page(self, context, payload):
        self.calls.append(("query_stock_page", payload))
        return YunCybDataResult(
            _page([{"productId": "P001", "productName": "土豆", "quantity": 10}]),
        )

    async def get_stock_by_product(self, context, product_id):
        self.calls.append(("get_stock_by_product", product_id))
        return YunCybDataResult(
            [{"warehouseName": "一号仓", "quantity": 10}],
        )

    async def get_stock_summary(self, context, payload):
        self.calls.append(("get_stock_summary", payload))
        return YunCybDataResult({"totalQuantity": 10, "totalAmount": 35.0})

    async def query_stock_logs(self, context, payload):
        self.calls.append(("query_stock_logs", payload))
        return YunCybDataResult(_page([{"productId": "P001", "changeType": 1}]))

    async def list_stock_alerts(self, context, payload):
        self.calls.append(("list_stock_alerts", payload))
        return YunCybDataResult(_page([{"productId": "P001", "alertType": 1}]))

    async def get_purchase_suggestions(self, context):
        self.calls.append(("get_purchase_suggestions",))
        return YunCybDataResult([{"productId": "P001", "suggestQuantity": 5}])

    async def list_stock_doc_types(self, context, kind):
        self.calls.append(("list_stock_doc_types", kind))
        return YunCybDataResult([{"id": "DT-1", "name": "报溢单"}])

    async def list_receivables(self, context, view, params):
        self.calls.append(("list_receivables", view, params))
        return YunCybDataResult(_page([{"customerId": "CUS-1", "amount": 7.0}]))

    async def list_payables(self, context, view, params):
        self.calls.append(("list_payables", view, params))
        return YunCybDataResult(_page([{"supplierId": "SUP-1", "amount": 5.0}]))

    async def get_financial_status(self, context, biz_date):
        self.calls.append(("get_financial_status", biz_date))
        return YunCybDataResult({"receiptAmount": 7.0})

    async def query_sales_report(self, context, view, params):
        self.calls.append(("query_sales_report", view, params))
        return YunCybDataResult(_page([{"productName": "土豆", "quantity": 2}]))

    async def query_purchase_report(self, context, view, params):
        self.calls.append(("query_purchase_report", view, params))
        return YunCybDataResult(_page([{"productName": "土豆", "quantity": 1}]))

    async def query_profit_report(self, context, view, params):
        self.calls.append(("query_profit_report", view, params))
        return YunCybDataResult(_page([{"profit": 3.5}]))

    async def query_settlement_report(self, context, params):
        self.calls.append(("query_settlement_report", params))
        return YunCybDataResult(_page([{"customerName": "客户甲", "amount": 7.0}]))

    async def query_reconciliation(self, context, view, params):
        self.calls.append(("query_reconciliation", view, params))
        return YunCybDataResult(_page([{"customerName": "客户甲", "balance": 7.0}]))


def _toolset(tmp_path):
    session = _session(
        tmp_path,
        [{"id": "P001", "name": "土豆", "unit": "斤", "salesPrice": 3.5}],
    )
    api = QueryApi()
    return _yuncyb_toolset(session, api), api


def test_query_stock_unwraps_page_and_resolves_warehouse_name(tmp_path):
    toolset, api = _toolset(tmp_path)
    result = asyncio.run(toolset.query_stock(
        keyword="土豆", warehouse_id="一号仓", stock_status=1,
    ))
    assert result["ok"] is True
    assert result["data"] == [
        {"productId": "P001", "productName": "土豆", "quantity": 10},
    ]
    assert result["total"] == 1
    assert result["has_more"] is False
    assert api.calls[0] == (
        "query_stock_page",
        {
            "productName": "土豆",
            "warehouseIds": ["WH-1"],
            "stockStatus": 1,
            "pageNum": 1,
            "pageSize": 20,
        },
    )


def test_query_stock_rejects_unknown_warehouse_name(tmp_path):
    toolset, _api = _toolset(tmp_path)
    result = asyncio.run(toolset.query_stock(warehouse_id="不存在的仓库"))
    assert result["ok"] is False
    assert result["error"]["code"] == "erp_reference_unmatched"


def test_get_stock_by_product_and_summary(tmp_path):
    toolset, api = _toolset(tmp_path)
    detail = asyncio.run(toolset.get_stock_by_product("P001"))
    assert detail["ok"] is True
    assert detail["data"] == [{"warehouseName": "一号仓", "quantity": 10}]
    summary = asyncio.run(toolset.get_stock_summary())
    assert summary["ok"] is True
    assert summary["data"] == {"totalQuantity": 10, "totalAmount": 35.0}
    assert api.calls[0] == ("get_stock_by_product", "P001")


def test_query_stock_logs_requires_valid_date_range(tmp_path):
    toolset, _api = _toolset(tmp_path)
    result = asyncio.run(toolset.query_stock_logs(
        start_date="2026-09-20", end_date="2026-09-01",
    ))
    assert result["ok"] is False
    assert result["error"]["code"] == "erp_sales_order_date_invalid"


def test_list_stock_alerts_rejects_unknown_type(tmp_path):
    toolset, _api = _toolset(tmp_path)
    result = asyncio.run(toolset.list_stock_alerts(alert_type=9))
    assert result["ok"] is False
    assert result["error"]["code"] == "erp_stock_alert_type_invalid"


def test_list_stock_doc_types_requires_known_kind(tmp_path):
    toolset, api = _toolset(tmp_path)
    ok = asyncio.run(toolset.list_stock_doc_types(" inbound "))
    assert ok["ok"] is True
    assert ok["kind"] == "inbound"
    assert ok["types"] == [{"id": "DT-1", "name": "报溢单"}]
    assert api.calls[0] == ("list_stock_doc_types", "inbound")

    invalid = asyncio.run(toolset.list_stock_doc_types("transfer"))
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "erp_stock_doc_type_invalid"


def test_list_receivables_resolves_customer_and_view(tmp_path):
    toolset, api = _toolset(tmp_path)
    result = asyncio.run(toolset.list_receivables(
        view="summary", customer_id="客户甲", start_date="2026-09-01",
        end_date="2026-09-20",
    ))
    assert result["ok"] is True
    assert result["data"][0]["customerId"] == "CUS-1"
    assert api.calls[0] == (
        "list_receivables",
        "summary",
        {
            "pageNum": 1, "pageSize": 20, "customerId": "CUS-1",
            "startDate": "2026-09-01", "endDate": "2026-09-20",
        },
    )

    invalid = asyncio.run(toolset.list_receivables(view="overview"))
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "erp_receivable_view_invalid"


def test_list_payables_resolves_supplier(tmp_path):
    toolset, api = _toolset(tmp_path)
    result = asyncio.run(toolset.list_payables(view="details", supplier_id="SUP-1"))
    assert result["ok"] is True
    assert api.calls[0] == (
        "list_payables",
        "details",
        {"pageNum": 1, "pageSize": 20, "supplierId": "SUP-1"},
    )


def test_get_financial_status_validates_date(tmp_path):
    toolset, api = _toolset(tmp_path)
    result = asyncio.run(toolset.get_financial_status("2026-09-20"))
    assert result["ok"] is True
    assert result["data"] == {"receiptAmount": 7.0}
    assert api.calls[0] == ("get_financial_status", "2026-09-20")

    invalid = asyncio.run(toolset.get_financial_status("2026/09/20"))
    assert invalid["ok"] is False


def test_report_queries_pass_view_and_filters(tmp_path):
    toolset, api = _toolset(tmp_path)
    sales = asyncio.run(toolset.query_sales_report(
        view="ranking_product", start_date="2026-09-01", end_date="2026-09-20",
        page=2, page_size=50,
    ))
    assert sales["ok"] is True
    assert sales["total"] == 1
    assert api.calls[0] == (
        "query_sales_report",
        "ranking_product",
        {"pageNum": 2, "pageSize": 50,
         "startDate": "2026-09-01", "endDate": "2026-09-20"},
    )

    purchase = asyncio.run(toolset.query_purchase_report(view="statistics"))
    assert purchase["ok"] is True
    profit = asyncio.run(toolset.query_profit_report(view="by_customer"))
    assert profit["ok"] is True
    settlement = asyncio.run(toolset.query_settlement_report(
        start_date="2026-09-01", sort_by="amount", order_type="desc",
    ))
    assert settlement["ok"] is True
    assert api.calls[3] == (
        "query_settlement_report",
        {"startDate": "2026-09-01", "sortBy": "amount", "orderType": "desc"},
    )


def test_query_reconciliation_validates_view(tmp_path):
    toolset, api = _toolset(tmp_path)
    result = asyncio.run(toolset.query_reconciliation(view="summary"))
    assert result["ok"] is True
    assert result["data"][0]["customerName"] == "客户甲"

    invalid = asyncio.run(toolset.query_reconciliation(view="detail"))
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "erp_reconciliation_view_invalid"
    assert api.calls[0] == ("query_reconciliation", "summary", {
        "pageNum": 1, "pageSize": 20,
    })

"""采购、退货、库存写单据与资金单据工具的单元测试。

使用记录型 Fake API 覆盖两段式提交、金额与引用校验、payload 契约
（含退货同时退款/优惠写入 payment*/discount* 字段）与作废确认门槛。
"""

import asyncio
import json

from yuncyb.config import YunCybSettings
from yuncyb.ports import (
    YunCybDataResult,
    YunCybDocumentDetailResult,
    YunCybDocumentPageResult,
    YunCybDocumentResult,
    YunCybProductSnapshot,
    YunCybReferenceSnapshot,
    YunCybSalesOrderDetailResult,
)
from yuncyb.session import YunCybSession
from yuncyb.toolset import YunCybToolSet
from gjp_common.context import InvocationContext, InvocationContextStore

_PRODUCTS = [
    {"id": "P001", "code": "SP001", "name": "土豆", "unit": "斤", "purchasePrice": 3.0},
    {"id": "P002", "code": "SP002", "name": "西红柿", "unit": "斤"},
]


class DocumentApi:
    """记录型 Fake：覆盖采购、退货、资金与库存单据端口方法。"""

    _REFERENCE_OPTIONS = {
        "customer": ({"id": "CUS-1", "code": "C001", "name": "客户甲"},),
        "warehouse": (
            {"id": "WH-1", "code": "W001", "name": "一号仓", "isDefault": True},
            {"id": "WH-2", "code": "W002", "name": "二号仓"},
        ),
        "handler": ({"id": "STAFF-1", "code": "S001", "name": "张三", "isDefault": True},),
        "supplier": ({"id": "SUP-1", "code": "G001", "name": "鑫达供货"},),
        "settlement_account": (
            {"id": "ACC-1", "code": "A001", "name": "现金账户"},
            {"id": "ACC-2", "code": "A002", "name": "微信账户"},
        ),
    }

    def __init__(self):
        self.created: list[tuple[str, dict]] = []
        self.created_other: list[tuple[str, dict]] = []
        self.voided: list[tuple[str, str]] = []
        self.voided_financial: list[tuple[str, str]] = []
        self.updated: list[tuple[str, dict]] = []
        self.order_money: list[tuple[str, str, dict]] = []
        self.financial_lists: list[tuple[str, dict]] = []
        self.searched: dict[str, dict] = {}
        self.purchase_quick_return = {
            "sourceOrderId": "PO-1",
            "sourceOrderNo": "PO20260901001",
            "supplierId": "SUP-1",
            "supplierName": "鑫达供货",
            "warehouseId": "WH-1",
            "warehouseName": "一号仓",
            "handlerId": "STAFF-1",
            "handlerName": "张三",
            "returnDate": "2026-09-01",
            "sourcePaidAmount": 0.0,
            "sourcePayableAmount": 10.0,
            "items": [
                {"productId": "P001", "productName": "土豆", "unit": "斤", "quantity": 5, "unitPrice": 2.0},
            ],
        }
        self.sales_quick_return = {
            "sourceOrderId": "SO-1",
            "sourceOrderNo": "SO20260901001",
            "customerId": "CUS-1",
            "customerName": "客户甲",
            "warehouseId": "WH-1",
            "warehouseName": "一号仓",
            "handlerId": "STAFF-1",
            "handlerName": "张三",
            "returnDate": "2026-09-01",
            "refundedAmount": 0.0,
            "unrefundedAmount": 10.0,
            "refundStatus": 0,
            "items": [
                {"productId": "P001", "productName": "土豆", "unit": "斤", "quantity": 5, "unitPrice": 2.0},
            ],
        }
        self.purchase_order = {
            "id": "PO-1",
            "orderNo": "PO20260901001",
            "supplierName": "鑫达供货",
            "totalAmount": 10.0,
            "paidAmount": 0.0,
            "unpaidAmount": 10.0,
        }
        self.sales_order = {
            "id": "SO-1",
            "orderNo": "SO20260901001",
            "customerName": "客户甲",
            "totalAmount": 10.0,
            "receivedAmount": 0.0,
            "unreceivedAmount": 10.0,
        }

    async def fetch_products(self, context, limit=None):
        return YunCybProductSnapshot(products=())

    async def _search(self, reference_type, context, keyword, limit=10, page=1):
        options = self._REFERENCE_OPTIONS[reference_type]
        token = (keyword or "").strip()
        matched = tuple(
            option
            for option in options
            if not token
            or token in {
                str(option.get("id")),
                str(option.get("code")),
                str(option.get("name")),
            }
            or token in str(option.get("name") or "")
        )
        return YunCybReferenceSnapshot(
            options=matched,
            total=len(matched),
            page_num=page,
            page_size=limit,
        )

    async def search_customers(self, context, keyword, limit=10, page=1):
        return await self._search("customer", context, keyword, limit, page)

    async def search_warehouses(self, context, keyword, limit=10, page=1):
        return await self._search("warehouse", context, keyword, limit, page)

    async def search_staff(self, context, keyword, limit=10, page=1):
        return await self._search("handler", context, keyword, limit, page)

    async def search_suppliers(self, context, keyword, limit=10, page=1):
        return await self._search("supplier", context, keyword, limit, page)

    async def search_settlement_accounts(self, context, keyword, limit=10, page=1):
        return await self._search("settlement_account", context, keyword, limit, page)

    _FUND_TYPES = {
        1: (
            {"id": "FT-SALES", "typeCode": "sales", "typeName": "销售收款", "isSystem": True},
            {"id": "FT-PR", "typeCode": "purchase_refund", "typeName": "采购退款收款", "isSystem": True},
            {"id": "FT-SKX", "typeCode": "SKX001", "typeName": "测试收款", "isSystem": False},
        ),
        2: (
            {"id": "FT-PURCHASE", "typeCode": "purchase", "typeName": "采购付款", "isSystem": True},
            {"id": "FT-SR", "typeCode": "sales_refund", "typeName": "销售退款付款", "isSystem": True},
            {"id": "FT-FKX", "typeCode": "FKX001", "typeName": "测试付款", "isSystem": False},
        ),
    }

    async def search_fund_types(self, context, direction):
        rows = self._FUND_TYPES[int(direction)]
        options = tuple(
            {
                "id": str(row["id"]),
                "code": row["typeCode"],
                "name": row["typeName"],
                "is_default": False,
                "is_system": bool(row["isSystem"]),
            }
            for row in rows
        )
        return YunCybReferenceSnapshot(
            options=options,
            total=len(options),
            page_num=1,
            page_size=len(options),
        )

    # -- 采购单 ------------------------------------------------------------

    async def create_purchase_order(self, context, payload):
        self.created.append(("create_purchase_order", payload))
        return YunCybDocumentResult(document_id="PO-DOC-1")

    async def get_purchase_order_detail(self, context, order_id):
        return YunCybDocumentDetailResult(document=dict(self.purchase_order))

    async def get_purchase_order_quick_return(self, context, order_id):
        return YunCybDocumentDetailResult(document=dict(self.purchase_quick_return))

    async def search_purchase_orders(self, context, **filters):
        self.searched["purchase_orders"] = filters
        return YunCybDocumentPageResult(
            total=1,
            page_num=filters.get("page_num", 1),
            page_size=filters.get("page_size", 20),
            rows=({"orderNo": "PO20260901001", "supplierName": "鑫达供货", "totalAmount": 10.0, "status": 2},),
        )

    async def void_purchase_order(self, context, order_id):
        self.voided.append(("void_purchase_order", order_id))

    async def update_purchase_order(self, context, order_id, payload):
        self.updated.append((order_id, payload))
        return YunCybDocumentResult(document_id=order_id)

    # -- 采购退货单 ----------------------------------------------------------

    async def create_purchase_return(self, context, payload):
        self.created.append(("create_purchase_return", payload))
        return YunCybDocumentResult(document_id="PR-DOC-1")

    async def get_purchase_return_detail(self, context, order_id):
        return YunCybDocumentDetailResult(document={"id": "PR-1", "returnNo": "PR20260901001"})

    async def search_purchase_returns(self, context, **filters):
        self.searched["purchase_returns"] = filters
        return YunCybDocumentPageResult(
            total=1,
            page_num=filters.get("page_num", 1),
            page_size=filters.get("page_size", 20),
            rows=({"returnNo": "PR20260901001", "supplierName": "鑫达供货", "totalAmount": 10.0},),
        )

    async def void_purchase_return(self, context, return_id):
        self.voided.append(("void_purchase_return", return_id))

    # -- 销售退货单 ----------------------------------------------------------

    async def get_sales_order_quick_return(self, context, order_id):
        return YunCybDocumentDetailResult(document=dict(self.sales_quick_return))

    async def create_sales_return(self, context, payload):
        self.created.append(("create_sales_return", payload))
        return YunCybDocumentResult(document_id="SR-DOC-1")

    async def get_sales_return_detail(self, context, return_id):
        return YunCybDocumentDetailResult(document={"id": "SR-1", "returnNo": "SR20260901001"})

    async def search_sales_returns(self, context, **filters):
        self.searched["sales_returns"] = filters
        return YunCybDocumentPageResult(
            total=1,
            page_num=filters.get("page_num", 1),
            page_size=filters.get("page_size", 20),
            rows=({"returnNo": "SR20260901001", "customerName": "客户甲", "totalAmount": 10.0},),
        )

    async def void_sales_return(self, context, return_id):
        self.voided.append(("void_sales_return", return_id))

    # -- 销售单继续收款 / 采购单继续付款 --------------------------------------

    async def get_sales_order_detail(self, context, order_id):
        return YunCybSalesOrderDetailResult(order=dict(self.sales_order))

    async def receive_sales_order(self, context, order_id, body):
        self.order_money.append(("receive_sales_order", order_id, body))

    async def pay_purchase_order(self, context, order_id, body):
        self.order_money.append(("pay_purchase_order", order_id, body))

    # -- 库存调拨与其他出入库 -------------------------------------------------

    async def create_stock_transfer(self, context, payload):
        self.created.append(("create_stock_transfer", payload))
        return YunCybDocumentResult(document_id="TR-1")

    async def create_other_stock_doc(self, context, kind, payload):
        self.created_other.append((kind, payload))
        return YunCybDocumentResult(document_id="OS-1")

    async def list_stock_doc_types(self, context, kind):
        data = (
            [{"id": 1, "name": "报溢"}, {"id": 3, "name": "盘盈"}]
            if kind == "inbound"
            else [{"id": 2, "name": "报损"}, {"id": 4, "name": "盘亏"}]
        )
        return YunCybDataResult(data=data)

    # -- 收款单与付款单 -------------------------------------------------------

    async def create_receipt_order(self, context, payload):
        self.created.append(("create_receipt_order", payload))
        return YunCybDocumentResult(document_id="RO-1")

    async def create_payment_order(self, context, payload):
        self.created.append(("create_payment_order", payload))
        return YunCybDocumentResult(document_id="PAY-1")

    async def get_financial_order_detail(self, context, kind, order_id):
        order_no = "SK20260901001" if kind == "receipt" else "FK20260901001"
        return YunCybDocumentDetailResult(document={"id": order_id, "orderNo": order_no})

    async def list_financial_orders(self, context, kind, params):
        self.financial_lists.append((kind, dict(params)))
        return YunCybDocumentPageResult(
            total=1,
            page_num=params["pageNum"],
            page_size=params["pageSize"],
            rows=({"orderNo": "SK20260901001", "amount": 10.0},),
        )

    async def void_financial_order(self, context, kind, order_id):
        self.voided_financial.append((kind, order_id))


def _session(tmp_path, products):
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(
        json.dumps({"products": products}, ensure_ascii=False),
        encoding="utf-8",
    )
    return YunCybSession.from_settings(
        YunCybSettings(
            product_catalog_path=catalog_path,
            alias_path=None,
            recommendation_score=0.60,
            use_default_fresh_aliases=True,
            category_path=None,
            use_default_categories=True,
        ),
    )


def _toolset(tmp_path, products=None, api=None):
    context = InvocationContext(
        tenant_id="tenant-test",
        subject_id="user-test",
        account_id="yuncyb-test",
        session_id="session-test",
        scopes=frozenset({"yuncyb:read", "yuncyb:write"}),
    )
    return YunCybToolSet(
        _session(tmp_path, products if products is not None else _PRODUCTS),
        api or DocumentApi(),
        InvocationContextStore(default=context),
    )


def test_preview_purchase_order_reports_missing_fields(tmp_path):
    result = asyncio.run(_toolset(tmp_path).preview_purchase_order(order_text="土豆2斤"))

    assert result["ok"] is True
    assert [item["field"] for item in result["missing_required_fields"]] == [
        "supplier",
        "warehouse",
        "handler",
        "order_date",
    ]
    assert result["required_actions"] == [
        "provide_supplier",
        "provide_warehouse",
        "provide_handler",
        "provide_order_date",
    ]
    assert result["ready_to_submit"] is False
    assert result["preview_id"] is None


def test_purchase_order_requires_confirmed_price_then_submits(tmp_path):
    api = DocumentApi()
    toolset = _toolset(tmp_path, api=api)

    warned = asyncio.run(toolset.preview_purchase_order(
        order_text="西红柿2斤",
        supplier="鑫达供货",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-09-01",
    ))
    assert warned["ready_to_submit"] is False
    assert [item["line_id"] for item in warned["price_warnings"]] == ["L001"]
    assert "confirm_prices" in warned["required_actions"]
    assert warned["preview_id"] is None

    prepared = asyncio.run(toolset.preview_purchase_order(
        order_text="西红柿2斤",
        supplier="鑫达供货",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-09-01",
        confirmed_prices=[{"line_id": "L001", "product_id": "P002", "unit_price": 2.5}],
    ))
    assert prepared["ready_to_submit"] is True
    assert prepared["required_actions"] == ["confirm_submit"]
    assert prepared["preview"]["total_amount"] == 5.0
    assert prepared["preview_id"] is not None

    rejected = asyncio.run(toolset.submit_purchase_order(
        prepared["preview_id"],
        confirmed_by_user=False,
    ))
    assert rejected["error"]["code"] == "erp_document_confirmation_required"
    assert api.created == []

    submitted = asyncio.run(toolset.submit_purchase_order(
        prepared["preview_id"],
        confirmed_by_user=True,
    ))
    assert submitted["ok"] is True
    assert submitted["submitted"] is True
    assert submitted["document_no"] == "PO20260901001"
    assert submitted["idempotent_replay"] is False
    kind, payload = api.created[-1]
    assert kind == "create_purchase_order"
    assert payload["orderDate"] == "2026-09-01"
    assert payload["supplierId"] == "SUP-1"
    assert payload["warehouseId"] == "WH-1"
    assert payload["handlerId"] == "STAFF-1"
    assert payload["saveType"] == 2
    assert payload["items"] == [
        {"productId": "P002", "quantity": 2, "unitPrice": 2.5, "unit": "斤"},
    ]

    replayed = asyncio.run(toolset.submit_purchase_order(
        prepared["preview_id"],
        confirmed_by_user=True,
    ))
    assert replayed["idempotent_replay"] is True
    assert len(api.created) == 1


def test_purchase_order_zero_catalog_price_requires_confirmation(tmp_path):
    """目录采购价为 0 的商品同样必须先确认单价（ERP 拒绝 0 价采购行）。"""
    api = DocumentApi()
    products = [
        {"id": "P001", "code": "SP001", "name": "啤酒", "unit": "瓶", "purchasePrice": 0.0},
    ]
    toolset = _toolset(tmp_path, products=products, api=api)

    warned = asyncio.run(toolset.preview_purchase_order(
        order_text="啤酒2瓶",
        supplier="鑫达供货",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-09-01",
    ))
    assert warned["ready_to_submit"] is False
    assert [item["line_id"] for item in warned["price_warnings"]] == ["L001"]
    assert warned["preview_id"] is None

    prepared = asyncio.run(toolset.preview_purchase_order(
        order_text="啤酒2瓶",
        supplier="鑫达供货",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-09-01",
        confirmed_prices=[{"line_id": "L001", "product_id": "P001", "unit_price": 1.0}],
    ))
    assert prepared["ready_to_submit"] is True
    assert prepared["preview"]["total_amount"] == 2.0


def test_purchase_order_rejects_zero_confirmed_price(tmp_path):
    """确认单价为 0 时拒绝生成预览，避免 0 价行写入 ERP。"""
    api = DocumentApi()
    products = [
        {"id": "P001", "code": "SP001", "name": "啤酒", "unit": "瓶", "purchasePrice": 0.0},
    ]
    toolset = _toolset(tmp_path, products=products, api=api)

    rejected = asyncio.run(toolset.preview_purchase_order(
        order_text="啤酒2瓶",
        supplier="鑫达供货",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-09-01",
        confirmed_prices=[{"line_id": "L001", "product_id": "P001", "unit_price": 0}],
    ))
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "erp_sales_order_item_invalid"
    assert api.created == []


def test_purchase_order_uses_catalog_purchase_price(tmp_path):
    api = DocumentApi()
    toolset = _toolset(tmp_path, api=api)

    prepared = asyncio.run(toolset.preview_purchase_order(
        order_text="土豆2斤",
        supplier="鑫达供货",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-09-01",
    ))
    assert prepared["ready_to_submit"] is True
    assert prepared["price_warnings"] == []
    assert prepared["preview"]["items"] == [
        {"name": "土豆", "quantity": 2, "unit": "斤", "unit_price": 3.0, "line_amount": 6.0},
    ]
    assert prepared["preview"]["total_amount"] == 6.0

    submitted = asyncio.run(toolset.submit_purchase_order(
        prepared["preview_id"],
        confirmed_by_user=True,
    ))
    assert submitted["ok"] is True
    _, payload = api.created[-1]
    assert payload["items"] == [
        {"productId": "P001", "quantity": 2, "unitPrice": 3.0, "unit": "斤"},
    ]


def test_purchase_return_whole_order_with_refund_money_in_payload(tmp_path):
    api = DocumentApi()
    toolset = _toolset(tmp_path, api=api)

    prepared = asyncio.run(toolset.preview_purchase_return(
        order_id="PO-1",
        refund_amount=4.0,
        refund_account_id="ACC-1",
        discount_amount=1.0,
        discount_account_id="ACC-2",
    ))
    assert prepared["ok"] is True
    assert prepared["ready_to_submit"] is True
    assert prepared["required_actions"] == ["confirm_submit"]
    assert prepared["source_order"]["order_no"] == "PO20260901001"
    assert prepared["items"] == [
        {"product_id": "P001", "product_name": "土豆", "quantity": 5.0, "unit": "斤", "unit_price": 2.0},
    ]
    assert prepared["preview"]["total_amount"] == 10.0
    assert prepared["preview"]["退款金额"] == 4.0
    assert prepared["preview"]["refund_account"] == "现金账户"
    assert prepared["preview"]["discount_amount"] == 1.0

    submitted = asyncio.run(toolset.submit_purchase_return(
        prepared["preview_id"],
        confirmed_by_user=True,
    ))
    assert submitted["ok"] is True
    assert submitted["document_no"] == "PR20260901001"
    kind, payload = api.created[-1]
    assert kind == "create_purchase_return"
    assert payload["sourceOrderId"] == "PO-1"
    assert payload["supplierId"] == "SUP-1"
    assert payload["warehouseId"] == "WH-1"
    assert payload["saveType"] == 2
    assert payload["sourcePaidAmount"] == 0.0
    assert payload["sourcePayableAmount"] == 10.0
    assert payload["paymentAmount"] == 4.0
    assert payload["paymentAccountId"] == "ACC-1"
    assert payload["paymentAccountName"] == "现金账户"
    assert payload["discountAmount"] == 1.0
    assert payload["discountAccountId"] == "ACC-2"
    assert payload["discountAccountName"] == "微信账户"
    assert payload["items"] == [
        {"productId": "P001", "quantity": 5.0, "unitPrice": 2.0, "unit": "斤"},
    ]


def test_purchase_return_rejects_invalid_items_and_money(tmp_path):
    toolset = _toolset(tmp_path)

    over = asyncio.run(toolset.preview_purchase_return(
        "PO-1",
        items=[{"product_id": "P001", "quantity": 6}],
    ))
    assert over["error"]["code"] == "erp_return_items_invalid"

    unknown = asyncio.run(toolset.preview_purchase_return(
        "PO-1",
        items=[{"product_id": "P999", "quantity": 1}],
    ))
    assert unknown["error"]["code"] == "erp_return_items_invalid"

    missing_account = asyncio.run(toolset.preview_purchase_return("PO-1", refund_amount=4.0))
    assert missing_account["error"]["code"] == "erp_return_money_invalid"

    exceed = asyncio.run(toolset.preview_purchase_return(
        "PO-1",
        refund_amount=8.0,
        refund_account_id="ACC-1",
        discount_amount=3.0,
        discount_account_id="ACC-2",
    ))
    assert exceed["error"]["code"] == "erp_return_money_invalid"


def test_sales_return_partial_items_with_refund_in_payload(tmp_path):
    api = DocumentApi()
    toolset = _toolset(tmp_path, api=api)

    prepared = asyncio.run(toolset.preview_sales_return(
        order_id="SO-1",
        items=[{"product_id": "P001", "quantity": 2}],
        refund_amount=4.0,
        refund_account_id="ACC-1",
    ))
    assert prepared["ready_to_submit"] is True
    assert prepared["source_order"]["customer"] == "客户甲"
    assert prepared["source_order"]["unrefunded_amount"] == 10.0
    assert prepared["preview"]["total_amount"] == 4.0
    assert prepared["preview"]["退款金额"] == 4.0

    submitted = asyncio.run(toolset.submit_sales_return(
        prepared["preview_id"],
        confirmed_by_user=True,
    ))
    assert submitted["ok"] is True
    assert submitted["document_no"] == "SR20260901001"
    kind, payload = api.created[-1]
    assert kind == "create_sales_return"
    assert payload["customerId"] == "CUS-1"
    assert payload["warehouseId"] == "WH-1"
    assert payload["saveType"] == 2
    assert payload["unrefundedAmount"] == 10.0
    assert payload["paymentAmount"] == 4.0
    assert payload["paymentAccountId"] == "ACC-1"
    assert payload["paymentAccountName"] == "现金账户"
    assert payload["items"] == [
        {"productId": "P001", "quantity": 2.0, "unitPrice": 2.0, "unit": "斤"},
    ]

    listed = asyncio.run(toolset.list_sales_returns(customer_id="客户甲"))
    assert listed["ok"] is True
    assert api.searched["sales_returns"]["customer_id"] == "CUS-1"


def test_sales_receipt_amount_guard_and_submit_body(tmp_path):
    api = DocumentApi()
    toolset = _toolset(tmp_path, api=api)

    exceed = asyncio.run(toolset.preview_sales_receipt("SO-1", 12.0, "现金账户"))
    assert exceed["error"]["code"] == "erp_sales_receipt_amount_invalid"

    zero = asyncio.run(toolset.preview_sales_receipt("SO-1", 0, "现金账户"))
    assert zero["error"]["code"] == "erp_sales_receipt_amount_invalid"

    unmatched = asyncio.run(toolset.preview_sales_receipt("SO-1", 5.0, "不存在的账户"))
    assert unmatched["ready_to_submit"] is False
    assert unmatched["required_actions"] == ["select_receipt_account"]
    assert unmatched["preview_id"] is None

    prepared = asyncio.run(toolset.preview_sales_receipt(
        "SO-1",
        8.0,
        "现金账户",
        discount_amount=2.0,
        discount_account="微信账户",
    ))
    assert prepared["ready_to_submit"] is True
    assert prepared["required_actions"] == ["confirm_submit"]
    assert prepared["order"]["unreceived_amount"] == 10.0
    assert prepared["preview"]["receipt_amount"] == 8.0
    assert prepared["preview"]["discount_amount"] == 2.0

    rejected = asyncio.run(toolset.submit_sales_receipt(
        prepared["preview_id"],
        confirmed_by_user=False,
    ))
    assert rejected["error"]["code"] == "erp_document_confirmation_required"
    assert api.order_money == []

    submitted = asyncio.run(toolset.submit_sales_receipt(
        prepared["preview_id"],
        confirmed_by_user=True,
    ))
    assert submitted["ok"] is True
    assert submitted["document_id"] == "SO-1"
    assert api.order_money == [
        (
            "receive_sales_order",
            "SO-1",
            {
                "receiptAmount": 8.0,
                "receiptAccountId": "ACC-1",
                "discountAmount": 2.0,
                "discountAccountId": "ACC-2",
            },
        ),
    ]


def test_purchase_payment_preview_and_submit(tmp_path):
    api = DocumentApi()
    toolset = _toolset(tmp_path, api=api)

    prepared = asyncio.run(toolset.preview_purchase_payment("PO-1", 6.0, "微信账户"))
    assert prepared["ready_to_submit"] is True
    assert prepared["required_actions"] == ["confirm_submit"]
    assert prepared["order"]["unpaid_amount"] == 10.0
    assert prepared["preview"]["payment_amount"] == 6.0

    submitted = asyncio.run(toolset.submit_purchase_payment(
        prepared["preview_id"],
        confirmed_by_user=True,
    ))
    assert submitted["ok"] is True
    assert submitted["document_id"] == "PO-1"
    assert api.order_money == [
        ("pay_purchase_order", "PO-1", {"paymentAmount": 6.0, "paymentAccountId": "ACC-2"}),
    ]


def test_stock_transfer_rejects_same_warehouse(tmp_path):
    result = asyncio.run(_toolset(tmp_path).preview_stock_transfer(
        order_text="土豆2斤",
        from_warehouse="一号仓",
        to_warehouse="一号仓",
        handler="张三",
    ))
    assert result["error"]["code"] == "erp_stock_transfer_warehouse_invalid"


def test_stock_transfer_ready_and_submit(tmp_path):
    api = DocumentApi()
    toolset = _toolset(tmp_path, api=api)

    prepared = asyncio.run(toolset.preview_stock_transfer(
        order_text="土豆2斤",
        from_warehouse="一号仓",
        to_warehouse="二号仓",
        handler="张三",
        transfer_date="2026-09-01",
    ))
    assert prepared["ready_to_submit"] is True
    assert prepared["required_actions"] == ["confirm_submit"]
    assert prepared["preview"]["from_warehouse"] == "一号仓"
    assert prepared["preview"]["to_warehouse"] == "二号仓"
    assert prepared["preview"]["total_quantity"] == 2.0
    assert prepared["preview"]["items"] == [{"name": "土豆", "quantity": 2, "unit": "斤"}]

    submitted = asyncio.run(toolset.submit_stock_transfer(
        prepared["preview_id"],
        confirmed_by_user=True,
    ))
    assert submitted["ok"] is True
    assert submitted["document_no"] == "TR-1"
    kind, payload = api.created[-1]
    assert kind == "create_stock_transfer"
    assert payload["transferDate"] == "2026-09-01"
    assert payload["fromWarehouseId"] == "WH-1"
    assert payload["toWarehouseId"] == "WH-2"
    assert payload["handlerId"] == "STAFF-1"
    assert payload["saveType"] == 2
    assert payload["items"] == [{"productId": "P001", "quantity": 2, "unit": "斤"}]


def test_other_stock_doc_resolves_doc_type_and_submits(tmp_path):
    api = DocumentApi()
    toolset = _toolset(tmp_path, api=api)

    invalid = asyncio.run(toolset.preview_other_stock_doc(
        kind="bad",
        order_text="土豆2斤",
        warehouse="一号仓",
        handler="张三",
        doc_type="报损",
    ))
    assert invalid["error"]["code"] == "erp_stock_doc_type_invalid"

    prepared = asyncio.run(toolset.preview_other_stock_doc(
        kind="outbound",
        order_text="土豆2斤",
        warehouse="一号仓",
        handler="张三",
        doc_type="报损",
        doc_date="2026-09-01",
    ))
    assert prepared["ok"] is True
    assert prepared["doc_label"] == "其他出库单"
    assert prepared["ready_to_submit"] is True
    assert prepared["preview"]["doc_type"] == "报损"

    submitted = asyncio.run(toolset.submit_other_stock_doc(
        prepared["preview_id"],
        confirmed_by_user=True,
    ))
    assert submitted["ok"] is True
    kind, payload = api.created_other[-1]
    assert kind == "outbound"
    assert payload["outboundDate"] == "2026-09-01"
    assert payload["outboundType"] == 2
    assert payload["warehouseId"] == "WH-1"
    assert payload["handlerId"] == "STAFF-1"
    assert payload["saveType"] == 2
    assert payload["items"] == [{"productId": "P001", "quantity": 2, "unit": "斤"}]

    by_digit = asyncio.run(toolset.preview_other_stock_doc(
        kind="inbound",
        order_text="土豆2斤",
        warehouse="一号仓",
        handler="张三",
        doc_type="1",
    ))
    assert by_digit["ready_to_submit"] is True
    submitted_digit = asyncio.run(toolset.submit_other_stock_doc(
        by_digit["preview_id"],
        confirmed_by_user=True,
    ))
    assert submitted_digit["ok"] is True
    digit_kind, digit_payload = api.created_other[-1]
    assert digit_kind == "inbound"
    assert digit_payload["inboundType"] == 1
    assert digit_payload["inboundDate"] == by_digit["preview"]["doc_date"]


def test_financial_order_preview_validation_rules(tmp_path):
    toolset = _toolset(tmp_path)

    xor = asyncio.run(toolset.preview_receipt_order(
        receipt_amount=10.0,
        account="现金账户",
        handler="张三",
        customer="客户甲",
        supplier="鑫达供货",
    ))
    assert xor["error"]["code"] == "erp_financial_order_counterparty_invalid"

    zero = asyncio.run(toolset.preview_receipt_order(0, "现金账户", "张三"))
    assert zero["error"]["code"] == "erp_receipt_order_amount_invalid"

    writeoff_without_customer = asyncio.run(toolset.preview_receipt_order(
        receipt_amount=10.0,
        account="现金账户",
        handler="张三",
        writeoff_details=[{"biz_type": "sales_order", "biz_id": "SO-1", "writeoff_amount": 5.0}],
    ))
    assert writeoff_without_customer["error"]["code"] == "erp_financial_order_counterparty_required"

    writeoff_exceed = asyncio.run(toolset.preview_receipt_order(
        receipt_amount=10.0,
        account="现金账户",
        handler="张三",
        customer="客户甲",
        writeoff_details=[{"biz_type": "sales_order", "biz_id": "SO-1", "writeoff_amount": 10.01}],
    ))
    assert writeoff_exceed["error"]["code"] == "erp_writeoff_details_invalid"

    writeoff_bad_type = asyncio.run(toolset.preview_receipt_order(
        receipt_amount=10.0,
        account="现金账户",
        handler="张三",
        customer="客户甲",
        writeoff_details=[{"biz_type": "other", "biz_id": "X", "writeoff_amount": 1.0}],
    ))
    assert writeoff_bad_type["error"]["code"] == "erp_writeoff_details_invalid"


def test_receipt_order_lifecycle_with_writeoff(tmp_path):
    api = DocumentApi()
    toolset = _toolset(tmp_path, api=api)

    prepared = asyncio.run(toolset.preview_receipt_order(
        receipt_amount=10.0,
        account="现金账户",
        handler="张三",
        customer="客户甲",
        order_date="2026-09-01",
        writeoff_details=[{"biz_type": "sales_order", "biz_id": "SO-1", "writeoff_amount": 7.0}],
        remark="预收款",
    ))
    assert prepared["ready_to_submit"] is True
    assert prepared["required_actions"] == ["confirm_submit"]
    assert prepared["preview"]["counterparty_label"] == "客户"
    assert prepared["preview"]["客户"] == "客户甲"
    assert prepared["preview"]["fund_type"] == "销售收款"
    assert prepared["preview"]["writeoff_details"] == [
        {"biz_type": "sales_order", "biz_id": "SO-1", "writeoff_amount": 7.0},
    ]

    rejected = asyncio.run(toolset.submit_receipt_order(
        prepared["preview_id"],
        confirmed_by_user=False,
    ))
    assert rejected["error"]["code"] == "erp_document_confirmation_required"

    submitted = asyncio.run(toolset.submit_receipt_order(
        prepared["preview_id"],
        confirmed_by_user=True,
    ))
    assert submitted["ok"] is True
    assert submitted["document_no"] == "SK20260901001"
    kind, payload = api.created[-1]
    assert kind == "create_receipt_order"
    assert payload["orderDate"] == "2026-09-01"
    assert payload["accountId"] == "ACC-1"
    assert payload["handlerId"] == "STAFF-1"
    assert payload["typeId"] == "FT-SALES"
    assert payload["counterpartyType"] == "customer"
    assert payload["customerId"] == "CUS-1"
    assert payload["receiptAmount"] == 10.0
    assert payload["saveType"] == 2
    assert payload["writeoffDetails"] == [
        {"bizType": "sales_order", "bizId": "SO-1", "writeoffAmount": 7.0},
    ]
    assert payload["remark"] == "预收款"

    void_rejected = asyncio.run(toolset.void_receipt_order("SK-1", confirmed_by_user=False))
    assert void_rejected["error"]["code"] == "erp_document_confirmation_required"
    assert api.voided_financial == []

    voided = asyncio.run(toolset.void_receipt_order("SK-1", confirmed_by_user=True))
    assert voided == {"ok": True, "voided": True, "document_no": "SK20260901001"}
    assert api.voided_financial == [("receipt", "SK-1")]


def test_payment_order_lifecycle_without_counterparty(tmp_path):
    api = DocumentApi()
    toolset = _toolset(tmp_path, api=api)

    # 无核销且未提供款项类型：返回候选并要求补参
    no_fund_type = asyncio.run(toolset.preview_payment_order(
        payment_amount=5.0,
        account="微信账户",
        handler="张三",
        supplier="鑫达供货",
    ))
    assert no_fund_type["ready_to_submit"] is False
    assert "provide_fund_type" in no_fund_type["required_actions"]
    assert [option["name"] for option in no_fund_type["reference_resolutions"]["fund_type"]["candidates"]] == [
        "采购付款", "销售退款付款", "测试付款",
    ]
    assert no_fund_type["preview_id"] is None

    with_supplier = asyncio.run(toolset.preview_payment_order(
        payment_amount=5.0,
        account="微信账户",
        handler="张三",
        supplier="鑫达供货",
        fund_type="测试付款",
    ))
    assert with_supplier["ready_to_submit"] is True
    assert with_supplier["preview"]["fund_type"] == "测试付款"
    submitted = asyncio.run(toolset.submit_payment_order(
        with_supplier["preview_id"],
        confirmed_by_user=True,
    ))
    assert submitted["ok"] is True
    kind, payload = api.created[-1]
    assert kind == "create_payment_order"
    assert payload["counterpartyType"] == "supplier"
    assert payload["supplierId"] == "SUP-1"
    assert payload["paymentAmount"] == 5.0
    assert payload["accountId"] == "ACC-2"
    assert payload["typeId"] == "FT-FKX"

    no_counterparty = asyncio.run(toolset.preview_payment_order(
        payment_amount=5.0,
        account="现金账户",
        handler="张三",
        fund_type="FKX001",
    ))
    assert no_counterparty["ready_to_submit"] is True
    submitted = asyncio.run(toolset.submit_payment_order(
        no_counterparty["preview_id"],
        confirmed_by_user=True,
    ))
    assert submitted["ok"] is True
    _, payload = api.created[-1]
    assert payload["skipSupplierValidation"] is True
    assert payload["typeId"] == "FT-FKX"


def test_void_and_update_purchase_order_require_confirmation(tmp_path):
    api = DocumentApi()
    toolset = _toolset(tmp_path, api=api)

    void_rejected = asyncio.run(toolset.void_purchase_order("PO-1", confirmed_by_user=False))
    assert void_rejected["error"]["code"] == "erp_document_confirmation_required"

    update_rejected = asyncio.run(toolset.update_purchase_order(
        "PO-1",
        remark="新备注",
        confirmed_by_user=False,
    ))
    assert update_rejected["error"]["code"] == "erp_document_confirmation_required"
    assert api.updated == []

    update_empty = asyncio.run(toolset.update_purchase_order("PO-1", confirmed_by_user=True))
    assert update_empty["error"]["code"] == "erp_purchase_order_update_empty"

    updated = asyncio.run(toolset.update_purchase_order(
        "PO-1",
        remark="新备注",
        supplier_id="鑫达供货",
        items=[{"product_id": "P001", "quantity": 3, "unit_price": 2.0}],
        confirmed_by_user=True,
    ))
    assert updated == {"ok": True, "modified": True, "document_no": "PO20260901001"}
    order_id, payload = api.updated[-1]
    assert order_id == "PO-1"
    assert payload == {
        "id": "PO-1",
        "remark": "新备注",
        "supplierId": "SUP-1",
        "items": [{"productId": "P001", "quantity": 3.0, "unitPrice": 2.0}],
    }

    voided = asyncio.run(toolset.void_purchase_order("PO-1", confirmed_by_user=True))
    assert voided == {"ok": True, "voided": True, "document_no": "PO20260901001"}
    assert api.voided == [("void_purchase_order", "PO-1")]


def test_document_lists_resolve_references(tmp_path):
    api = DocumentApi()
    toolset = _toolset(tmp_path, api=api)

    orders = asyncio.run(toolset.list_purchase_orders(supplier_id="鑫达供货", status=2))
    assert orders["ok"] is True
    assert orders["page"] == 1
    assert orders["total"] == 1
    assert orders["has_more"] is False
    assert orders["documents"] == [
        {"orderNo": "PO20260901001", "supplierName": "鑫达供货", "totalAmount": 10.0, "status": 2},
    ]
    assert api.searched["purchase_orders"]["supplier_id"] == "SUP-1"
    assert api.searched["purchase_orders"]["status"] == 2

    receipts = asyncio.run(toolset.list_receipt_orders(
        counterparty_id="客户甲",
        account_id="现金账户",
    ))
    assert receipts["ok"] is True
    kind, params = api.financial_lists[-1]
    assert kind == "receipt"
    assert params["customerId"] == "CUS-1"
    assert params["accountId"] == "ACC-1"
    assert params["pageNum"] == 1
    assert params["pageSize"] == 20

    unmatched = asyncio.run(toolset.list_payment_orders(counterparty_id="不存在的单位"))
    assert unmatched["error"]["code"] == "erp_reference_unmatched"

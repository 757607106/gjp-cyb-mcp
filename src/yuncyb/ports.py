"""开单产品业务 API 端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from gjp_common.context import InvocationContext


@dataclass(frozen=True)
class YunCybProductSnapshot:
    """一次商品目录同步结果。"""

    products: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class YunCybReferenceSnapshot:
    """客户、仓库或职员分页查询结果。"""

    options: tuple[dict[str, Any], ...]
    total: int
    page_num: int
    page_size: int


@dataclass(frozen=True)
class YunCybSalesOrderResult:
    """ERP 新增或修改销售单的最小返回结果。"""

    order_id: str


@dataclass(frozen=True)
class YunCybSalesOrderDetailResult:
    """ERP 销售单详情查询结果。"""

    order: dict[str, Any]


@dataclass(frozen=True)
class YunCybSalesOrderPageResult:
    """ERP 销售单分页查询结果。"""

    total: int
    page_num: int
    page_size: int
    orders: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class YunCybDocumentResult:
    """ERP 新增或修改各类业务单据的最小返回结果。"""

    document_id: str


@dataclass(frozen=True)
class YunCybDocumentDetailResult:
    """ERP 业务单据详情或预填数据查询结果。"""

    document: dict[str, Any]


@dataclass(frozen=True)
class YunCybDocumentPageResult:
    """ERP 业务单据分页查询结果。"""

    total: int
    page_num: int
    page_size: int
    rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class YunCybDataResult:
    """ERP 库存、预警、报表等通用数据查询结果。"""

    data: Any


class AuthenticatedJsonClient(Protocol):
    """由对接产品实现的已鉴权 JSON 请求执行器。"""

    async def get_json(
        self,
        context: InvocationContext,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...

    async def post_json(
        self,
        context: InvocationContext,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    async def put_json(
        self,
        context: InvocationContext,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...


class YunCybApiPort(Protocol):
    """完整销售单流程所需的已鉴权业务 API 端口。"""

    async def fetch_products(
        self,
        context: InvocationContext,
        limit: int | None = None,
    ) -> YunCybProductSnapshot:
        ...

    async def search_customers(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 5,
        page: int = 1,
    ) -> YunCybReferenceSnapshot:
        ...

    async def search_warehouses(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 5,
        page: int = 1,
    ) -> YunCybReferenceSnapshot:
        ...

    async def search_staff(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 5,
        page: int = 1,
    ) -> YunCybReferenceSnapshot:
        ...

    async def create_sales_order(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> YunCybSalesOrderResult:
        ...

    async def get_sales_order_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> YunCybSalesOrderDetailResult:
        ...

    async def search_sales_orders(
        self,
        context: InvocationContext,
        *,
        page_num: int = 1,
        page_size: int = 20,
        sort_by: str = "",
        order_type: str = "",
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        payment_status: int | None = None,
        return_status: int | None = None,
        order_no: str = "",
        customer_id: str = "",
    ) -> YunCybSalesOrderPageResult:
        ...

    async def void_sales_order(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> None:
        ...

    async def update_sales_order(
        self,
        context: InvocationContext,
        order_id: str,
        payload: dict[str, Any],
    ) -> YunCybSalesOrderResult:
        """接受显式修改字段；适配器负责保留未传字段并映射 ERP 完整 PUT。"""
        ...

    async def search_suppliers(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 5,
        page: int = 1,
    ) -> YunCybReferenceSnapshot:
        ...

    async def search_settlement_accounts(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 5,
        page: int = 1,
        account_type: int | None = None,
    ) -> YunCybReferenceSnapshot:
        ...

    async def search_fund_types(
        self,
        context: InvocationContext,
        direction: int,
    ) -> YunCybReferenceSnapshot:
        """按资金方向查询款项类型；direction 1=收款性质，2=付款性质。"""
        ...

    async def create_purchase_order(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> YunCybDocumentResult:
        ...

    async def get_purchase_order_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> YunCybDocumentDetailResult:
        ...

    async def search_purchase_orders(
        self,
        context: InvocationContext,
        *,
        page_num: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        payment_status: int | None = None,
        return_status: int | None = None,
        order_no: str = "",
        supplier_id: str = "",
    ) -> YunCybDocumentPageResult:
        ...

    async def void_purchase_order(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> None:
        ...

    async def update_purchase_order(
        self,
        context: InvocationContext,
        order_id: str,
        payload: dict[str, Any],
    ) -> YunCybDocumentResult:
        ...

    async def pay_purchase_order(
        self,
        context: InvocationContext,
        order_id: str,
        payload: dict[str, Any],
    ) -> None:
        ...

    async def get_purchase_order_quick_return(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> YunCybDocumentDetailResult:
        ...

    async def create_purchase_return(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> YunCybDocumentResult:
        ...

    async def get_purchase_return_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> YunCybDocumentDetailResult:
        ...

    async def search_purchase_returns(
        self,
        context: InvocationContext,
        *,
        page_num: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        return_no: str = "",
        supplier_id: str = "",
    ) -> YunCybDocumentPageResult:
        ...

    async def void_purchase_return(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> None:
        ...

    async def get_sales_order_quick_return(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> YunCybDocumentDetailResult:
        ...

    async def create_sales_return(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> YunCybDocumentResult:
        ...

    async def get_sales_return_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> YunCybDocumentDetailResult:
        ...

    async def search_sales_returns(
        self,
        context: InvocationContext,
        *,
        page_num: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        refund_status: int | None = None,
        return_no: str = "",
        customer_id: str = "",
    ) -> YunCybDocumentPageResult:
        ...

    async def void_sales_return(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> None:
        ...

    async def query_stock_page(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> YunCybDataResult:
        ...

    async def get_stock_by_product(
        self,
        context: InvocationContext,
        product_id: str,
    ) -> YunCybDataResult:
        ...

    async def get_stock_summary(
        self,
        context: InvocationContext,
        params: dict[str, Any],
    ) -> YunCybDataResult:
        ...

    async def query_stock_logs(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> YunCybDataResult:
        ...

    async def list_stock_alerts(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> YunCybDataResult:
        ...

    async def get_purchase_suggestions(
        self,
        context: InvocationContext,
    ) -> YunCybDataResult:
        ...

    async def list_stock_doc_types(
        self,
        context: InvocationContext,
        kind: str,
    ) -> YunCybDataResult:
        ...

    async def create_stock_transfer(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> YunCybDocumentResult:
        ...

    async def get_stock_transfer_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> YunCybDocumentDetailResult:
        ...

    async def create_other_stock_doc(
        self,
        context: InvocationContext,
        kind: str,
        payload: dict[str, Any],
    ) -> YunCybDocumentResult:
        ...

    async def get_other_stock_doc_detail(
        self,
        context: InvocationContext,
        kind: str,
        order_id: str,
    ) -> YunCybDocumentDetailResult:
        ...

    async def receive_sales_order(
        self,
        context: InvocationContext,
        order_id: str,
        payload: dict[str, Any],
    ) -> None:
        ...

    async def create_receipt_order(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> YunCybDocumentResult:
        ...

    async def create_payment_order(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> YunCybDocumentResult:
        ...

    async def get_financial_order_detail(
        self,
        context: InvocationContext,
        kind: str,
        order_id: str,
    ) -> YunCybDocumentDetailResult:
        ...

    async def list_financial_orders(
        self,
        context: InvocationContext,
        kind: str,
        params: dict[str, Any],
    ) -> YunCybDocumentPageResult:
        ...

    async def void_financial_order(
        self,
        context: InvocationContext,
        kind: str,
        order_id: str,
    ) -> None:
        ...

    async def list_receivables(
        self,
        context: InvocationContext,
        view: str,
        params: dict[str, Any],
    ) -> YunCybDataResult:
        ...

    async def list_payables(
        self,
        context: InvocationContext,
        view: str,
        params: dict[str, Any],
    ) -> YunCybDataResult:
        ...

    async def query_sales_report(
        self,
        context: InvocationContext,
        view: str,
        params: dict[str, Any],
    ) -> YunCybDataResult:
        ...

    async def query_purchase_report(
        self,
        context: InvocationContext,
        view: str,
        params: dict[str, Any],
    ) -> YunCybDataResult:
        ...

    async def query_profit_report(
        self,
        context: InvocationContext,
        view: str,
        params: dict[str, Any],
    ) -> YunCybDataResult:
        ...

    async def get_financial_status(
        self,
        context: InvocationContext,
        biz_date: str = "",
    ) -> YunCybDataResult:
        ...

    async def query_settlement_report(
        self,
        context: InvocationContext,
        params: dict[str, Any],
    ) -> YunCybDataResult:
        ...

    async def query_reconciliation(
        self,
        context: InvocationContext,
        view: str,
        params: dict[str, Any],
    ) -> YunCybDataResult:
        ...


@dataclass(frozen=True)
class MatchEvent:
    """一次开单匹配的最终确认结果，用于离线挖掘同义词候选。"""

    source: str
    requested_name: str
    product_id: str
    product_name: str
    match_type: str


class MatchEventLogger(Protocol):
    """匹配事件旁路日志端口：记录"搜X→确认Y"语料，不参与匹配主流程。"""

    def record(self, event: MatchEvent) -> None:
        ...

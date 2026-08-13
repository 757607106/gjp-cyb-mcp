"""开单产品业务 API 端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from gjp_common.context import InvocationContext


@dataclass(frozen=True)
class BillingProductSnapshot:
    """一次商品目录同步结果。"""

    products: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class BillingReferenceSnapshot:
    """客户、仓库或职员分页查询结果。"""

    options: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class BillingSalesOrderResult:
    """ERP 新增或修改销售单的最小返回结果。"""

    order_id: str


@dataclass(frozen=True)
class BillingSalesOrderDetailResult:
    """ERP 销售单详情查询结果。"""

    order: dict[str, Any]


@dataclass(frozen=True)
class BillingSalesOrderPageResult:
    """ERP 销售单分页查询结果。"""

    total: int
    page_num: int
    page_size: int
    orders: tuple[dict[str, Any], ...]


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


class BillingApiPort(Protocol):
    """完整销售单流程所需的已鉴权业务 API 端口。"""

    async def fetch_products(
        self,
        context: InvocationContext,
        limit: int | None = None,
    ) -> BillingProductSnapshot:
        ...

    async def search_customers(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 10,
    ) -> BillingReferenceSnapshot:
        ...

    async def search_warehouses(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 10,
    ) -> BillingReferenceSnapshot:
        ...

    async def search_staff(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 10,
    ) -> BillingReferenceSnapshot:
        ...

    async def create_sales_order(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> BillingSalesOrderResult:
        ...

    async def get_sales_order_detail(
        self,
        context: InvocationContext,
        order_id: str,
    ) -> BillingSalesOrderDetailResult:
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
    ) -> BillingSalesOrderPageResult:
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
    ) -> BillingSalesOrderResult:
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

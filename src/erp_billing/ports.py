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
    """ERP 新增销售单的最小返回结果。"""

    order_id: str


class AuthenticatedJsonClient(Protocol):
    """由对接产品实现的已鉴权 JSON 请求执行器。"""

    def get_json(
        self,
        context: InvocationContext,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ...

    def post_json(
        self,
        context: InvocationContext,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        ...


class BillingApiPort(Protocol):
    """完整销售单流程所需的已鉴权业务 API 端口。"""

    def fetch_products(
        self,
        context: InvocationContext,
        limit: int | None = None,
    ) -> BillingProductSnapshot:
        ...

    def search_customers(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 10,
    ) -> BillingReferenceSnapshot:
        ...

    def search_warehouses(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 10,
    ) -> BillingReferenceSnapshot:
        ...

    def search_staff(
        self,
        context: InvocationContext,
        keyword: str,
        limit: int = 10,
    ) -> BillingReferenceSnapshot:
        ...

    def create_sales_order(
        self,
        context: InvocationContext,
        payload: dict[str, Any],
    ) -> BillingSalesOrderResult:
        ...

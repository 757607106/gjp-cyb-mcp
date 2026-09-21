"""库存、往来与报表的只读查询工具。

这些工具无副作用，直接发布给模型；统一收拢分页与日期范围参数，
避免大结果集。实现为 BillingToolSet 的混入类，与销售单工具共用
session、API 端口与基础资料解析。
"""

from __future__ import annotations

from typing import Any

from gjp_common.context import InvocationContextStore
from gjp_common.errors import DomainError
from gjp_common.tools import SessionFunctionTool
from .ports import BillingApiPort
from .session import ErpBillingSession

_ERROR_OUTPUT_OBJECT = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
    },
    "additionalProperties": True,
}

# 查询类工具的通用输出：顶层 ok + data，分页结果另带 page 元数据
_QUERY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "data": {},
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
        "total": {"type": "integer"},
        "has_more": {"type": "boolean"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_STOCK_ALERT_TYPES = {1: "库存不足", 2: "库存积压", 3: "负库存"}

_QUERY_STOCK_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "keyword": {"type": "string", "description": "商品名称模糊关键词"},
        "warehouse_id": {"type": "string", "description": "仓库内部 ID 或名称"},
        "stock_status": {"type": "integer", "enum": [0, 1, 2, 3]},
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
}

_QUERY_STOCK_LOGS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "product_id": {"type": "string", "description": "商品内部 ID"},
        "keyword": {"type": "string", "description": "商品名称模糊关键词"},
        "warehouse_id": {"type": "string", "description": "仓库内部 ID 或名称"},
        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
}

_LIST_STOCK_ALERTS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "alert_type": {"type": "integer", "enum": [1, 2, 3]},
        "keyword": {"type": "string", "description": "商品名称模糊关键词"},
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
}

_LIST_STOCK_DOC_TYPES_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["inbound", "outbound"],
            "description": "inbound=入库类型，outbound=出库类型",
        },
    },
    "required": ["kind"],
}

_LIST_RECEIVABLES_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "view": {"type": "string", "enum": ["summary", "details"]},
        "customer_id": {"type": "string"},
        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
    "required": ["view"],
}

_LIST_PAYABLES_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "view": {"type": "string", "enum": ["summary", "details"]},
        "supplier_id": {"type": "string"},
        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
    "required": ["view"],
}

_GET_FINANCIAL_STATUS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "biz_date": {"type": "string", "description": "业务日期 YYYY-MM-DD，默认当天"},
    },
}

_SALES_REPORT_VIEWS = {
    "analysis": "销售分析",
    "details": "销售明细",
    "details_summary": "销售明细汇总",
    "ranking_product": "商品销量排行",
    "ranking_customer": "客户销量排行",
}

_PURCHASE_REPORT_VIEWS = {
    "statistics": "采购统计",
    "details": "采购明细",
    "details_summary": "采购明细汇总",
}

_PROFIT_REPORT_VIEWS = {
    "summary": "利润汇总",
    "by_customer": "按客户利润",
    "by_product": "按商品利润",
}

_REPORT_INPUT_SCHEMAS = {
    "sales": {
        "type": "object",
        "properties": {
            "view": {"type": "string", "enum": sorted(_SALES_REPORT_VIEWS)},
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            "customer_id": {"type": "string"},
            "handler_id": {"type": "string"},
            "page": {"type": "integer", "minimum": 1, "default": 1},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        "required": ["view"],
    },
    "purchase": {
        "type": "object",
        "properties": {
            "view": {"type": "string", "enum": sorted(_PURCHASE_REPORT_VIEWS)},
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            "supplier_id": {"type": "string"},
            "page": {"type": "integer", "minimum": 1, "default": 1},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        "required": ["view"],
    },
    "profit": {
        "type": "object",
        "properties": {
            "view": {"type": "string", "enum": sorted(_PROFIT_REPORT_VIEWS)},
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            "customer_id": {"type": "string"},
            "page": {"type": "integer", "minimum": 1, "default": 1},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        },
        "required": ["view"],
    },
}

_QUERY_SETTLEMENT_REPORT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
        "sort_by": {"type": "string"},
        "order_type": {"type": "string", "enum": ["asc", "desc"]},
    },
}

_QUERY_RECONCILIATION_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "view": {
            "type": "string",
            "enum": ["summary", "statement"],
            "description": "summary=客户对账报表，statement=客户对账单明细",
        },
        "customer_id": {"type": "string", "description": "客户内部 ID；statement 视图必填"},
        "start_date": {"type": "string", "description": "YYYY-MM-DD，statement 视图可用"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD，statement 视图可用"},
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
    "required": ["view"],
}


class QueryTools:
    """库存查询、库存预警、往来账与报表分析工具（只读）。

    以混入类并入 BillingToolSet：session、_api、_contexts 与
    ok_response 等基础设施由宿主 ToolSet 提供。
    """

    session: ErpBillingSession
    _api: BillingApiPort
    _contexts: InvocationContextStore

    async def query_stock(
        self,
        keyword: str = "",
        warehouse_id: str = "",
        stock_status: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """分页查询商品库存，支持按商品关键词、仓库和库存状态筛选。

        用户问"某商品还有多少""某仓库有什么货"时用此工具。

        Args:
            keyword: 商品名称模糊关键词。
            warehouse_id: 仓库内部 ID 或名称。
            stock_status: 库存状态：0=全部 1=正常 2=零库存 3=负库存。
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            payload: dict[str, Any] = {}
            if keyword.strip():
                payload["productName"] = keyword.strip()
            warehouse = await self._optional_reference_id("warehouse", warehouse_id)
            if warehouse:
                payload["warehouseIds"] = [warehouse]
            if stock_status is not None:
                payload["stockStatus"] = int(stock_status)
            result = await self._api.query_stock_page(context, self._with_page(payload, page, page_size))
            return self._paged_data_response(result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def get_stock_by_product(self, product_id: str) -> dict[str, Any]:
        """查询单个商品的库存分布（各仓库库存量）。

        Args:
            product_id: 商品内部 ID；不是商品名称，名称需先用
                search_products 匹配到商品后取其 product_id。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if not product_id.strip():
                raise DomainError("erp_stock_query_invalid", "商品 ID 不能为空")
            result = await self._api.get_stock_by_product(context, product_id.strip())
            return self.ok_response(data=result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def get_stock_summary(self) -> dict[str, Any]:
        """查询库存汇总（库存总量、库存总值等经营概览数据）。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            result = await self._api.get_stock_summary(context, {})
            return self.ok_response(data=result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def query_stock_logs(
        self,
        product_id: str = "",
        keyword: str = "",
        warehouse_id: str = "",
        start_date: str = "",
        end_date: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """分页查询库存变动流水（入库、出库、调拨等历史记录）。

        用户问"某商品的进出记录""最近库存怎么变的"时用此工具。

        Args:
            product_id: 商品内部 ID。
            keyword: 商品名称模糊关键词。
            warehouse_id: 仓库内部 ID 或名称。
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            self._validate_date_range(start_date.strip(), end_date.strip())
            payload: dict[str, Any] = {}
            if product_id.strip():
                payload["productId"] = product_id.strip()
            if keyword.strip():
                payload["productName"] = keyword.strip()
            warehouse = await self._optional_reference_id("warehouse", warehouse_id)
            if warehouse:
                payload["warehouseId"] = warehouse
            if start_date.strip():
                payload["startDate"] = start_date.strip()
            if end_date.strip():
                payload["endDate"] = end_date.strip()
            result = await self._api.query_stock_logs(context, self._with_page(payload, page, page_size))
            return self._paged_data_response(result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_stock_alerts(
        self,
        alert_type: int | None = None,
        keyword: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """分页查询库存预警商品列表（库存不足、积压或负库存）。

        Args:
            alert_type: 预警类型：1=库存不足 2=库存积压 3=负库存；不传查全部。
            keyword: 商品名称模糊关键词。
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            payload: dict[str, Any] = {}
            if alert_type is not None:
                if alert_type not in _STOCK_ALERT_TYPES:
                    raise DomainError("erp_stock_alert_type_invalid", "alert_type 必须是 1、2 或 3")
                payload["alertType"] = alert_type
            if keyword.strip():
                payload["productName"] = keyword.strip()
            result = await self._api.list_stock_alerts(context, self._with_page(payload, page, page_size))
            return self._paged_data_response(result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def get_purchase_suggestions(self) -> dict[str, Any]:
        """查询采购建议：库存不足商品的建议采购清单。"""
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            result = await self._api.get_purchase_suggestions(context)
            return self.ok_response(data=result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_stock_doc_types(self, kind: str) -> dict[str, Any]:
        """查询其他入库或出库的类型列表（如报损、报溢、盘盈、盘亏）。

        创建其他出入库单前先查询可用类型，取其 id 作为 doc_type。

        Args:
            kind: inbound=入库类型，outbound=出库类型。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            normalized = kind.strip()
            if normalized not in {"inbound", "outbound"}:
                raise DomainError("erp_stock_doc_type_invalid", "kind 必须是 inbound 或 outbound")
            result = await self._api.list_stock_doc_types(context, normalized)
            return self.ok_response(kind=normalized, types=result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_receivables(
        self,
        view: str,
        customer_id: str = "",
        start_date: str = "",
        end_date: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """查询应收账款：summary 按客户汇总，details 为应收明细。

        用户问"客户欠多少钱""应收款有哪些"时用此工具。

        Args:
            view: 视图：summary=按客户汇总，details=明细。
            customer_id: 客户内部 ID 或名称。
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            normalized = view.strip()
            if normalized not in {"summary", "details"}:
                raise DomainError("erp_receivable_view_invalid", "view 必须是 summary 或 details")
            self._validate_date_range(start_date.strip(), end_date.strip())
            params = self._with_page({}, page, page_size)
            customer = await self._optional_reference_id("customer", customer_id)
            if customer:
                params["customerId"] = customer
            if start_date.strip():
                params["startDate"] = start_date.strip()
            if end_date.strip():
                params["endDate"] = end_date.strip()
            result = await self._api.list_receivables(context, normalized, params)
            return self._paged_data_response(result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_payables(
        self,
        view: str,
        supplier_id: str = "",
        start_date: str = "",
        end_date: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """查询应付账款：summary 按供应商汇总，details 为应付明细。

        用户问"欠供应商多少钱""应付款有哪些"时用此工具。

        Args:
            view: 视图：summary=按供应商汇总，details=明细。
            supplier_id: 供应商内部 ID 或名称。
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            normalized = view.strip()
            if normalized not in {"summary", "details"}:
                raise DomainError("erp_payable_view_invalid", "view 必须是 summary 或 details")
            self._validate_date_range(start_date.strip(), end_date.strip())
            params = self._with_page({}, page, page_size)
            supplier = await self._optional_reference_id("supplier", supplier_id)
            if supplier:
                params["supplierId"] = supplier
            if start_date.strip():
                params["startDate"] = start_date.strip()
            if end_date.strip():
                params["endDate"] = end_date.strip()
            result = await self._api.list_payables(context, normalized, params)
            return self._paged_data_response(result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def get_financial_status(self, biz_date: str = "") -> dict[str, Any]:
        """查询资金状况：收款、付款、账户余额等当日财务概览。

        Args:
            biz_date: 业务日期 YYYY-MM-DD；不传默认当天。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            normalized = biz_date.strip()
            if normalized:
                self._validate_order_date(normalized)
            result = await self._api.get_financial_status(context, normalized)
            return self.ok_response(data=result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def query_sales_report(
        self,
        view: str,
        start_date: str = "",
        end_date: str = "",
        customer_id: str = "",
        handler_id: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """查询销售报表：分析、明细、明细汇总或销量排行。

        Args:
            view: 视图：analysis=销售分析，details=销售明细，
                details_summary=销售明细汇总，ranking_product=商品销量排行，
                ranking_customer=客户销量排行。
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            customer_id: 客户内部 ID 或名称。
            handler_id: 经手人内部 ID 或名称。
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            normalized = view.strip()
            if normalized not in _SALES_REPORT_VIEWS:
                raise DomainError(
                    "erp_report_view_invalid",
                    "view 必须是 %s" % "、".join(sorted(_SALES_REPORT_VIEWS)),
                )
            self._validate_date_range(start_date.strip(), end_date.strip())
            params = self._with_page({}, page, page_size)
            customer = await self._optional_reference_id("customer", customer_id)
            if customer:
                params["customerIds"] = [customer]
            handler = await self._optional_reference_id("handler", handler_id)
            if handler:
                params["handlerIds"] = [handler]
            if start_date.strip():
                params["startDate"] = start_date.strip()
            if end_date.strip():
                params["endDate"] = end_date.strip()
            result = await self._api.query_sales_report(context, normalized, params)
            return self._paged_data_response(result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def query_purchase_report(
        self,
        view: str,
        start_date: str = "",
        end_date: str = "",
        supplier_id: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """查询采购报表：采购统计、采购明细或明细汇总。

        Args:
            view: 视图：statistics=采购统计，details=采购明细，
                details_summary=采购明细汇总。
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            supplier_id: 供应商内部 ID 或名称。
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            normalized = view.strip()
            if normalized not in _PURCHASE_REPORT_VIEWS:
                raise DomainError(
                    "erp_report_view_invalid",
                    "view 必须是 %s" % "、".join(sorted(_PURCHASE_REPORT_VIEWS)),
                )
            self._validate_date_range(start_date.strip(), end_date.strip())
            params = self._with_page({}, page, page_size)
            supplier = await self._optional_reference_id("supplier", supplier_id)
            if supplier:
                params["supplierIds"] = [supplier]
            if start_date.strip():
                params["startDate"] = start_date.strip()
            if end_date.strip():
                params["endDate"] = end_date.strip()
            result = await self._api.query_purchase_report(context, normalized, params)
            return self._paged_data_response(result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def query_profit_report(
        self,
        view: str,
        start_date: str = "",
        end_date: str = "",
        customer_id: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """查询利润报表：利润汇总、按客户利润或按商品利润。

        Args:
            view: 视图：summary=利润汇总，by_customer=按客户利润，
                by_product=按商品利润。
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            customer_id: 客户内部 ID 或名称。
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            normalized = view.strip()
            if normalized not in _PROFIT_REPORT_VIEWS:
                raise DomainError(
                    "erp_report_view_invalid",
                    "view 必须是 %s" % "、".join(sorted(_PROFIT_REPORT_VIEWS)),
                )
            self._validate_date_range(start_date.strip(), end_date.strip())
            params = self._with_page({}, page, page_size)
            customer = await self._optional_reference_id("customer", customer_id)
            if customer:
                params["customerIds"] = [customer]
            if start_date.strip():
                params["startDate"] = start_date.strip()
            if end_date.strip():
                params["endDate"] = end_date.strip()
            result = await self._api.query_profit_report(context, normalized, params)
            return self._paged_data_response(result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def query_settlement_report(
        self,
        start_date: str = "",
        end_date: str = "",
        sort_by: str = "",
        order_type: str = "",
    ) -> dict[str, Any]:
        """查询结算统计：各结算账户的收款、付款与余额统计。

        Args:
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            sort_by: 排序字段，如 receiptAmount、paymentAmount、profit。
            order_type: 排序方向：asc 或 desc。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            self._validate_date_range(start_date.strip(), end_date.strip())
            params: dict[str, Any] = {}
            if start_date.strip():
                params["startDate"] = start_date.strip()
            if end_date.strip():
                params["endDate"] = end_date.strip()
            if sort_by.strip():
                params["sortBy"] = sort_by.strip()
            if order_type.strip():
                params["orderType"] = order_type.strip()
            result = await self._api.query_settlement_report(context, params)
            return self.ok_response(data=result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def query_reconciliation(
        self,
        view: str,
        customer_id: str = "",
        start_date: str = "",
        end_date: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """查询客户对账：summary 为对账报表，statement 为对账单明细。

        用户要"和某客户对账"时：先用 summary 查该客户往来汇总，
        需要逐单明细时再用 statement 并传 customer_id。

        Args:
            view: 视图：summary=客户对账报表，statement=客户对账单明细。
            customer_id: 客户内部 ID 或名称；statement 视图必填。
            start_date: 开始日期，格式 YYYY-MM-DD（statement 视图可用）。
            end_date: 结束日期，格式 YYYY-MM-DD（statement 视图可用）。
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            normalized = view.strip()
            if normalized not in {"summary", "statement"}:
                raise DomainError("erp_reconciliation_view_invalid", "view 必须是 summary 或 statement")
            self._validate_date_range(start_date.strip(), end_date.strip())
            customer = await self._optional_reference_id("customer", customer_id)
            params = self._with_page({}, page, page_size)
            if normalized == "statement":
                if not customer:
                    raise DomainError(
                        "erp_reconciliation_customer_required",
                        "statement 视图必须提供 customer_id",
                    )
                params["customerId"] = customer
                if start_date.strip():
                    params["startDate"] = start_date.strip()
                if end_date.strip():
                    params["endDate"] = end_date.strip()
            elif customer:
                params["customerIds"] = [customer]
            result = await self._api.query_reconciliation(context, normalized, params)
            return self._paged_data_response(result.data)
        except DomainError as exc:
            return self.error_response(exc)


def build_query_tools(host: QueryTools) -> list[SessionFunctionTool]:
    """把库存、往来与报表查询工具注册为 MCP 工具。"""
    return [
        SessionFunctionTool(
            host.query_stock,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
            input_schema_override=_QUERY_STOCK_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_stock_by_product,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_stock_summary,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.query_stock_logs,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
            input_schema_override=_QUERY_STOCK_LOGS_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_stock_alerts,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
            input_schema_override=_LIST_STOCK_ALERTS_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_purchase_suggestions,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_stock_doc_types,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
            input_schema_override=_LIST_STOCK_DOC_TYPES_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_receivables,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
            input_schema_override=_LIST_RECEIVABLES_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_payables,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
            input_schema_override=_LIST_PAYABLES_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_financial_status,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
            input_schema_override=_GET_FINANCIAL_STATUS_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.query_sales_report,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
            input_schema_override=_REPORT_INPUT_SCHEMAS["sales"],
        ),
        SessionFunctionTool(
            host.query_purchase_report,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
            input_schema_override=_REPORT_INPUT_SCHEMAS["purchase"],
        ),
        SessionFunctionTool(
            host.query_profit_report,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
            input_schema_override=_REPORT_INPUT_SCHEMAS["profit"],
        ),
        SessionFunctionTool(
            host.query_settlement_report,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
            input_schema_override=_QUERY_SETTLEMENT_REPORT_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.query_reconciliation,
            is_read_only=True,
            output_schema=_QUERY_OUTPUT_SCHEMA,
            input_schema_override=_QUERY_RECONCILIATION_INPUT_SCHEMA,
        ),
    ]

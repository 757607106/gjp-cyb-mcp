"""库存、往来与报表的只读查询工具。

这些工具无副作用，直接发布给模型；统一收拢分页与日期范围参数，
避免大结果集。实现为 YunCybToolSet 的混入类，与销售单工具共用
session、API 端口与基础资料解析。
"""

from __future__ import annotations

from typing import Any

from gjp_common.context import InvocationContextStore
from gjp_common.errors import DomainError
from gjp_common.tools import SessionFunctionTool
from .ports import YunCybApiPort
from .session import YunCybSession

_ERROR_OUTPUT_OBJECT = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
    },
    "additionalProperties": True,
}

# 查询类工具的通用输出：顶层 ok + data，分页结果另带 page 元数据。
# data 随工具与视图不同为列表（分页行）或对象（汇总），上游缺数据时为 null。
_QUERY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "data": {
            "anyOf": [
                {"type": "array"},
                {"type": "object"},
                {"type": "null"},
            ]
        },
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
        "keyword": {"type": "string", "description": "商品名称模糊关键词；留空不按商品名称筛选，不是商品 ID"},
        "warehouse_id": {"type": "string", "description": "仓库内部 ID 或可唯一匹配的名称；ID 来自 searchBusinessReferences(reference_type=warehouse)，不得自造；留空不限定仓库"},
        "stock_status": {"type": "integer", "enum": [0, 1, 2, 3], "description": "当前库存状态：0=全部，1=正常，2=零库存，3=负库存；省略不传状态筛选。不是库存预警类型"},
        "page": {"type": "integer", "minimum": 1, "default": 1, "description": "页码，从 1 开始，默认 1；查后续商品时递增页码，保持筛选条件不变"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "每页商品数量，1 到 100，默认 20；不是查询总数上限"},
    },
}

_QUERY_STOCK_LOGS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "product_id": {"type": "string", "description": "可选商品内部 ID，取自 searchProducts 或 queryStock 已匹配商品；不得填名称、商品编号或自造 ID；留空不按 ID 筛选"},
        "keyword": {"type": "string", "description": "商品名称模糊关键词；留空不按名称筛选。与 product_id 同传时同时生效，不是备用条件"},
        "warehouse_id": {"type": "string", "description": "仓库内部 ID 或可唯一匹配的名称；ID 来自 searchBusinessReferences(reference_type=warehouse)，不得自造；留空不限定仓库"},
        "start_date": {"type": "string", "description": "流水开始日期 YYYY-MM-DD，按用户指定期间填写；留空不传该边界，由 ERP 默认行为决定，不自动设为当月"},
        "end_date": {"type": "string", "description": "流水结束日期 YYYY-MM-DD，不得早于 start_date；留空不传该边界，由 ERP 默认行为决定"},
        "page": {"type": "integer", "minimum": 1, "default": 1, "description": "页码，从 1 开始，默认 1；查后续流水时递增页码，保持筛选条件不变"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "每页流水数量，1 到 100，默认 20；不是查询总数上限"},
    },
}

_LIST_STOCK_ALERTS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "alert_type": {"type": "integer", "enum": [1, 2, 3], "description": "现有预警类型：1=库存不足，2=库存积压，3=负库存；省略查全部类型。与 queryStock 的 stock_status 数字含义不同"},
        "keyword": {"type": "string", "description": "预警商品的名称模糊关键词；留空不按商品名称筛选"},
        "page": {"type": "integer", "minimum": 1, "default": 1, "description": "页码，从 1 开始，默认 1；查后续预警时递增页码，保持筛选条件不变"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "每页预警数量，1 到 100，默认 20；不是查询总数上限"},
    },
}

_LIST_STOCK_DOC_TYPES_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["inbound", "outbound"],
            "description": "必填：inbound=其他入库类型（如报溢），outbound=其他出库类型（如报损）；与后续 previewOtherStockDoc 的 kind 一致，只查类型、不创建单据",
        },
    },
    "required": ["kind"],
}

_LIST_RECEIVABLES_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "view": {"type": "string", "enum": ["summary", "details"], "description": "必填：summary=按客户汇总应收（各客户欠我多少钱）；details=应收明细（欠款由哪些业务构成）。不是销售单列表或实收款统计"},
        "customer_id": {"type": "string", "description": "客户内部 ID 或可唯一匹配的名称；ID 来自 searchBusinessReferences(reference_type=customer)，不得自造；留空不限定客户"},
        "start_date": {"type": "string", "description": "查询开始日期 YYYY-MM-DD，按用户指定期间填写；留空不传该边界，由 ERP 默认行为决定，不自动设为当月"},
        "end_date": {"type": "string", "description": "查询结束日期 YYYY-MM-DD，不得早于 start_date；留空不传该边界，由 ERP 默认行为决定"},
        "page": {"type": "integer", "minimum": 1, "default": 1, "description": "页码，从 1 开始，默认 1；翻页时保持 view、日期与客户筛选不变"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "每页数量，1 到 100，默认 20；不是查询总数上限"},
    },
    "required": ["view"],
}

_LIST_PAYABLES_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "view": {"type": "string", "enum": ["summary", "details"], "description": "必填：summary=按供应商汇总应付（我欠各供应商多少钱）；details=应付明细（欠款由哪些业务构成）。不是采购单列表或实付款统计"},
        "supplier_id": {"type": "string", "description": "供应商内部 ID 或可唯一匹配的名称；ID 来自 searchBusinessReferences(reference_type=supplier)，不得自造；留空不限定供应商"},
        "start_date": {"type": "string", "description": "查询开始日期 YYYY-MM-DD，按用户指定期间填写；留空不传该边界，由 ERP 默认行为决定，不自动设为当月"},
        "end_date": {"type": "string", "description": "查询结束日期 YYYY-MM-DD，不得早于 start_date；留空不传该边界，由 ERP 默认行为决定"},
        "page": {"type": "integer", "minimum": 1, "default": 1, "description": "页码，从 1 开始，默认 1；翻页时保持 view、日期与供应商筛选不变"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "每页数量，1 到 100，默认 20；不是查询总数上限"},
    },
    "required": ["view"],
}

_GET_FINANCIAL_STATUS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "biz_date": {"type": "string", "description": "财务状况业务日期 YYYY-MM-DD，按用户指定日期填写；留空不传日期，由 ERP 默认行为决定；不是开始/结束日期范围"},
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
            "view": {"type": "string", "enum": sorted(_SALES_REPORT_VIEWS), "description": "必填：analysis=销售分析（销售整体怎么样）；details=销售明细（具体卖了哪些商品）；details_summary=销售明细合计（这些销售明细合计多少）；ranking_product=商品销量排行（什么商品卖得好）；ranking_customer=客户销量排行（哪些客户买得多）。查销售单号/单据状态用 listSalesOrders，不用报表"},
            "start_date": {"type": "string", "description": "报表开始日期 YYYY-MM-DD，按用户指定期间填写；留空不传该边界，由 ERP 默认行为决定，不自动设为当月"},
            "end_date": {"type": "string", "description": "报表结束日期 YYYY-MM-DD，不得早于 start_date；留空不传该边界，由 ERP 默认行为决定"},
            "customer_id": {"type": "string", "description": "客户内部 ID 或可唯一匹配的名称；ID 来自 searchBusinessReferences(reference_type=customer)，不得自造；留空不限定客户"},
            "handler_id": {"type": "string", "description": "经手人内部 ID 或可唯一匹配的名称；ID 来自 searchBusinessReferences(reference_type=handler)，不得自造；留空不限定经手人"},
            "page": {"type": "integer", "minimum": 1, "default": 1, "description": "页码，从 1 开始，默认 1；用于支持分页的报表视图，翻页保持 view 与筛选条件不变，汇总不靠翻页累加"},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "每页数量，1 到 100，默认 20；用于支持分页的报表视图，不是统计总量或排行指标"},
        },
        "required": ["view"],
    },
    "purchase": {
        "type": "object",
        "properties": {
            "view": {"type": "string", "enum": sorted(_PURCHASE_REPORT_VIEWS), "description": "必填：statistics=采购统计（整体采购情况）；details=采购明细（具体采购了哪些商品）；details_summary=采购明细合计（这些采购明细合计多少）。查采购单号/单据状态用 listPurchaseOrders，不用报表"},
            "start_date": {"type": "string", "description": "报表开始日期 YYYY-MM-DD，按用户指定期间填写；留空不传该边界，由 ERP 默认行为决定，不自动设为当月"},
            "end_date": {"type": "string", "description": "报表结束日期 YYYY-MM-DD，不得早于 start_date；留空不传该边界，由 ERP 默认行为决定"},
            "supplier_id": {"type": "string", "description": "供应商内部 ID 或可唯一匹配的名称；ID 来自 searchBusinessReferences(reference_type=supplier)，不得自造；留空不限定供应商"},
            "page": {"type": "integer", "minimum": 1, "default": 1, "description": "页码，从 1 开始，默认 1；用于支持分页的报表视图，翻页保持 view 与筛选条件不变，汇总不靠翻页累加"},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "每页数量，1 到 100，默认 20；用于支持分页的报表视图，不是统计总量"},
        },
        "required": ["view"],
    },
    "profit": {
        "type": "object",
        "properties": {
            "view": {"type": "string", "enum": sorted(_PROFIT_REPORT_VIEWS), "description": "必填：summary=利润汇总（整体赚了多少）；by_customer=按客户统计利润（哪个客户贡献多少利润）；by_product=按商品统计利润（哪些商品赚钱）。利润不是销售额、收款额或账户余额"},
            "start_date": {"type": "string", "description": "报表开始日期 YYYY-MM-DD，按用户指定期间填写；留空不传该边界，由 ERP 默认行为决定，不自动设为当月"},
            "end_date": {"type": "string", "description": "报表结束日期 YYYY-MM-DD，不得早于 start_date；留空不传该边界，由 ERP 默认行为决定"},
            "customer_id": {"type": "string", "description": "客户内部 ID 或可唯一匹配的名称；ID 来自 searchBusinessReferences(reference_type=customer)，不得自造；留空不限定客户"},
            "page": {"type": "integer", "minimum": 1, "default": 1, "description": "页码，从 1 开始，默认 1；用于支持分页的报表视图，翻页保持 view 与筛选条件不变，汇总不靠翻页累加"},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "每页数量，1 到 100，默认 20；用于支持分页的报表视图，不是统计总量"},
        },
        "required": ["view"],
    },
}

_QUERY_SETTLEMENT_REPORT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "start_date": {"type": "string", "description": "结算统计开始日期 YYYY-MM-DD，按用户指定期间填写；留空不传该边界，由 ERP 默认行为决定，不自动设为当月"},
        "end_date": {"type": "string", "description": "结算统计结束日期 YYYY-MM-DD，不得早于 start_date；留空不传该边界，由 ERP 默认行为决定"},
        "sort_by": {"type": "string", "description": "可选 ERP 结算统计排序字段；仅在已知受支持的字段且用户要求排序时填写，不把中文指标名或猜测值当字段；留空使用 ERP 默认排序"},
        "order_type": {"type": "string", "enum": ["asc", "desc"], "description": "排序方向：asc=升序（从小到大），desc=降序（从大到小）；配合已知 sort_by 使用；省略不传排序方向"},
    },
}

_QUERY_RECONCILIATION_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "view": {
            "type": "string",
            "enum": ["summary", "statement"],
            "description": "必填：summary=客户对账报表（各客户往来汇总，不支持日期筛选）；statement=指定客户对账单明细（逐笔核对某段时间往来，必须提供 customer_id）。不是供应商对账或新建收款单",
        },
        "customer_id": {"type": "string", "description": "客户内部 ID 或可唯一匹配的名称；ID 来自 searchBusinessReferences(reference_type=customer)，不得自造；statement 必填，summary 留空不限定客户"},
        "start_date": {"type": "string", "description": "仅 statement 的开始日期 YYYY-MM-DD，按用户指定期间填写；留空不传该边界，由 ERP 默认行为决定，不自动设为当月；summary 不使用，应省略"},
        "end_date": {"type": "string", "description": "仅 statement 的结束日期 YYYY-MM-DD，不得早于 start_date；留空不传该边界，由 ERP 默认行为决定；summary 不使用，应省略"},
        "page": {"type": "integer", "minimum": 1, "default": 1, "description": "页码，从 1 开始，默认 1；翻页时保持 view、客户及日期条件不变"},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "每页数量，1 到 100，默认 20；不是查询总数上限"},
    },
    "required": ["view"],
}


class QueryTools:
    """库存查询、库存预警、往来账与报表分析工具（只读）。

    以混入类并入 YunCybToolSet：session、_api、_contexts 与
    ok_response 等基础设施由宿主 ToolSet 提供。
    """

    session: YunCybSession
    _api: YunCybApiPort
    _contexts: InvocationContextStore

    async def query_stock(
        self,
        keyword: str = "",
        warehouse_id: str = "",
        stock_status: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """只读、分页查询商品当前库存，可按商品名称、仓库和库存状态筛选。

        用户问“某商品现在还有多少”“某仓库有什么货”“哪些商品是零库存”时使用。
        只有商品名称、尚未匹配 ID 时也可用关键词查询；不是历史库存或库存变动记录。
        已匹配一个商品、要看其各仓库库存用 getStockByProduct；全账套库存总量/
        总值用 getStockSummary；历史进出流水用 queryStockLogs；现有预警用
        listStockAlerts。不要把本工具的单页商品数量当成全局库存汇总。"""
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
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
        """只读查询一个已匹配商品的当前各仓库库存，回答“这个商品分布在哪些仓库”。

        本工具不做名称匹配，也不查询多个商品。只有名称时先用 searchProducts
        或 queryStock 找到并确认商品；要按关键词/仓库分页查商品库存用 queryStock，
        看全局库存总量/总值用 getStockSummary，看历史变动用 queryStockLogs。

        Args:
            product_id: 必填，来自 searchProducts 或 queryStock 查询结果中已匹配
                商品的真实内部 ID；不得填商品名称、商品编号或自造 ID。
        """
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
            if not product_id.strip():
                raise DomainError("erp_stock_query_invalid", "商品 ID 不能为空")
            result = await self._api.get_stock_by_product(context, product_id.strip())
            return self.ok_response(data=result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def get_stock_summary(self) -> dict[str, Any]:
        """只读查询当前账套的全局库存汇总，回答“库存总量/库存总值是多少”。

        无业务参数，不支持商品、仓库、日期或分页筛选，不提供商品库存明细。
        “某仓库有哪些货”“某商品还有多少”用 queryStock；已匹配商品的各仓库
        库存用 getStockByProduct；历史进出流水用 queryStockLogs。
        """
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
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
        """只读、分页查询商品或仓库的库存进出记录与历史变动流水。

        用户问“某商品最近的进出记录”“某段时间库存为什么变化”时直接使用；
        只有商品名称时可传 keyword，无需先查当前库存或商品目录。不是当前库存
        快照；查现在剩多少用 queryStock，查一个已匹配商品的各仓库库存用
        getStockByProduct。本工具不创建出入库单，也不调整库存。"""
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
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
        """只读、分页查询 ERP 已有的库存预警：库存不足、积压或负库存。

        用户问“哪些商品库存不足”“有哪些积压预警”“负库存预警有哪些”时使用。
        本工具不设置预警阈值、不新建或处理预警；只查当前库存量用 queryStock，
        要建议补货清单用 getPurchaseSuggestions，查历史变动用 queryStockLogs。"""
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
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
        """只读查询 ERP 对库存不足商品给出的采购建议，回答“哪些货建议补、补多少”。

        无业务参数，不支持自填商品、仓库、供应商、日期或分页筛选。
        这是建议清单，不是已有采购单，也不会创建采购单或自动补货。
        只看现有预警用 listStockAlerts；查已开采购单用 listPurchaseOrders；
        用户决定采购并明确商品、数量等信息后用 previewPurchaseOrder 预览，
        经用户确认后才可用 submitPurchaseOrder 提交，不能把查询建议当作授权。
        """
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
            result = await self._api.get_purchase_suggestions(context)
            return self.ok_response(data=result.data)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_stock_doc_types(self, kind: str) -> dict[str, Any]:
        """只读查询其他入库/出库的可用业务类型，为 previewOtherStockDoc 选择类型。

        用户问“报损该选什么出库类型”“其他入库有哪些类型”时使用；按方向查询，
        从真实候选中选择类型 ID 填入 previewOtherStockDoc 的 doc_type，不得自造。
        本工具不新建类型、不开单、不调整库存，也不是盘点或出入库历史单据查询；
        查历史进出记录用 queryStockLogs，实际开其他出入库单先用 previewOtherStockDoc。"""
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
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
        """只读查询应收账款：客户欠本企业的钱，支持按客户汇总或查看欠款明细。

        用户问“客户还欠我多少”“应收款由哪些业务构成”时使用；应收不等于销售额
        或已经收到的钱。欠供应商的钱用 listPayables；客户往来逐笔对账用
        queryReconciliation；已开销售单的单号/状态查询用 listSalesOrders，
        不把单据列表当应收汇总；结算账户收付款统计用 querySettlementReport。
        本工具不收款、不核销。"""
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
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
        """只读查询应付账款：本企业欠供应商的钱，支持按供应商汇总或查看欠款明细。

        用户问“我还欠供应商多少”“应付款由哪些业务构成”时使用；应付不等于
        采购额或已经付出的钱。客户欠本企业的钱用 listReceivables；采购统计用
        queryPurchaseReport；已开采购单的单号/状态查询用 listPurchaseOrders，
        不把单据列表当应付汇总；结算账户收付款统计用 querySettlementReport。
        本工具不付款、不核销，queryReconciliation 也不是供应商对账工具。"""
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
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
        """只读查询指定业务日期的财务状况，了解企业资产、负债等整体概况。

        用户问“企业资产负债情况如何”“某天的财务状况怎样”时使用；不是某段
        时间的收付款流水或利润报表。期间利润用 queryProfitReport；结算账户
        收付款统计用 querySettlementReport；客户欠款用 listReceivables，
        欠供应商款用 listPayables。本工具不支持客户、账户或日期范围筛选。"""
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
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
        """只读查询销售分析、商品销售明细及其合计、商品或客户销量排行。

        用户要销售统计或“什么卖得好”时使用；“列出销售单”“找某单号/状态的单据”
        用 listSalesOrders，查看一张销售单用 getSalesOrder，而不是报表 details。
        不要用销售单列表替代统计。利润用 queryProfitReport，客户欠款用
        listReceivables，实际结算收付款用 querySettlementReport，采购统计用
        queryPurchaseReport；这些指标不等同于销售额。"""
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
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
        """只读查询采购统计、商品采购明细及其合计，了解实际采购业务情况。

        用户问“这段时间采购了多少”“采购了哪些商品”时使用；“列出采购单”
        “找某单号/状态的单据”用 listPurchaseOrders，查看一张采购单用
        getPurchaseOrder，而不是报表 details。不要用采购单列表替代统计。
        欠供应商多少钱用 listPayables；建议补哪些货用 getPurchaseSuggestions；
        新建采购单先用 previewPurchaseOrder；本工具不创建采购单。"""
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
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
        """只读查询期间利润，可看利润汇总、按客户利润或按商品利润。

        用户问“赚了多少”“哪些商品/客户贡献利润”时使用；利润不等于销售额、
        收款额、应收款或账户余额。销售额和销量排行用 querySalesReport；结算
        收付款用 querySettlementReport；资产负债概况用 getFinancialStatus。
        本工具没有商品 ID 筛选参数，不要为 by_product 自造 product_id 参数。"""
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
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
        """只读查询结算统计，了解各结算账户的收款、付款与余额情况。

        用户问“各账户这段时间收了多少、付了多少”“结算账户收支怎么样”时使用。
        不是客户欠款（listReceivables）、供应商欠款（listPayables）、企业资产
        负债概况（getFinancialStatus）或经营利润（queryProfitReport）。查具体
        收款单/付款单用 listReceiptOrders / listPaymentOrders，不用统计替代单据查询。
        无 view、分页或账户 ID 筛选参数；本工具不会发起收付款或核销。"""
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
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
        """只读查询客户往来对账报表或指定客户的对账单明细，不执行结算或核销。

        用户问“各客户往来汇总”“和某客户逐笔对账”时使用。只问客户欠多少可用
        listReceivables；本工具不是销售统计（querySalesReport），不是销售单
        列表（listSalesOrders），也不支持供应商对账；供应商应付查询用 listPayables。
        summary 不支持日期筛选；要核对指定期间的某客户往来，直接使用 statement
        并提供真实客户，不必先查 summary。缺少客户时先确认，不为通过校验自造 ID。"""
        try:
            context = self._contexts.get()
            context.require_scope("yuncyb:read")
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

"""完整销售单流程的 AgentScope 工具集。"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from gjp_common.context import InvocationContext, InvocationContextStore
from gjp_common.errors import DomainError
from gjp_common.tools import SessionFunctionTool
from gjp_common.toolset import AgentScopeToolSet
from .catalog import normalize_name
from .models import BillingDraft
from .ports import BillingApiPort, BillingReferenceSnapshot
from .session import ErpBillingSession


BILLING_MCP_TOOL_NAMES = frozenset(
    {
        "sync_products",
        "list_products",
        "search_products",
        "search_billing_references",
        "preview_sales_order",
        "submit_sales_order",
        "get_sales_order",
        "list_sales_orders",
        "void_sales_order",
        "update_sales_order",
    },
)

_SAVE_TYPE_CODES = {
    "draft": 0,
    "pre_receipt": 1,
    "final": 2,
}
_SAVE_TYPE_LABELS = {
    "draft": "草稿",
    "pre_receipt": "预收",
    "final": "正式",
}
_REQUIRED_FIELDS = (
    ("customer", "客户", "请问销售客户是哪一位？"),
    ("warehouse", "出库仓库", "请问从哪个仓库出库？"),
    ("handler", "经手人", "请问本单经手人是谁？"),
    ("order_date", "录单日期", "请问录单日期是哪一天？"),
    ("order_text", "商品明细", "请提供商品、数量和单位。"),
)
_MONEY_QUANTUM = Decimal("0.01")


_ERROR_OUTPUT_OBJECT = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
    },
    "additionalProperties": True,
}

# 开单工具输出 schema：顶层字段声明类型，嵌套对象/数组项保持宽松
# （additionalProperties=True），避免动态字段（如非空才带的 image_urls）
# 和可空字段触发 jsonschema.validate 失败；required 只放必定出现的 ok。

_SYNC_PRODUCTS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "catalog_version": {"type": "string"},
        "product_count": {"type": "integer"},
        "sample_products": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_LIST_PRODUCTS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
        "total": {"type": "integer"},
        "has_more": {"type": "boolean"},
        "products": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_SEARCH_PRODUCTS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "status": {"type": "string"},
                    "product": {"type": ["object", "null"]},
                    "recommendations": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                    },
                },
                "required": ["query", "status"],
                "additionalProperties": True,
            },
        },
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_SEARCH_BILLING_REFERENCES_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "reference_type": {"type": "string"},
        "keyword": {"type": "string"},
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
        "total": {"type": "integer"},
        "has_more": {"type": "boolean"},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "is_default": {"type": "boolean"},
                },
                "additionalProperties": True,
            },
        },
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_PREVIEW_SALES_ORDER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "missing_required_fields": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "reference_resolutions": {"type": "object", "additionalProperties": True},
        "unit_warnings": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "required_actions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "confirmed_products": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "recommended_products": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "unmatched_products": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "ready_to_submit": {"type": "boolean"},
        "preview_id": {"type": ["string", "null"]},
        "preview": {"type": ["object", "null"]},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_SUBMIT_SALES_ORDER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "submitted": {"type": "boolean"},
        "order_no": {"type": "string"},
        "save_type": {"type": "string"},
        "idempotent_replay": {"type": "boolean"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_GET_SALES_ORDER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "order": {"type": ["object", "null"], "additionalProperties": True},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_LIST_SALES_ORDERS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
        "total": {"type": "integer"},
        "has_more": {"type": "boolean"},
        "orders": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_VOID_SALES_ORDER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "voided": {"type": "boolean"},
        "order_no": {"type": "string"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_UPDATE_SALES_ORDER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "modified": {"type": "boolean"},
        "order_no": {"type": "string"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}


# 输入 schema：为含枚举和范围约束的参数补充 JSON Schema 约束，
# 使模型在调用前就被限制在合法值域内，而非运行时才被拦截。
# 只收紧约束（加 enum/minimum/maximum），不改变参数结构。

_LIST_PRODUCTS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
    },
}

_SEARCH_BILLING_REFERENCES_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "reference_type": {
            "type": "string",
            "enum": ["customer", "warehouse", "handler"],
        },
        "keyword": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        "page": {"type": "integer", "minimum": 1, "default": 1},
    },
    "required": ["reference_type"],
}

_PREVIEW_SALES_ORDER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_text": {"type": "string"},
        "customer": {"type": "string"},
        "warehouse": {"type": "string"},
        "handler": {"type": "string"},
        "order_date": {"type": "string", "description": "YYYY-MM-DD"},
        "remark": {"type": "string"},
        "save_type": {
            "type": "string",
            "enum": ["draft", "pre_receipt", "final"],
            "default": "final",
        },
        "source": {
            "type": "string",
            "enum": ["text", "voice", "image"],
            "default": "text",
        },
        "confirmed_products": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_id": {"type": "string"},
                    "product_id": {"type": "string"},
                },
                "required": ["line_id", "product_id"],
            },
        },
        "partial": {"type": "boolean", "default": False},
    },
}

_LIST_SALES_ORDERS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        "sort_by": {"type": "string", "enum": ["updateTime", "orderDate", ""]},
        "order_type": {"type": "string", "enum": ["asc", "desc", ""]},
        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
        "status": {"type": "integer", "enum": [0, 1, 2, 3]},
        "payment_status": {"type": "integer", "enum": [0, 1, 2]},
        "return_status": {"type": "integer", "enum": [0, 1, 2]},
        "order_no": {"type": "string"},
        "customer_id": {"type": "string"},
    },
}

_UPDATE_SALES_ORDER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {
            "type": "string",
            "description": "内部 ID 或业务单号 orderNo（如 XS 开头）",
        },
        "order_date": {"type": "string", "description": "YYYY-MM-DD"},
        "handler_id": {
            "type": "string",
            "description": "经办人内部 ID 或名称；名称必须唯一匹配",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "quantity": {"type": "number"},
                    "unit": {"type": "string"},
                    "unit_price": {"type": "number"},
                    "order_item_id": {"type": "string"},
                    "remark": {"type": "string"},
                },
                "required": ["product_id", "quantity"],
            },
        },
        "customer_id": {
            "type": "string",
            "description": "客户内部 ID 或名称；已生效单据不可修改",
        },
        "warehouse_id": {
            "type": "string",
            "description": "出库仓库内部 ID 或名称；已生效单据不可修改",
        },
        "save_type": {
            "type": "string",
            "enum": ["draft", "pre_receipt", "final"],
            "default": "draft",
        },
        "remark": {"type": "string"},
        "discount_amount": {"type": "number"},
        "discount_account_id": {"type": "string"},
        "receipt_amount": {"type": "number"},
        "receipt_account_id": {"type": "string"},
        "confirmed_by_user": {"type": "boolean", "default": False},
    },
    "required": ["order_id", "order_date", "handler_id", "items"],
}


class BillingToolSet(AgentScopeToolSet):
    """开单 ToolSet：检索基础资料、生成预览并在确认后写入销售单。"""

    def __init__(
        self,
        session: ErpBillingSession,
        api: BillingApiPort,
        contexts: InvocationContextStore,
    ) -> None:
        self.session = session
        self._api = api
        super().__init__(
            [
                SessionFunctionTool(
                    self.sync_products,
                    is_concurrency_safe=False,
                    output_schema=_SYNC_PRODUCTS_OUTPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.list_products,
                    is_read_only=True,
                    output_schema=_LIST_PRODUCTS_OUTPUT_SCHEMA,
                    input_schema_override=_LIST_PRODUCTS_INPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    session.search_products,
                    is_read_only=True,
                    output_schema=_SEARCH_PRODUCTS_OUTPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.search_billing_references,
                    is_read_only=True,
                    output_schema=_SEARCH_BILLING_REFERENCES_OUTPUT_SCHEMA,
                    input_schema_override=_SEARCH_BILLING_REFERENCES_INPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.preview_sales_order,
                    is_read_only=True,
                    is_concurrency_safe=False,
                    output_schema=_PREVIEW_SALES_ORDER_OUTPUT_SCHEMA,
                    input_schema_override=_PREVIEW_SALES_ORDER_INPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.submit_sales_order,
                    is_concurrency_safe=False,
                    output_schema=_SUBMIT_SALES_ORDER_OUTPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.get_sales_order,
                    is_read_only=True,
                    output_schema=_GET_SALES_ORDER_OUTPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.list_sales_orders,
                    is_read_only=True,
                    output_schema=_LIST_SALES_ORDERS_OUTPUT_SCHEMA,
                    input_schema_override=_LIST_SALES_ORDERS_INPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.void_sales_order,
                    is_concurrency_safe=False,
                    output_schema=_VOID_SALES_ORDER_OUTPUT_SCHEMA,
                ),
                SessionFunctionTool(
                    self.update_sales_order,
                    is_concurrency_safe=False,
                    output_schema=_UPDATE_SALES_ORDER_OUTPUT_SCHEMA,
                    input_schema_override=_UPDATE_SALES_ORDER_INPUT_SCHEMA,
                ),
            ],
            contexts=contexts,
            agent_tool_names=BILLING_MCP_TOOL_NAMES,
            mcp_tool_names=BILLING_MCP_TOOL_NAMES,
        )

    async def sync_products(self, limit: int | None = None) -> dict[str, Any]:
        """同步当前租户的商品目录到内存缓存。

        未传 limit 时全量同步并刷新租户共享目录；传 limit 时截断结果
        只作用于当前会话。

        Args:
            limit: 可选的最大商品数量。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            loader = self._catalog_loader(context, limit)
            if limit is None:
                synced_at = await self.session.sync_catalog(loader)
            else:
                synced_at = await self.session.sync_catalog_partial(loader)
            catalog = self.session.catalog
            products = catalog.products if catalog is not None else []
            sample = [
                product.listing_fields()
                for product in products[:5]
            ]
            return self.ok_response(
                catalog_version=synced_at,
                product_count=len(products),
                sample_products=sample,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def list_products(
        self,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """列出当前会话商品目录中的所有商品（分页）。

        目录为空时自动同步一次。用户问"有哪些商品"时用此工具，
        不要用 search_products 遍历关键词。

        Args:
            page: 页码，从 1 开始。
            page_size: 每页商品数量，范围 1 到 100。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            await self.session.ensure_catalog(self._catalog_loader(context))
            catalog = self.session.catalog
            products = catalog.products if catalog is not None else []
            total = len(products)
            effective_page = max(1, int(page or 1))
            effective_size = max(1, min(int(page_size or 20), 100))
            start = (effective_page - 1) * effective_size
            end = start + effective_size
            items = [
                product.listing_fields()
                for product in products[start:end]
            ]
            return self.ok_response(
                page=effective_page,
                page_size=effective_size,
                total=total,
                has_more=end < total,
                products=items,
            )
        except DomainError as exc:
            return self.error_response(exc)

    def _catalog_loader(
        self,
        context: InvocationContext,
        limit: int | None = None,
    ) -> Callable[[], Awaitable[list[dict[str, Any]]]]:
        """构建目录加载闭包：捕获当前上下文，供后台刷新复用鉴权。

        自动同步（未显式传 limit）时按 settings.auto_sync_limit 设上限，
        避免超大商品目录的串行翻页把首次开单拖到超时。
        """
        context.require_scope("billing:read")
        effective_limit = (
            limit if limit is not None else self.session.settings.auto_sync_limit
        )

        async def loader() -> list[dict[str, Any]]:
            snapshot = await self._api.fetch_products(context, effective_limit)
            if not snapshot.products:
                raise DomainError(
                    "erp_live_product_empty",
                    "当前账号没有返回可用于开单的商品",
                )
            return list(snapshot.products)

        return loader

    async def search_billing_references(
        self,
        reference_type: str,
        keyword: str = "",
        limit: int = 5,
        page: int = 1,
    ) -> dict[str, Any]:
        """查询销售单的客户、出库仓库或经手人候选。

        Args:
            reference_type: 基础资料类型：customer、warehouse 或 handler。
            keyword: 名称或编号关键词；留空时返回第一页可用项。
            limit: 每页最多返回的候选数，范围 1 到 20，默认 5。
            page: 页码，从 1 开始；候选过多时翻页查看。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            effective_limit = max(1, min(int(limit or 5), 20))
            effective_page = max(1, int(page or 1))
            clean_keyword = keyword.strip()
            snapshot = await self._search_reference(
                reference_type,
                clean_keyword,
                effective_limit,
                effective_page,
            )
            options = sorted(
                snapshot.options,
                key=lambda option: self._reference_sort_key(option, clean_keyword),
            )
            return self.ok_response(
                reference_type=reference_type,
                keyword=clean_keyword,
                page=snapshot.page_num,
                page_size=snapshot.page_size,
                total=snapshot.total,
                has_more=(snapshot.page_num * snapshot.page_size) < snapshot.total,
                options=[
                    self._public_reference_option(option)
                    for option in options
                ],
            )
        except (DomainError, TypeError, ValueError) as exc:
            error = (
                exc
                if isinstance(exc, DomainError)
                else DomainError("erp_reference_limit_invalid", "limit 必须是整数")
            )
            return self.error_response(error)

    async def preview_sales_order(
        self,
        order_text: str = "",
        customer: str = "",
        warehouse: str = "",
        handler: str = "",
        order_date: str = "",
        remark: str = "",
        save_type: str = "final",
        source: str = "text",
        confirmed_products: list[dict[str, str]] | None = None,
        partial: bool = False,
    ) -> dict[str, Any]:
        """校验完整销售单信息、匹配真实资料并生成不可变提交预览。

        Args:
            order_text: 完整商品文本，必须包含商品和数量；多轮修改后传完整内容。
            customer: 必填，客户名称或编号。
            warehouse: 必填，出库仓库名称或编号。
            handler: 必填，经手人名称或编号。
            order_date: 必填，录单日期，格式 YYYY-MM-DD。
            remark: 可选的整单备注，最多 200 个字符。
            save_type: 保存类型；draft 草稿、pre_receipt 预收、final 正式。
            source: 文本来源；语音和图片必须先由前端转成文本。
            confirmed_products: 用户确认的商品列表，每个元素格式为
                {"line_id": "L001", "product_id": "ERP商品ID"}；
                line_id 取自 recommended_products 或 unmatched_products
                的 line_id 字段，product_id 取自 product_id 字段；
                对 unmatched_products 中无候选的行同样有效。
            partial: 为 true 时只提交已匹配商品，跳过未匹配行；
                用于部分开单场景。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            values = {
                "customer": customer.strip(),
                "warehouse": warehouse.strip(),
                "handler": handler.strip(),
                "order_date": order_date.strip(),
                "order_text": order_text.strip(),
            }
            self._validate_order_fields(
                order_date=values["order_date"],
                remark=remark,
                save_type=save_type,
                source=source,
            )
            missing = [
                {"field": field, "label": label, "prompt": prompt}
                for field, label, prompt in _REQUIRED_FIELDS
                if not values[field]
            ]

            draft = await self._match_order_products(
                values["order_text"],
                source,
                confirmed_products,
            )
            product_payload = (
                draft.billing_products_payload()
                if draft is not None
                else {
                    "confirmed_products": [],
                    "recommended_products": [],
                    "unmatched_products": [],
                }
            )

            reference_resolutions = {
                "customer": await self._resolve_reference("customer", values["customer"]),
                "warehouse": await self._resolve_reference("warehouse", values["warehouse"]),
                "handler": await self._resolve_reference("handler", values["handler"]),
            }
            # 对外解析结果隐藏内部 ID；内部原始结果继续供预览构建使用
            public_reference_resolutions = {
                field: {
                    "status": resolution["status"],
                    "query": resolution["query"],
                    "selected": (
                        self._public_reference_option(resolution["selected"])
                        if resolution["selected"] is not None
                        else None
                    ),
                    "candidates": [
                        self._public_reference_option(option)
                        for option in resolution["candidates"]
                    ],
                }
                for field, resolution in reference_resolutions.items()
            }
            unit_warnings = self._unit_warnings(draft)
            references_ready = all(
                resolution["status"] == "matched"
                for resolution in reference_resolutions.values()
            )
            has_matched = (
                draft is not None
                and any(
                    line.status == "matched" and line.product is not None
                    for line in draft.lines
                )
            )
            ready = bool(
                not missing
                and references_ready
                and not unit_warnings
                and draft is not None
                and (
                    draft.status == "ready"
                    or (partial and has_matched)
                )
            )

            preview_id: str | None = None
            preview: dict[str, Any] | None = None
            if ready and draft is not None:
                payload, preview = self._build_sales_order_preview(
                    draft=draft,
                    references=reference_resolutions,
                    order_date=values["order_date"],
                    remark=remark.strip(),
                    save_type=save_type,
                    partial=partial,
                )
                preview_id = self.session.store_prepared_sales_order(payload, preview)

            required_actions = self._required_actions(
                missing=missing,
                reference_resolutions=reference_resolutions,
                product_payload=product_payload,
                unit_warnings=unit_warnings,
                ready=ready,
            )

            return self.ok_response(
                missing_required_fields=missing,
                reference_resolutions=public_reference_resolutions,
                unit_warnings=unit_warnings,
                required_actions=required_actions,
                **product_payload,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_sales_order(
        self,
        preview_id: str,
        idempotency_key: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """用户明确确认预览后，把销售单写入真实 ERP。

        成功后返回 order_no（业务单号，如 XS 开头）；预览提交成功后
        即失效，再次开单必须重新生成预览并确认。

        Args:
            preview_id: preview_sales_order 返回的预览 ID；提交成功后失效。
            idempotency_key: 调用方为本次提交生成的唯一业务键，重试必须复用。
            confirmed_by_user: 仅在用户明确确认该预览后传 true。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            if confirmed_by_user is not True:
                raise DomainError(
                    "erp_sales_order_confirmation_required",
                    "必须先向用户展示销售单预览并取得明确确认",
                )
            key = idempotency_key.strip()
            if not key or len(key) > 128:
                raise DomainError(
                    "erp_sales_order_idempotency_key_invalid",
                    "idempotency_key 不能为空且最多 128 个字符",
                )
            cached = self.session.submission_result(key)
            if cached is not None:
                if cached["preview_id"] != preview_id.strip():
                    raise DomainError(
                        "erp_sales_order_idempotency_key_conflict",
                        "该 idempotency_key 已用于另一份销售单预览",
                    )
                return self.ok_response(
                    **self._public_submission_result(cached),
                    idempotent_replay=True,
                )

            payload, preview = self.session.require_prepared_sales_order(preview_id)
            result = await self._api.create_sales_order(context, payload)
            # 创建成功后回查详情，把内部 ID 换成用户可读的业务单号；
            # 回查失败时降级返回内部 ID（双轨标识下仍可用于后续查询）。
            order_no = await self._lookup_order_no(result.order_id)
            cached_result = {
                "submitted": True,
                "order_no": order_no or result.order_id,
                "preview_id": preview_id.strip(),
                "save_type": preview["save_type"],
            }
            self.session.remember_submission(key, cached_result)
            # 预览一次性消费：成功后立即失效，换新幂等键重放同一预览会被
            # 拒绝，防止上下文丢失后模型用新 key 重复提交同一份预览。
            self.session.consume_prepared_sales_order(preview_id)
            return self.ok_response(
                **self._public_submission_result(cached_result),
                idempotent_replay=False,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def get_sales_order(self, order_id: str) -> dict[str, Any]:
        """查询销售单详情，含商品明细、收款记录和状态。

        Args:
            order_id: 销售单标识，同时接受内部 ID（list_sales_orders
                返回的 id）和业务单号 orderNo（如 XS 开头）。传入业务
                单号时按 orderNo 精确匹配取回内部 ID 后再查详情。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            result = await self._api.get_sales_order_detail(
                context,
                order_id.strip(),
            )
            return self.ok_response(order=result.order)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_sales_orders(
        self,
        page: int = 1,
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
    ) -> dict[str, Any]:
        """分页查询销售单列表，支持按日期、状态和客户筛选。

        适用于录单日常查询和客户单据查询场景。传 start_date 和
        end_date 可查指定时间段的单据；传 status 可筛选草稿、
        预收、已生效或已作废单据。

        Args:
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
            sort_by: 排序字段，如 updateTime、orderDate。
            order_type: 排序方向：asc 或 desc。
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            status: 单据状态：0=草稿 1=预收 2=已生效 3=已作废。
            payment_status: 收款状态：0=未收款 1=部分收款 2=已完成。
            return_status: 退货状态：0=无退货 1=部分退货 2=全部退货。
            order_no: 单据编号模糊匹配关键词。
            customer_id: 客户 ID。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            self._validate_date_range(start_date.strip(), end_date.strip())
            result = await self._api.search_sales_orders(
                context,
                page_num=max(1, int(page or 1)),
                page_size=max(1, min(int(page_size or 20), 100)),
                sort_by=sort_by.strip(),
                order_type=order_type.strip(),
                start_date=start_date.strip(),
                end_date=end_date.strip(),
                status=status,
                payment_status=payment_status,
                return_status=return_status,
                order_no=order_no.strip(),
                customer_id=customer_id.strip(),
            )
            return self.ok_response(
                page=result.page_num,
                page_size=result.page_size,
                total=result.total,
                has_more=(result.page_num * result.page_size) < result.total,
                orders=list(result.orders),
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def void_sales_order(
        self,
        order_id: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """作废销售单；只有用户明确确认后才能执行。

        作废后单据状态变为已作废，不可恢复。调用前建议先调用
        get_sales_order 向用户展示单据内容。成功后返回业务单号 order_no。

        Args:
            order_id: 销售单标识，同时接受内部 ID 和业务单号 orderNo
                （如 XS 开头），传入业务单号时自动回查内部 ID。
            confirmed_by_user: 仅在用户明确确认作废后传 true。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            if confirmed_by_user is not True:
                raise DomainError(
                    "erp_sales_order_confirmation_required",
                    "必须先向用户展示销售单详情并取得明确确认",
                )
            target_id = order_id.strip()
            if not target_id:
                raise DomainError(
                    "erp_sales_order_id_invalid",
                    "销售单 ID 不能为空",
                )
            # 先回查业务单号再作废：作废后详情可能不可查，回查失败时降级回显入参
            order_no = await self._lookup_order_no(target_id)
            await self._api.void_sales_order(context, target_id)
            return self.ok_response(voided=True, order_no=order_no or target_id)
        except DomainError as exc:
            return self.error_response(exc)

    async def update_sales_order(
        self,
        order_id: str,
        order_date: str,
        handler_id: str,
        items: list[dict[str, Any]],
        customer_id: str = "",
        warehouse_id: str = "",
        save_type: str = "draft",
        remark: str = "",
        discount_amount: float | None = None,
        discount_account_id: str = "",
        receipt_amount: float | None = None,
        receipt_account_id: str = "",
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """修改已存在的销售单；只有用户明确确认后才能执行。

        建议先调用 get_sales_order 获取当前数据，再做修改。
        已生效单据的客户和出库仓库不可修改。成功后返回业务单号 order_no。

        Args:
            order_id: 销售单标识，同时接受内部 ID 和业务单号 orderNo
                （如 XS 开头），传入业务单号时自动回查内部 ID。
            order_date: 单据日期，格式 YYYY-MM-DD。
            handler_id: 经手人内部 ID（get_sales_order 返回的
                handlerId）或经手人名称；传名称时必须唯一匹配。
            items: 商品明细列表，每个元素包含 product_id（必填）、
                quantity（必填）、unit、unit_price、order_item_id、remark 等。
            customer_id: 客户内部 ID 或名称（已生效状态不可修改）。
            warehouse_id: 出库仓库内部 ID 或名称（已生效状态不可修改）。
            save_type: 保存类型；draft 保持草稿/预收、pre_receipt 预收、final 转为正式过账。
            remark: 备注。
            discount_amount: 优惠金额（已生效状态不可修改）。
            discount_account_id: 优惠账户 ID（已生效状态不可修改）。
            receipt_amount: 本次追加收款金额（预收转正式过账时使用）。
            receipt_account_id: 收款账户 ID（预收转正式过账时使用）。
            confirmed_by_user: 仅在用户明确确认修改内容后传 true。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            if confirmed_by_user is not True:
                raise DomainError(
                    "erp_sales_order_confirmation_required",
                    "必须先向用户展示修改内容并取得明确确认",
                )
            target_id = order_id.strip()
            if not target_id:
                raise DomainError(
                    "erp_sales_order_id_invalid",
                    "销售单 ID 不能为空",
                )
            clean_date = order_date.strip()
            if not clean_date:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "录单日期不能为空",
                )
            self._validate_order_date(clean_date)
            clean_handler = handler_id.strip()
            if not clean_handler:
                raise DomainError(
                    "erp_sales_order_handler_required",
                    "经办人不能为空，可传内部 ID 或名称",
                )
            if len(remark.strip()) > 200:
                raise DomainError(
                    "erp_sales_order_remark_too_long",
                    "备注最多 200 个字符",
                )
            if save_type not in _SAVE_TYPE_CODES:
                raise DomainError(
                    "erp_sales_order_save_type_invalid",
                    "save_type 必须是 draft、pre_receipt 或 final",
                )
            order_items = self._build_modify_items(items)
            payload: dict[str, Any] = {
                "id": int(target_id) if target_id.isdigit() else target_id,
                "orderDate": clean_date,
                "handlerId": await self._resolve_update_reference(
                    "handler", clean_handler, "经手人",
                ),
                "items": order_items,
                "remark": remark.strip(),
                "saveType": _SAVE_TYPE_CODES[save_type],
            }
            clean_customer = customer_id.strip()
            if clean_customer:
                payload["customerId"] = await self._resolve_update_reference(
                    "customer", clean_customer, "客户",
                )
            clean_warehouse = warehouse_id.strip()
            if clean_warehouse:
                payload["warehouseId"] = await self._resolve_update_reference(
                    "warehouse", clean_warehouse, "出库仓库",
                )
            if discount_amount is not None:
                payload["discountAmount"] = float(discount_amount)
            if discount_account_id.strip():
                payload["discountAccountId"] = discount_account_id.strip()
            if receipt_amount is not None:
                payload["receiptAmount"] = float(receipt_amount)
            if receipt_account_id.strip():
                payload["receiptAccountId"] = receipt_account_id.strip()
            result = await self._api.update_sales_order(context, target_id, payload)
            order_no = await self._lookup_order_no(result.order_id)
            return self.ok_response(
                modified=True,
                order_no=order_no or result.order_id,
            )
        except DomainError as exc:
            return self.error_response(exc)

    @staticmethod
    def _public_reference_option(
        option: dict[str, Any],
    ) -> dict[str, Any]:
        """基础资料候选对外仅保留展示和推荐所需字段。"""
        return {
            "name": str(option.get("name") or ""),
            "is_default": bool(
                option.get("is_default") or option.get("isDefault"),
            ),
        }

    @staticmethod
    def _reference_sort_key(
        option: dict[str, Any],
        query: str,
    ) -> tuple[int, int, str]:
        """按精确、默认、名称包含、其他候选的顺序稳定排序。"""
        normalized_query = normalize_name(query)
        normalized_name = normalize_name(str(option.get("name") or ""))
        normalized_values = {
            normalize_name(str(option.get(key) or ""))
            for key in ("id", "code", "name")
        }
        if normalized_query and normalized_query in normalized_values:
            type_rank = 0
        elif option.get("is_default") or option.get("isDefault"):
            type_rank = 1
        else:
            type_rank = 2 if normalized_query else 3
        contains_rank = int(
            bool(normalized_query) and normalized_query not in normalized_name,
        )
        return type_rank, contains_rank, normalized_name

    @staticmethod
    def _public_submission_result(cached: dict[str, Any]) -> dict[str, Any]:
        """提交结果对外剥离 preview_id；幂等冲突检测只在内部使用它。"""
        return {
            key: value
            for key, value in cached.items()
            if key != "preview_id"
        }

    async def _lookup_order_no(self, order_id: str) -> str:
        """创建成功后回查详情，取用户可读的业务单号 orderNo。

        回查是尽力而为的增值信息：任何失败都不影响已创建的单据，
        调用方降级使用内部 ID。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            detail = await self._api.get_sales_order_detail(context, order_id)
        except DomainError:
            return ""
        return str(detail.order.get("orderNo") or "").strip()

    async def _search_reference(
        self,
        reference_type: str,
        keyword: str,
        limit: int,
        page: int = 1,
    ) -> BillingReferenceSnapshot:
        context = self._contexts.get()
        if reference_type == "customer":
            return await self._api.search_customers(context, keyword, limit, page)
        if reference_type == "warehouse":
            return await self._api.search_warehouses(context, keyword, limit, page)
        if reference_type == "handler":
            return await self._api.search_staff(context, keyword, limit, page)
        raise DomainError(
            "erp_reference_type_invalid",
            "reference_type 必须是 customer、warehouse 或 handler",
        )

    async def _resolve_reference(self, reference_type: str, value: str) -> dict[str, Any]:
        if not value:
            return {
                "status": "missing",
                "query": "",
                "selected": None,
                "candidates": [],
            }
        # 解析时多取一些结果保证精确项不因展示上限被截断，最终候选仍收敛为 5 个。
        options = list((await self._search_reference(reference_type, value, 10)).options)
        normalized = normalize_name(value)
        exact = [
            option
            for option in options
            if normalized
            in {
                normalize_name(str(option.get("id") or "")),
                normalize_name(str(option.get("code") or "")),
                normalize_name(str(option.get("name") or "")),
            }
        ]
        if len(exact) == 1:
            return {
                "status": "matched",
                "query": value,
                "selected": exact[0],
                "candidates": [],
            }
        # 无唯一精确匹配时，对同主体多业务类型候选去重，最多返回 5 个
        if options:
            options = self._deduplicate_reference_options(options, value)
        status = "ambiguous" if options else "unmatched"
        return {
            "status": status,
            "query": value,
            "selected": None,
            "candidates": options,
        }

    async def _resolve_update_reference(
        self,
        reference_type: str,
        value: str,
        label: str,
    ) -> str:
        """把修改单据的基础资料参数解析为内部 ID。

        纯数字直接视为内部 ID 透传（与销售单标识的既有约定一致）；
        其余按名称或编号解析，未唯一匹配时抛出结构化错误，由模型
        引导用户提供更准确的名称，而不是把错误标识写入 ERP。
        """
        if value.isdigit():
            return value
        resolution = await self._resolve_reference(reference_type, value)
        if resolution["status"] == "matched" and resolution["selected"] is not None:
            return str(resolution["selected"]["id"])
        candidate_names = "、".join(
            str(option.get("name") or "")
            for option in resolution["candidates"][:5]
            if option.get("name")
        )
        if candidate_names:
            raise DomainError(
                "erp_update_reference_ambiguous",
                "%s“%s”匹配到多个候选：%s；请提供更准确的名称或内部 ID"
                % (label, value, candidate_names),
            )
        raise DomainError(
            "erp_update_reference_unmatched",
            "未找到与“%s”匹配的%s；请提供准确的名称或内部 ID" % (value, label),
        )

    @classmethod
    def _deduplicate_reference_options(
        cls,
        options: list[dict[str, Any]],
        query: str,
    ) -> list[dict[str, Any]]:
        """按基础名称去重并按匹配质量排序，最多返回 5 个。

        ERP 常为同一客户/经手人返回多业务类型变体（COVR/SALE/PURC 等），
        名称仅在尾部后缀不同；按第一个分隔符前的名称去重后大幅减少候选数。
        """
        sorted_options = sorted(
            options,
            key=lambda option: cls._reference_sort_key(option, query),
        )
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for option in sorted_options:
            name = str(option.get("name") or "")
            base = re.split(r"[-\u2012-\u2015]|\(|\uff08|\u3010", name, maxsplit=1)[0].strip()
            base_key = (
                normalize_name(base) if base else normalize_name(name)
            )
            if not base_key or base_key in seen:
                continue
            seen.add(base_key)
            deduped.append(option)
        return deduped[:5]

    async def _match_order_products(
        self,
        order_text: str,
        source: str,
        confirmed_products: list[dict[str, str]] | None,
    ) -> BillingDraft | None:
        if not order_text:
            return None
        context = self._contexts.get()
        await self.session.ensure_catalog(self._catalog_loader(context))
        return self.session.create_draft_from_text(
            order_text,
            source=source,
            confirmed_products=confirmed_products,
        )

    @staticmethod
    def _validate_order_fields(
        *,
        order_date: str,
        remark: str,
        save_type: str,
        source: str,
    ) -> None:
        if order_date:
            try:
                parsed = date.fromisoformat(order_date)
            except ValueError as exc:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "录单日期必须使用 YYYY-MM-DD 格式",
                ) from exc
            if parsed.isoformat() != order_date:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "录单日期必须使用 YYYY-MM-DD 格式",
                )
        if len(remark.strip()) > 200:
            raise DomainError(
                "erp_sales_order_remark_too_long",
                "备注最多 200 个字符",
            )
        if save_type not in _SAVE_TYPE_CODES:
            raise DomainError(
                "erp_sales_order_save_type_invalid",
                "save_type 必须是 draft、pre_receipt 或 final",
            )
        if source not in {"text", "voice", "image"}:
            raise DomainError(
                "erp_order_source_invalid",
                "source 必须是 text、voice 或 image",
            )

    @staticmethod
    def _validate_order_date(order_date: str) -> None:
        """校验录单日期为合法的 YYYY-MM-DD 格式。"""
        try:
            parsed = date.fromisoformat(order_date)
        except ValueError as exc:
            raise DomainError(
                "erp_sales_order_date_invalid",
                "录单日期必须使用 YYYY-MM-DD 格式",
            ) from exc
        if parsed.isoformat() != order_date:
            raise DomainError(
                "erp_sales_order_date_invalid",
                "录单日期必须使用 YYYY-MM-DD 格式",
            )

    @staticmethod
    def _validate_date_range(start_date: str, end_date: str) -> None:
        """校验查询日期范围格式和先后顺序。"""
        parsed_start = None
        if start_date:
            try:
                parsed_start = date.fromisoformat(start_date)
            except ValueError as exc:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "开始日期必须使用 YYYY-MM-DD 格式",
                ) from exc
            if parsed_start.isoformat() != start_date:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "开始日期必须使用 YYYY-MM-DD 格式",
                )
        if end_date:
            try:
                parsed_end = date.fromisoformat(end_date)
            except ValueError as exc:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "结束日期必须使用 YYYY-MM-DD 格式",
                ) from exc
            if parsed_end.isoformat() != end_date:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "结束日期必须使用 YYYY-MM-DD 格式",
                )
            if parsed_start is not None and parsed_end < parsed_start:
                raise DomainError(
                    "erp_sales_order_date_invalid",
                    "结束日期不能早于开始日期",
                )

    @staticmethod
    def _build_modify_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """清洗修改商品明细，确保每行包含必填的 product_id 和 quantity。"""
        if not items:
            raise DomainError(
                "erp_sales_order_items_empty",
                "商品明细不能为空",
            )
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                raise DomainError(
                    "erp_sales_order_item_invalid",
                    "第%d行商品明细不是 JSON 对象" % index,
                )
            product_id = str(raw.get("product_id") or raw.get("productId") or "").strip()
            if not product_id:
                raise DomainError(
                    "erp_sales_order_item_invalid",
                    "第%d行商品明细缺少 product_id" % index,
                )
            quantity = raw.get("quantity")
            if quantity is None:
                raise DomainError(
                    "erp_sales_order_item_invalid",
                    "第%d行商品明细缺少 quantity" % index,
                )
            try:
                qty = float(quantity)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise DomainError(
                    "erp_sales_order_item_invalid",
                    "第%d行商品明细数量不是数字" % index,
                ) from exc
            if qty <= 0:
                raise DomainError(
                    "erp_sales_order_item_invalid",
                    "第%d行商品明细数量必须大于 0" % index,
                )
            item: dict[str, Any] = {"productId": product_id, "quantity": qty}
            field_map = {
                "unit": "unit",
                "unitPrice": "unit_price",
                "remark": "remark",
                "orderItemId": "order_item_id",
            }
            for camel_key, snake_key in field_map.items():
                value = raw.get(snake_key)
                if value is None:
                    value = raw.get(camel_key)
                if value not in (None, ""):
                    item[camel_key] = value
            result.append(item)
        return result

    @staticmethod
    def _unit_warnings(draft: BillingDraft | None) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        if draft is None:
            return warnings
        for line in draft.lines:
            if line.status != "matched" or line.product is None:
                continue
            requested_unit = _normalized_unit(line.order_line.unit)
            product_unit = _normalized_unit(line.product.unit)
            if requested_unit and product_unit and requested_unit != product_unit:
                warnings.append(
                    {
                        "line_id": line.order_line.line_id,
                        "product": line.product.name,
                        "requested_unit": line.order_line.unit,
                        "erp_unit": line.product.unit,
                        "prompt": "请按 ERP 商品单位重新确认数量，工具不会猜测单位换算。",
                    },
                )
        return warnings

    @staticmethod
    def _required_actions(
        *,
        missing: list[dict[str, Any]],
        reference_resolutions: dict[str, dict[str, Any]],
        product_payload: dict[str, Any],
        unit_warnings: list[dict[str, Any]],
        ready: bool,
    ) -> list[str]:
        """把分散的校验结果收敛为 Agent 可顺序执行的业务待办。"""
        if ready:
            return ["confirm_submit"]

        actions = ["provide_%s" % item["field"] for item in missing]
        for field, resolution in reference_resolutions.items():
            if resolution["status"] == "ambiguous":
                actions.append("select_%s" % field)
            elif resolution["status"] == "unmatched":
                actions.append("replace_%s" % field)
        if product_payload["recommended_products"]:
            actions.append("select_products")
        if product_payload["unmatched_products"]:
            actions.append("resolve_unmatched_products")
        if unit_warnings:
            actions.append("confirm_units")
        return actions

    @staticmethod
    def _build_sales_order_preview(
        *,
        draft: BillingDraft,
        references: dict[str, dict[str, Any]],
        order_date: str,
        remark: str,
        save_type: str,
        partial: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        customer = references["customer"]["selected"]
        warehouse = references["warehouse"]["selected"]
        handler = references["handler"]["selected"]
        items: list[dict[str, Any]] = []
        preview_items: list[dict[str, Any]] = []
        total_amount = Decimal("0")
        has_complete_amount = True
        for line in draft.lines:
            if line.product is None:
                if partial:
                    continue
                raise DomainError(
                    "erp_sales_order_product_unconfirmed",
                    "销售单仍有未确认商品，不能生成提交预览",
                )
            item: dict[str, Any] = {
                "productId": line.product.product_id,
                "quantity": line.order_line.quantity,
            }
            if line.product.unit:
                item["unit"] = line.product.unit
            if line.product.price is not None:
                item["unitPrice"] = line.product.price
            if line.order_line.note:
                item["remark"] = line.order_line.note
            items.append(item)
            preview_item = {
                "name": line.product.name,
                "quantity": line.order_line.quantity,
                "unit": line.product.unit or line.order_line.unit,
                "unit_price": line.product.price,
            }
            line_amount = _line_amount(
                line.order_line.quantity,
                line.product.price,
            )
            if line_amount is None:
                has_complete_amount = False
            else:
                preview_item["line_amount"] = float(line_amount)
                total_amount += line_amount
            preview_items.append(preview_item)
        payload = {
            "id": 0,
            "orderDate": order_date,
            "customerId": customer["id"],
            "warehouseId": warehouse["id"],
            "handlerId": handler["id"],
            "saveType": _SAVE_TYPE_CODES[save_type],
            "remark": remark,
            "items": items,
        }
        # 预览面向用户展示，基础资料只出现名称，不出现内部 ID
        preview = {
            "order_date": order_date,
            "customer": str(customer.get("name") or ""),
            "warehouse": str(warehouse.get("name") or ""),
            "handler": str(handler.get("name") or ""),
            "remark": remark,
            "save_type": save_type,
            "save_type_label": _SAVE_TYPE_LABELS[save_type],
            "items": preview_items,
        }
        if preview_items and has_complete_amount:
            preview["total_amount"] = float(total_amount)
        return payload, preview


def _normalized_unit(value: str) -> str:
    normalized = normalize_name(value)
    return {
        "公斤": "kg",
        "千克": "kg",
        "kg": "kg",
        "毫升": "ml",
        "ml": "ml",
        "升": "l",
        "l": "l",
    }.get(normalized, normalized)


def _line_amount(quantity: float, unit_price: float | None) -> Decimal | None:
    """按实际提交单价计算两位小数的预览行金额。"""
    if unit_price is None:
        return None
    amount = Decimal(str(quantity)) * Decimal(str(unit_price))
    if not amount.is_finite():
        return None
    return amount.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)

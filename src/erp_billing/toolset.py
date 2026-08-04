"""完整销售单流程的 AgentScope 工具集。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from gjp_common.context import InvocationContextStore
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
        "search_products",
        "search_sales_order_options",
        "prepare_sales_order",
        "submit_sales_order",
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
                ),
                SessionFunctionTool(
                    session.search_products,
                    is_read_only=True,
                ),
                SessionFunctionTool(
                    self.search_sales_order_options,
                    is_read_only=True,
                ),
                SessionFunctionTool(
                    self.prepare_sales_order,
                    is_read_only=True,
                    is_concurrency_safe=False,
                ),
                SessionFunctionTool(
                    self.submit_sales_order,
                    is_concurrency_safe=False,
                ),
            ],
            contexts=contexts,
            agent_tool_names=BILLING_MCP_TOOL_NAMES,
            mcp_tool_names=BILLING_MCP_TOOL_NAMES,
        )

    def sync_products(self, limit: int | None = None) -> dict[str, Any]:
        """同步当前已认证账号可见的 ERP 商品到本会话内存目录。

        Args:
            limit: 可选的最大商品数量。
        """
        try:
            synced_at = self._sync_catalog(limit)
            return self.ok_response(
                catalogVersion=synced_at,
                productCount=len(self.session.catalog.products),
            )
        except DomainError as exc:
            return self.error_response(exc)

    def _sync_catalog(self, limit: int | None = None) -> str:
        """从当前 ERP 账号拉取商品并替换会话内存目录。"""
        context = self._contexts.get()
        context.require_scope("billing:read")
        snapshot = self._api.fetch_products(context, limit)
        if not snapshot.products:
            raise DomainError(
                "ERP_LIVE_PRODUCT_EMPTY",
                "当前账号没有返回可用于开单的商品",
            )
        self.session.replace_products(snapshot.products)
        return datetime.now(timezone.utc).isoformat()

    def search_sales_order_options(
        self,
        option_type: str,
        keyword: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        """查询销售单的客户、出库仓库或经手人候选。

        Args:
            option_type: 基础资料类型：customer、warehouse 或 handler。
            keyword: 名称或编号关键词；留空时返回前一页可用项。
            limit: 最多返回的候选数，范围 1 到 20。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            effective_limit = max(1, min(int(limit or 10), 20))
            snapshot = self._search_reference(
                option_type,
                keyword.strip(),
                effective_limit,
            )
            return self.ok_response(
                optionType=option_type,
                keyword=keyword.strip(),
                options=list(snapshot.options),
            )
        except (DomainError, TypeError, ValueError) as exc:
            error = (
                exc
                if isinstance(exc, DomainError)
                else DomainError("ERP_REFERENCE_LIMIT_INVALID", "limit 必须是整数")
            )
            return self.error_response(error)

    def prepare_sales_order(
        self,
        order_text: str = "",
        customer: str = "",
        warehouse: str = "",
        handler: str = "",
        order_date: str = "",
        remark: str = "",
        save_type: str = "final",
        source: str = "text",
        confirmed_products: dict[str, str] | None = None,
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
            confirmed_products: 用户选择的推荐商品，键为 lineId，值为 ERP 商品 ID。
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

            draft = self._match_order_products(
                values["order_text"],
                source,
                confirmed_products,
            )
            product_payload = (
                draft.billing_products_payload()
                if draft is not None
                else {
                    "confirmedProducts": [],
                    "recommendedProducts": [],
                    "unmatchedProducts": [],
                }
            )

            reference_resolutions = {
                "customer": self._resolve_reference("customer", values["customer"]),
                "warehouse": self._resolve_reference("warehouse", values["warehouse"]),
                "handler": self._resolve_reference("handler", values["handler"]),
            }
            needs_confirmation = [
                {
                    "field": field,
                    "label": _field_label(field),
                    "query": resolution["query"],
                    "candidates": resolution["candidates"],
                }
                for field, resolution in reference_resolutions.items()
                if resolution["status"] not in {"matched", "missing"}
            ]
            unit_warnings = self._unit_warnings(draft)
            ready = bool(
                not missing
                and not needs_confirmation
                and not unit_warnings
                and draft is not None
                and draft.status == "ready"
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
                )
                preview_id = self.session.store_prepared_sales_order(payload, preview)

            return self.ok_response(
                fieldRequirements={
                    "required": [field for field, _, _ in _REQUIRED_FIELDS],
                    "optional": ["remark"],
                    "systemManaged": ["id", "saveType"],
                },
                missingRequiredFields=missing,
                referenceResolutions=reference_resolutions,
                needsConfirmation=needs_confirmation,
                unitWarnings=unit_warnings,
                **product_payload,
                readyToSubmit=ready,
                previewId=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    def submit_sales_order(
        self,
        preview_id: str,
        idempotency_key: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """用户明确确认预览后，把销售单写入真实 ERP。

        Args:
            preview_id: prepare_sales_order 返回的预览 ID。
            idempotency_key: 调用方为本次提交生成的唯一业务键，重试必须复用。
            confirmed_by_user: 仅在用户明确确认该预览后传 true。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            if confirmed_by_user is not True:
                raise DomainError(
                    "ERP_SALES_ORDER_CONFIRMATION_REQUIRED",
                    "必须先向用户展示销售单预览并取得明确确认",
                )
            key = idempotency_key.strip()
            if not key or len(key) > 128:
                raise DomainError(
                    "ERP_SALES_ORDER_IDEMPOTENCY_KEY_INVALID",
                    "idempotency_key 不能为空且最多 128 个字符",
                )
            cached = self.session.submission_result(key)
            if cached is not None:
                if cached["previewId"] != preview_id.strip():
                    raise DomainError(
                        "ERP_SALES_ORDER_IDEMPOTENCY_KEY_CONFLICT",
                        "该 idempotency_key 已用于另一份销售单预览",
                    )
                return self.ok_response(**cached, idempotentReplay=True)

            payload, preview = self.session.require_prepared_sales_order(preview_id)
            result = self._api.create_sales_order(context, payload)
            response = {
                "submitted": True,
                "orderId": result.order_id,
                "previewId": preview_id.strip(),
                "saveType": preview["saveType"],
            }
            self.session.remember_submission(key, response)
            return self.ok_response(**response, idempotentReplay=False)
        except DomainError as exc:
            return self.error_response(exc)

    def _search_reference(
        self,
        option_type: str,
        keyword: str,
        limit: int,
    ) -> BillingReferenceSnapshot:
        context = self._contexts.get()
        if option_type == "customer":
            return self._api.search_customers(context, keyword, limit)
        if option_type == "warehouse":
            return self._api.search_warehouses(context, keyword, limit)
        if option_type == "handler":
            return self._api.search_staff(context, keyword, limit)
        raise DomainError(
            "ERP_REFERENCE_TYPE_INVALID",
            "option_type 必须是 customer、warehouse 或 handler",
        )

    def _resolve_reference(self, option_type: str, value: str) -> dict[str, Any]:
        if not value:
            return {
                "status": "missing",
                "query": "",
                "selected": None,
                "candidates": [],
            }
        options = list(self._search_reference(option_type, value, 10).options)
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
        selected = exact[0] if len(exact) == 1 else None
        status = "matched" if selected else "ambiguous" if options else "unmatched"
        return {
            "status": status,
            "query": value,
            "selected": selected,
            "candidates": options if selected is None else [],
        }

    def _match_order_products(
        self,
        order_text: str,
        source: str,
        confirmed_products: dict[str, str] | None,
    ) -> BillingDraft | None:
        if not order_text:
            return None
        if not self.session.catalog.products:
            self._sync_catalog()
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
                    "ERP_SALES_ORDER_DATE_INVALID",
                    "录单日期必须使用 YYYY-MM-DD 格式",
                ) from exc
            if parsed.isoformat() != order_date:
                raise DomainError(
                    "ERP_SALES_ORDER_DATE_INVALID",
                    "录单日期必须使用 YYYY-MM-DD 格式",
                )
        if len(remark.strip()) > 200:
            raise DomainError(
                "ERP_SALES_ORDER_REMARK_TOO_LONG",
                "备注最多 200 个字符",
            )
        if save_type not in _SAVE_TYPE_CODES:
            raise DomainError(
                "ERP_SALES_ORDER_SAVE_TYPE_INVALID",
                "save_type 必须是 draft、pre_receipt 或 final",
            )
        if source not in {"text", "voice", "image"}:
            raise DomainError(
                "ERP_ORDER_SOURCE_INVALID",
                "source 必须是 text、voice 或 image",
            )

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
                        "lineId": line.order_line.line_id,
                        "product": line.product.name,
                        "requestedUnit": line.order_line.unit,
                        "erpUnit": line.product.unit,
                        "prompt": "请按 ERP 商品单位重新确认数量，工具不会猜测单位换算。",
                    },
                )
        return warnings

    @staticmethod
    def _build_sales_order_preview(
        *,
        draft: BillingDraft,
        references: dict[str, dict[str, Any]],
        order_date: str,
        remark: str,
        save_type: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        customer = references["customer"]["selected"]
        warehouse = references["warehouse"]["selected"]
        handler = references["handler"]["selected"]
        items: list[dict[str, Any]] = []
        preview_items: list[dict[str, Any]] = []
        for line in draft.lines:
            if line.product is None:
                raise DomainError(
                    "ERP_SALES_ORDER_PRODUCT_UNCONFIRMED",
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
            preview_items.append(
                {
                    "productId": line.product.product_id,
                    "name": line.product.name,
                    "quantity": line.order_line.quantity,
                    "unit": line.product.unit or line.order_line.unit,
                    "unitPrice": line.product.price,
                },
            )
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
        preview = {
            "orderDate": order_date,
            "customer": customer,
            "warehouse": warehouse,
            "handler": handler,
            "remark": remark,
            "saveType": save_type,
            "saveTypeLabel": _SAVE_TYPE_LABELS[save_type],
            "items": preview_items,
        }
        return payload, preview


def _field_label(field: str) -> str:
    return next(label for name, label, _ in _REQUIRED_FIELDS if name == field)


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

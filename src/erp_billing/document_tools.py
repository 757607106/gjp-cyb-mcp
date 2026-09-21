"""采购、退货、库存写单据与资金单据的两段式工具。

所有写单据沿用 preview → submit 确认模式：预览生成不可变 payload 并
返回 preview_id，用户明确确认后 submit 才写入真实 ERP；统一 saveType=2
保存过账，不暴露草稿态。实现为 BillingToolSet 的混入类，与销售单工具
共用 session、API 端口、基础资料解析与通用提交逻辑。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable

from gjp_common.context import InvocationContextStore
from gjp_common.errors import DomainError
from gjp_common.tools import SessionFunctionTool

from .catalog import normalize_name
from .ports import BillingApiPort
from .session import ErpBillingSession
from .validation import line_amount, non_negative_amount, positive_amount

_ERROR_OUTPUT_OBJECT = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
    },
    "additionalProperties": True,
}

# ---------------------------------------------------------------------------
# 输出 schema：顶层字段声明类型，嵌套对象保持宽松（additionalProperties=True）
# ---------------------------------------------------------------------------

_TEXT_DOCUMENT_PREVIEW_OUTPUT_SCHEMA = {
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
        "price_warnings": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
        "required_actions": {"type": "array", "items": {"type": "string"}},
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

_RETURN_PREVIEW_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "source_order": {"type": ["object", "null"], "additionalProperties": True},
        "items": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "required_actions": {"type": "array", "items": {"type": "string"}},
        "ready_to_submit": {"type": "boolean"},
        "preview_id": {"type": ["string", "null"]},
        "preview": {"type": ["object", "null"]},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_MONEY_PREVIEW_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "order": {"type": ["object", "null"], "additionalProperties": True},
        "reference_resolutions": {"type": "object", "additionalProperties": True},
        "required_actions": {"type": "array", "items": {"type": "string"}},
        "ready_to_submit": {"type": "boolean"},
        "preview_id": {"type": ["string", "null"]},
        "preview": {"type": ["object", "null"]},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_DOCUMENT_SUBMIT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "submitted": {"type": "boolean"},
        "document_id": {"type": "string"},
        "document_no": {"type": "string"},
        "idempotent_replay": {"type": "boolean"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_DOCUMENT_GET_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "document": {"type": ["object", "null"], "additionalProperties": True},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_DOCUMENT_LIST_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
        "total": {"type": "integer"},
        "has_more": {"type": "boolean"},
        "documents": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": True},
        },
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_DOCUMENT_VOID_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "voided": {"type": "boolean"},
        "document_no": {"type": "string"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

_DOCUMENT_UPDATE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "error": _ERROR_OUTPUT_OBJECT,
        "modified": {"type": "boolean"},
        "document_no": {"type": "string"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}

# ---------------------------------------------------------------------------
# 输入 schema：为枚举和范围参数补充 JSON Schema 约束
# ---------------------------------------------------------------------------

_CONFIRMED_PRODUCTS_INPUT = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "line_id": {"type": "string"},
            "product_id": {"type": "string"},
        },
        "required": ["line_id", "product_id"],
    },
}

_CONFIRMED_UNITS_INPUT = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "line_id": {"type": "string"},
            "product_id": {"type": "string"},
            "unit": {"type": "string"},
            "quantity": {"type": "number", "minimum": 0.0001},
        },
        "required": ["line_id", "product_id", "unit", "quantity"],
        "additionalProperties": False,
    },
}

_CONFIRMED_PRICES_INPUT = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "line_id": {"type": "string"},
            "product_id": {"type": "string"},
            "unit_price": {"type": "number", "exclusiveMinimum": 0},
        },
        "required": ["line_id", "product_id", "unit_price"],
        "additionalProperties": False,
    },
}

_RETURN_ITEMS_INPUT = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "product_id": {"type": "string"},
            "quantity": {"type": "number", "minimum": 0.0001},
            "unit_price": {"type": "number", "minimum": 0},
        },
        "required": ["product_id", "quantity"],
        "additionalProperties": False,
    },
}

_WRITEOFF_DETAILS_INPUT = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "biz_type": {
                "type": "string",
                "enum": ["sales_order", "purchase_order", "sales_return", "purchase_return"],
            },
            "biz_id": {"type": "string"},
            "writeoff_amount": {"type": "number", "minimum": 0},
        },
        "required": ["biz_type", "biz_id", "writeoff_amount"],
        "additionalProperties": False,
    },
}

_PREVIEW_PURCHASE_ORDER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_text": {"type": "string", "description": "完整商品文本，含商品和数量"},
        "supplier": {"type": "string", "description": "供应商名称、编号或 ID"},
        "warehouse": {"type": "string", "description": "入库仓库名称、编号或 ID"},
        "handler": {"type": "string", "description": "经手人名称、编号或 ID"},
        "order_date": {"type": "string", "description": "YYYY-MM-DD"},
        "remark": {"type": "string"},
        "confirmed_products": _CONFIRMED_PRODUCTS_INPUT,
        "confirmed_units": _CONFIRMED_UNITS_INPUT,
        "confirmed_prices": _CONFIRMED_PRICES_INPUT,
        "partial": {"type": "boolean", "default": False},
    },
}

_LIST_PURCHASE_ORDERS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
        "status": {"type": "integer", "enum": [0, 1, 2, 3]},
        "payment_status": {"type": "integer", "enum": [0, 1, 2]},
        "return_status": {"type": "integer", "enum": [0, 1, 2]},
        "order_no": {"type": "string"},
        "supplier_id": {"type": "string"},
    },
}

_UPDATE_PURCHASE_ORDER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "内部 ID 或业务单号 orderNo"},
        "order_date": {"type": "string", "description": "YYYY-MM-DD"},
        "handler_id": {"type": "string", "description": "经手人内部 ID 或名称"},
        "supplier_id": {"type": "string", "description": "供应商内部 ID 或名称"},
        "warehouse_id": {"type": "string", "description": "入库仓库内部 ID 或名称"},
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
                "required": ["product_id", "quantity", "unit_price"],
            },
        },
        "remark": {"type": "string"},
        "discount_amount": {"type": "number"},
        "discount_account_id": {"type": "string"},
        "payment_amount": {"type": "number"},
        "payment_account_id": {"type": "string"},
        "confirmed_by_user": {"type": "boolean", "default": False},
    },
    "required": ["order_id"],
}

_PREVIEW_PURCHASE_RETURN_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "采购单内部 ID 或业务单号 orderNo"},
        "items": {
            "type": "array",
            "description": "退货明细；不传默认整单退货（全部商品原数量）",
            "items": _RETURN_ITEMS_INPUT["items"],
        },
        "refund_amount": {"type": "number", "minimum": 0, "description": "退货同时收到的退款金额"},
        "refund_account_id": {"type": "string", "description": "退款收款账户"},
        "discount_amount": {"type": "number", "minimum": 0, "description": "优惠/减免金额"},
        "discount_account_id": {"type": "string", "description": "优惠承担账户"},
        "return_date": {"type": "string", "description": "YYYY-MM-DD，默认源单日期或当天"},
        "remark": {"type": "string"},
    },
    "required": ["order_id"],
}

_LIST_PURCHASE_RETURNS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
        "status": {"type": "integer", "enum": [0, 1, 2, 3]},
        "return_no": {"type": "string"},
        "supplier_id": {"type": "string"},
    },
}

_PREVIEW_SALES_RETURN_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "销售单内部 ID 或业务单号 orderNo"},
        "items": {
            "type": "array",
            "description": "退货明细；不传默认整单退货（全部商品原数量）",
            "items": _RETURN_ITEMS_INPUT["items"],
        },
        "refund_amount": {"type": "number", "minimum": 0, "description": "退货同时退给客户的金额"},
        "refund_account_id": {"type": "string", "description": "退款账户"},
        "discount_amount": {"type": "number", "minimum": 0, "description": "折让金额"},
        "discount_account_id": {"type": "string", "description": "折让承担账户"},
        "return_date": {"type": "string", "description": "YYYY-MM-DD，默认源单日期或当天"},
        "remark": {"type": "string"},
    },
    "required": ["order_id"],
}

_LIST_SALES_RETURNS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
        "status": {"type": "integer", "enum": [0, 1, 2, 3]},
        "refund_status": {"type": "integer", "enum": [0, 1, 2]},
        "return_no": {"type": "string"},
        "customer_id": {"type": "string"},
    },
}

_PREVIEW_SALES_RECEIPT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "销售单内部 ID 或业务单号 orderNo"},
        "receipt_amount": {"type": "number", "minimum": 0, "description": "本次收款金额"},
        "receipt_account": {"type": "string", "description": "收款结算账户名称、编号或 ID"},
        "discount_amount": {"type": "number", "minimum": 0, "description": "免账金额（优惠/折让）"},
        "discount_account": {"type": "string", "description": "免账承担账户"},
    },
    "required": ["order_id", "receipt_amount", "receipt_account"],
}

_PREVIEW_PURCHASE_PAYMENT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "description": "采购单内部 ID 或业务单号 orderNo"},
        "payment_amount": {"type": "number", "minimum": 0, "description": "本次付款金额"},
        "payment_account": {"type": "string", "description": "付款结算账户名称、编号或 ID"},
        "discount_amount": {"type": "number", "minimum": 0, "description": "免账金额（优惠/折让）"},
        "discount_account": {"type": "string", "description": "免账承担账户"},
    },
    "required": ["order_id", "payment_amount", "payment_account"],
}

_PREVIEW_STOCK_TRANSFER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "order_text": {"type": "string", "description": "调拨商品文本，含商品和数量"},
        "from_warehouse": {"type": "string", "description": "调出仓库名称、编号或 ID"},
        "to_warehouse": {"type": "string", "description": "调入仓库名称、编号或 ID"},
        "handler": {"type": "string", "description": "经手人名称、编号或 ID"},
        "transfer_date": {"type": "string", "description": "YYYY-MM-DD，默认当天"},
        "remark": {"type": "string"},
        "confirmed_products": _CONFIRMED_PRODUCTS_INPUT,
        "confirmed_units": _CONFIRMED_UNITS_INPUT,
        "partial": {"type": "boolean", "default": False},
    },
}

_PREVIEW_OTHER_STOCK_DOC_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["inbound", "outbound"]},
        "order_text": {"type": "string", "description": "商品文本，含商品和数量"},
        "warehouse": {"type": "string", "description": "仓库名称、编号或 ID"},
        "handler": {"type": "string", "description": "经手人名称、编号或 ID"},
        "doc_type": {"type": "string", "description": "入库/出库类型 ID 或名称（如报损、报溢）"},
        "doc_date": {"type": "string", "description": "YYYY-MM-DD，默认当天"},
        "remark": {"type": "string"},
        "confirmed_products": _CONFIRMED_PRODUCTS_INPUT,
        "confirmed_units": _CONFIRMED_UNITS_INPUT,
        "partial": {"type": "boolean", "default": False},
    },
    "required": ["kind"],
}

_PREVIEW_RECEIPT_ORDER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "receipt_amount": {"type": "number", "minimum": 0.01, "description": "收款总额"},
        "account": {"type": "string", "description": "收款结算账户名称、编号或 ID"},
        "handler": {"type": "string", "description": "经手人名称、编号或 ID"},
        "customer": {"type": "string", "description": "客户名称或 ID；核销销售单时必填"},
        "supplier": {"type": "string", "description": "供应商名称或 ID；核销采购退货单时使用"},
        "order_date": {"type": "string", "description": "YYYY-MM-DD，默认当天"},
        "discount_amount": {"type": "number", "minimum": 0, "description": "优惠（抹零）金额"},
        "discount_account": {"type": "string", "description": "免账承担账户"},
        "fund_type": {
            "type": "string",
            "description": "款项类型名称、编号或 ID；不传且无核销时返回候选列表",
        },
        "writeoff_details": {
            "type": "array",
            "description": "核销明细；不传则只收款不核销",
            "items": _WRITEOFF_DETAILS_INPUT["items"],
        },
        "remark": {"type": "string"},
    },
    "required": ["receipt_amount", "account", "handler"],
}

_PREVIEW_PAYMENT_ORDER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "payment_amount": {"type": "number", "minimum": 0.01, "description": "付款总额"},
        "account": {"type": "string", "description": "付款结算账户名称、编号或 ID"},
        "handler": {"type": "string", "description": "经手人名称、编号或 ID"},
        "supplier": {"type": "string", "description": "供应商名称或 ID；核销采购单时必填"},
        "customer": {"type": "string", "description": "客户名称或 ID；核销销售退货单时使用"},
        "order_date": {"type": "string", "description": "YYYY-MM-DD，默认当天"},
        "discount_amount": {"type": "number", "minimum": 0, "description": "优惠（抹零）金额"},
        "discount_account": {"type": "string", "description": "免账承担账户"},
        "fund_type": {
            "type": "string",
            "description": "款项类型名称、编号或 ID；不传且无核销时返回候选列表",
        },
        "writeoff_details": {
            "type": "array",
            "description": "核销明细；不传则只付款不核销",
            "items": _WRITEOFF_DETAILS_INPUT["items"],
        },
        "remark": {"type": "string"},
    },
    "required": ["payment_amount", "account", "handler"],
}

_LIST_FINANCIAL_ORDERS_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "page": {"type": "integer", "minimum": 1, "default": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
        "status": {"type": "integer", "enum": [0, 2, 3]},
        "order_no": {"type": "string"},
        "counterparty_id": {"type": "string", "description": "客户或供应商内部 ID、名称"},
        "account_id": {"type": "string"},
    },
}

# 款项类型方向与核销场景推断的系统类型编码：
# ERP 收付款单 typeId 必填；系统类型按核销业务唯一确定。
_FUND_TYPE_DIRECTION_CODES = {"receipt": 1, "payment": 2}
_FUND_TYPE_AUTO_CODES = {
    ("receipt", "sales_order"): "sales",
    ("receipt", "purchase_return"): "purchase_refund",
    ("payment", "purchase_order"): "purchase",
    ("payment", "sales_return"): "sales_refund",
}


class DocumentTools:
    """采购单、退货、库存写单据与资金单据的两段式工具。

    以混入类并入 BillingToolSet：session、_api、_contexts、基础资料解析
    （_resolve_reference 等）与通用提交（_submit_prepared_document）
    由宿主 ToolSet 提供。
    """

    session: ErpBillingSession
    _api: BillingApiPort
    _contexts: InvocationContextStore

    # ------------------------------------------------------------------
    # 采购单
    # ------------------------------------------------------------------

    async def preview_purchase_order(
        self,
        order_text: str = "",
        supplier: str = "",
        warehouse: str = "",
        handler: str = "",
        order_date: str = "",
        remark: str = "",
        confirmed_products: list[dict[str, str]] | None = None,
        confirmed_units: list[dict[str, Any]] | None = None,
        confirmed_prices: list[dict[str, Any]] | None = None,
        partial: bool = False,
    ) -> dict[str, Any]:
        """校验完整采购单信息、匹配商品并生成不可变提交预览。

        采购价默认取商品目录的最近采购价；目录无有效采购价（为空或 0，
        ERP 拒绝 0 价采购行）的商品会出现在 price_warnings 中，需通过
        confirmed_prices 确认单价后重新预览。

        Args:
            order_text: 完整商品文本，必须包含商品和数量；多轮修改后传完整内容。
            supplier: 必填，供应商名称、编号或候选返回的 ID。
            warehouse: 必填，入库仓库名称、编号或候选返回的 ID。
            handler: 必填，经手人名称、编号或候选返回的 ID。
            order_date: 必填，录单日期，格式 YYYY-MM-DD。
            remark: 可选的整单备注，最多 200 个字符。
            confirmed_products: 用户确认的商品列表，元素格式为
                {"line_id": "L001", "product_id": "ERP商品ID"}。
            confirmed_units: 用户按 ERP 单位确认后的行数据，含 line_id、
                product_id、unit、quantity。
            confirmed_prices: 用户确认的采购单价，元素格式为
                {"line_id": "L001", "product_id": "ERP商品ID", "unit_price": 3.5}。
            partial: 为 true 时只提交已匹配商品，跳过未匹配行。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            values = {
                "supplier": supplier.strip(),
                "warehouse": warehouse.strip(),
                "handler": handler.strip(),
                "order_date": order_date.strip(),
                "order_text": order_text.strip(),
            }
            if values["order_date"]:
                self._validate_order_date(values["order_date"])
            if len(remark.strip()) > 200:
                raise DomainError("erp_purchase_order_remark_too_long", "备注最多 200 个字符")
            missing = self._missing_fields(
                values,
                (
                    ("supplier", "供应商", "请问采购供应商是哪一位？"),
                    ("warehouse", "入库仓库", "请问入到哪个仓库？"),
                    ("handler", "经手人", "请问本单经手人是谁？"),
                    ("order_date", "录单日期", "请问录单日期是哪一天？"),
                    ("order_text", "商品明细", "请提供商品、数量和单位。"),
                ),
            )

            draft = await self._match_order_products(
                values["order_text"], "text", confirmed_products,
            )
            self._apply_confirmed_units(draft, confirmed_units)
            prices = self._normalize_confirmed_prices(draft, confirmed_prices)
            product_payload = (
                draft.billing_products_payload()
                if draft is not None
                else {
                    "confirmed_products": [],
                    "recommended_products": [],
                    "unmatched_products": [],
                }
            )
            price_warnings = self._purchase_price_warnings(draft, prices)

            reference_resolutions = {
                "supplier": await self._resolve_reference("supplier", values["supplier"]),
                "warehouse": await self._resolve_reference("warehouse", values["warehouse"]),
                "handler": await self._resolve_reference("handler", values["handler"]),
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
                and not price_warnings
                and draft is not None
                and (draft.status == "ready" or (partial and has_matched))
            )

            payload: dict[str, Any] | None = None
            preview: dict[str, Any] | None = None
            if ready and draft is not None:
                payload, preview = self._build_purchase_order_preview(
                    draft=draft,
                    references=reference_resolutions,
                    order_date=values["order_date"],
                    remark=remark.strip(),
                    prices=prices,
                    partial=partial,
                )
            return self._finalize_document_preview(
                kind="purchase_order",
                missing=missing,
                reference_resolutions=reference_resolutions,
                unit_warnings=unit_warnings,
                price_warnings=price_warnings,
                product_payload=product_payload,
                ready=ready,
                payload=payload,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_purchase_order(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """用户明确确认预览后，把采购单写入真实 ERP（保存过账）。

        Args:
            preview_id: preview_purchase_order 返回的预览 ID；提交成功后失效。
            idempotency_key: 可省略，默认使用 preview_id；显式指定时重试必须复用。
            confirmed_by_user: 仅在用户明确确认该预览后传 true。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            result = await self._api.create_purchase_order(context, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="purchase_order",
            doc_label="采购单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def get_purchase_order(self, order_id: str) -> dict[str, Any]:
        """查询采购单详情，含商品明细、付款记录和状态。

        Args:
            order_id: 采购单标识，同时接受内部 ID 和业务单号 orderNo。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if not order_id.strip():
                raise DomainError("erp_purchase_order_id_invalid", "采购单 ID 不能为空")
            result = await self._api.get_purchase_order_detail(context, order_id.strip())
            return self.ok_response(document=result.document)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_purchase_orders(
        self,
        page: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        payment_status: int | None = None,
        return_status: int | None = None,
        order_no: str = "",
        supplier_id: str = "",
    ) -> dict[str, Any]:
        """分页查询采购单列表，支持按日期、状态和供应商筛选。

        Args:
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            status: 单据状态：0=草稿 1=预付 2=已生效 3=已作废。
            payment_status: 付款状态：0=未付款 1=部分付款 2=已完成。
            return_status: 退货状态：0=无退货 1=部分退货 2=全部退货。
            order_no: 单据编号模糊匹配关键词。
            supplier_id: 供应商 ID 或名称。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            self._validate_date_range(start_date.strip(), end_date.strip())
            supplier = await self._optional_reference_id("supplier", supplier_id)
            result = await self._api.search_purchase_orders(
                context,
                page_num=max(1, int(page or 1)),
                page_size=max(1, min(int(page_size or 20), 100)),
                start_date=start_date.strip(),
                end_date=end_date.strip(),
                status=status,
                payment_status=payment_status,
                return_status=return_status,
                order_no=order_no.strip(),
                supplier_id=supplier,
            )
            return self._document_page_response(result)
        except DomainError as exc:
            return self.error_response(exc)

    async def void_purchase_order(
        self,
        order_id: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """作废采购单；只有用户明确确认后才能执行。

        作废后单据状态变为已作废，不可恢复。调用前建议先调用
        get_purchase_order 向用户展示单据内容。

        Args:
            order_id: 采购单标识，同时接受内部 ID 和业务单号 orderNo。
            confirmed_by_user: 仅在用户明确确认作废后传 true。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            document_no = await self._void_document_flow(
                "purchase_order",
                "采购单",
                order_id,
                confirmed_by_user,
                lambda resolved: self._api.void_purchase_order(context, resolved),
            )
            return self.ok_response(voided=True, document_no=document_no)
        except DomainError as exc:
            return self.error_response(exc)

    async def update_purchase_order(
        self,
        order_id: str,
        order_date: str | None = None,
        handler_id: str | None = None,
        items: list[dict[str, Any]] | None = None,
        supplier_id: str = "",
        warehouse_id: str = "",
        remark: str | None = None,
        discount_amount: float | None = None,
        discount_account_id: str = "",
        payment_amount: float | None = None,
        payment_account_id: str = "",
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """修改已存在的采购单；只有用户明确确认后才能执行。

        仅传需要修改的字段；省略字段由适配器保留 ERP 当前值。
        items 传入时替换完整明细（每行必填 product_id、quantity、unit_price）。

        Args:
            order_id: 采购单标识，同时接受内部 ID 和业务单号 orderNo。
            order_date: 单据日期，格式 YYYY-MM-DD。
            handler_id: 经手人内部 ID 或名称。
            items: 商品明细列表，每行含 product_id、quantity、unit_price。
            supplier_id: 供应商内部 ID 或名称。
            warehouse_id: 入库仓库内部 ID 或名称。
            remark: 备注；传空字符串表示清空。
            discount_amount: 优惠金额。
            discount_account_id: 优惠账户 ID。
            payment_amount: 付款金额。
            payment_account_id: 付款账户 ID。
            confirmed_by_user: 仅在用户明确确认修改内容后传 true。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            if confirmed_by_user is not True:
                raise DomainError(
                    "erp_document_confirmation_required",
                    "必须先向用户展示修改内容并取得明确确认",
                )
            target_id = order_id.strip()
            if not target_id:
                raise DomainError("erp_purchase_order_id_invalid", "采购单 ID 不能为空")
            payload: dict[str, Any] = {"id": target_id}
            if order_date is not None:
                if not order_date.strip():
                    raise DomainError("erp_purchase_order_date_invalid", "录单日期不能为空")
                self._validate_order_date(order_date.strip())
                payload["orderDate"] = order_date.strip()
            if handler_id is not None:
                if not handler_id.strip():
                    raise DomainError("erp_purchase_order_handler_required", "经办人不能为空")
                payload["handlerId"] = await self._resolve_update_reference(
                    "handler", handler_id.strip(), "经手人",
                )
            if remark is not None:
                if len(remark.strip()) > 200:
                    raise DomainError("erp_purchase_order_remark_too_long", "备注最多 200 个字符")
                payload["remark"] = remark.strip()
            if items is not None:
                payload["items"] = self._build_purchase_modify_items(items)
            clean_supplier = supplier_id.strip()
            if clean_supplier:
                payload["supplierId"] = await self._resolve_update_reference(
                    "supplier", clean_supplier, "供应商",
                )
            clean_warehouse = warehouse_id.strip()
            if clean_warehouse:
                payload["warehouseId"] = await self._resolve_update_reference(
                    "warehouse", clean_warehouse, "入库仓库",
                )
            if discount_amount is not None:
                payload["discountAmount"] = non_negative_amount(discount_amount, "优惠金额")
            if discount_account_id.strip():
                payload["discountAccountId"] = discount_account_id.strip()
            if payment_amount is not None:
                payload["paymentAmount"] = non_negative_amount(payment_amount, "付款金额")
            if payment_account_id.strip():
                payload["paymentAccountId"] = payment_account_id.strip()
            if len(payload) == 1:
                raise DomainError("erp_purchase_order_update_empty", "请提供需要修改的字段")
            result = await self._api.update_purchase_order(context, target_id, payload)
            document_no = await self._lookup_document_no("purchase_order", result.document_id)
            return self.ok_response(
                modified=True,
                document_no=document_no or result.document_id,
            )
        except DomainError as exc:
            return self.error_response(exc)

    # ------------------------------------------------------------------
    # 采购退货单
    # ------------------------------------------------------------------

    async def preview_purchase_return(
        self,
        order_id: str,
        items: list[dict[str, Any]] | None = None,
        refund_amount: float | None = None,
        refund_account_id: str = "",
        discount_amount: float | None = None,
        discount_account_id: str = "",
        return_date: str = "",
        remark: str = "",
    ) -> dict[str, Any]:
        """基于采购单生成退货预览；不传 items 默认整单退货。

        先回读采购单快捷退货数据（供应商、仓库、经手人自动带出），
        用户只需确认退货商品和数量。

        Args:
            order_id: 采购单标识，同时接受内部 ID 和业务单号 orderNo。
            items: 退货明细，每行含 product_id、quantity，可选 unit_price；
                不传默认整单退货。
            refund_amount: 退货同时收到的退款金额，可选。
            refund_account_id: 退款收款账户；退款金额大于 0 时必填。
            discount_amount: 优惠/减免金额，可选。
            discount_account_id: 优惠承担账户；优惠金额大于 0 时必填。
            return_date: 退货日期 YYYY-MM-DD；默认源单日期或当天。
            remark: 备注。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            token = order_id.strip()
            if not token:
                raise DomainError("erp_purchase_order_id_invalid", "采购单 ID 不能为空")
            prefill = (await self._api.get_purchase_order_quick_return(context, token)).document
            source_items = self._quick_return_items(prefill)
            if not source_items:
                raise DomainError("erp_purchase_return_items_empty", "采购单没有可退货的商品明细")
            selected = self._select_return_items(source_items, items, "采购单")
            if return_date.strip():
                self._validate_order_date(return_date.strip())
            effective_date = (
                return_date.strip()
                or str(prefill.get("returnDate") or "").strip()
                or date.today().isoformat()
            )
            total_amount = self._return_total_amount(selected)
            refund, discount, refund_account, discount_account, ready = (
                await self._resolve_return_money(
                    total_amount,
                    refund_amount,
                    refund_account_id,
                    discount_amount,
                    discount_account_id,
                )
            )
            payload: dict[str, Any] = {
                "id": 0,
                "returnDate": effective_date,
                "sourceOrderId": str(prefill.get("sourceOrderId") or ""),
                "sourceOrderNo": str(prefill.get("sourceOrderNo") or ""),
                "supplierId": str(prefill.get("supplierId") or ""),
                "supplierName": str(prefill.get("supplierName") or ""),
                "warehouseId": str(prefill.get("warehouseId") or ""),
                "warehouseName": str(prefill.get("warehouseName") or ""),
                "handlerId": str(prefill.get("handlerId") or ""),
                "handlerName": str(prefill.get("handlerName") or ""),
                "saveType": 2,
                "remark": remark.strip(),
                "items": [
                    {
                        "productId": item["product_id"],
                        "quantity": item["quantity"],
                        "unitPrice": item["unit_price"],
                        **({"unit": item["unit"]} if item["unit"] else {}),
                    }
                    for item in selected
                ],
            }
            if prefill.get("sourcePaidAmount") is not None:
                payload["sourcePaidAmount"] = prefill.get("sourcePaidAmount")
            if prefill.get("sourcePayableAmount") is not None:
                payload["sourcePayableAmount"] = prefill.get("sourcePayableAmount")
            if refund is not None and refund > 0 and refund_account is not None:
                payload["paymentAmount"] = refund
                payload["paymentAccountId"] = str(refund_account["id"])
                payload["paymentAccountName"] = str(refund_account.get("name") or "")
            if discount is not None and discount > 0 and discount_account is not None:
                payload["discountAmount"] = discount
                payload["discountAccountId"] = str(discount_account["id"])
                payload["discountAccountName"] = str(discount_account.get("name") or "")
            preview = self._build_return_preview_summary(
                document_kind="purchase_return",
                source_order_no=str(prefill.get("sourceOrderNo") or ""),
                counterparty_label="供应商",
                counterparty_name=str(prefill.get("supplierName") or ""),
                warehouse_name=str(prefill.get("warehouseName") or ""),
                handler_name=str(prefill.get("handlerName") or ""),
                doc_date=effective_date,
                items=selected,
                total_amount=total_amount,
                refund_label="退款金额",
                refund_amount=refund,
                refund_account=refund_account,
                discount_amount=discount,
                discount_account=discount_account,
                remark=remark.strip(),
            )
            required_actions = ["confirm_submit"] if ready else []
            preview_id = None
            if ready:
                preview_id = self.session.store_prepared_document("purchase_return", payload, preview)
            return self.ok_response(
                source_order={
                    "order_id": str(prefill.get("sourceOrderId") or ""),
                    "order_no": str(prefill.get("sourceOrderNo") or ""),
                    "supplier": str(prefill.get("supplierName") or ""),
                    "warehouse": str(prefill.get("warehouseName") or ""),
                },
                items=[
                    {
                        "product_id": item["product_id"],
                        "product_name": item["product_name"],
                        "quantity": item["quantity"],
                        "unit": item["unit"],
                        "unit_price": item["unit_price"],
                    }
                    for item in selected
                ],
                required_actions=required_actions,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_purchase_return(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """用户明确确认预览后，把采购退货单写入真实 ERP（保存过账）。

        Args:
            preview_id: preview_purchase_return 返回的预览 ID；提交成功后失效。
            idempotency_key: 可省略，默认使用 preview_id；显式指定时重试必须复用。
            confirmed_by_user: 仅在用户明确确认该预览后传 true。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            result = await self._api.create_purchase_return(context, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="purchase_return",
            doc_label="采购退货单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def get_purchase_return(self, return_id: str) -> dict[str, Any]:
        """查询采购退货单详情。

        Args:
            return_id: 采购退货单标识，同时接受内部 ID 和业务单号 returnNo。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if not return_id.strip():
                raise DomainError("erp_purchase_return_id_invalid", "采购退货单 ID 不能为空")
            result = await self._api.get_purchase_return_detail(context, return_id.strip())
            return self.ok_response(document=result.document)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_purchase_returns(
        self,
        page: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        return_no: str = "",
        supplier_id: str = "",
    ) -> dict[str, Any]:
        """分页查询采购退货单列表。

        Args:
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            status: 单据状态：0=草稿 2=已生效 3=已作废。
            return_no: 退货单编号模糊匹配关键词。
            supplier_id: 供应商 ID 或名称。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            self._validate_date_range(start_date.strip(), end_date.strip())
            supplier = await self._optional_reference_id("supplier", supplier_id)
            result = await self._api.search_purchase_returns(
                context,
                page_num=max(1, int(page or 1)),
                page_size=max(1, min(int(page_size or 20), 100)),
                start_date=start_date.strip(),
                end_date=end_date.strip(),
                status=status,
                return_no=return_no.strip(),
                supplier_id=supplier,
            )
            return self._document_page_response(result)
        except DomainError as exc:
            return self.error_response(exc)

    async def void_purchase_return(
        self,
        return_id: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """作废采购退货单；只有用户明确确认后才能执行。

        Args:
            return_id: 采购退货单标识，同时接受内部 ID 和业务单号 returnNo。
            confirmed_by_user: 仅在用户明确确认作废后传 true。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            document_no = await self._void_document_flow(
                "purchase_return",
                "采购退货单",
                return_id,
                confirmed_by_user,
                lambda resolved: self._api.void_purchase_return(context, resolved),
            )
            return self.ok_response(voided=True, document_no=document_no)
        except DomainError as exc:
            return self.error_response(exc)

    # ------------------------------------------------------------------
    # 销售退货单
    # ------------------------------------------------------------------

    async def preview_sales_return(
        self,
        order_id: str,
        items: list[dict[str, Any]] | None = None,
        refund_amount: float | None = None,
        refund_account_id: str = "",
        discount_amount: float | None = None,
        discount_account_id: str = "",
        return_date: str = "",
        remark: str = "",
    ) -> dict[str, Any]:
        """基于销售单生成退货预览；不传 items 默认整单退货。

        先回读销售单快捷退货数据（客户、仓库、经手人自动带出），
        用户只需确认退货商品、数量和退款方式。

        Args:
            order_id: 销售单标识，同时接受内部 ID 和业务单号 orderNo。
            items: 退货明细，每行含 product_id、quantity，可选 unit_price；
                不传默认整单退货。
            refund_amount: 退货同时退给客户的金额，可选。
            refund_account_id: 退款账户；退款金额大于 0 时必填。
            discount_amount: 折让金额，可选。
            discount_account_id: 折让承担账户；折让金额大于 0 时必填。
            return_date: 退货日期 YYYY-MM-DD；默认源单日期或当天。
            remark: 备注。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            token = order_id.strip()
            if not token:
                raise DomainError("erp_sales_order_id_invalid", "销售单 ID 不能为空")
            prefill = (await self._api.get_sales_order_quick_return(context, token)).document
            source_items = self._quick_return_items(prefill)
            if not source_items:
                raise DomainError("erp_sales_return_items_empty", "销售单没有可退货的商品明细")
            selected = self._select_return_items(source_items, items, "销售单")
            if return_date.strip():
                self._validate_order_date(return_date.strip())
            effective_date = (
                return_date.strip()
                or str(prefill.get("returnDate") or "").strip()
                or date.today().isoformat()
            )
            total_amount = self._return_total_amount(selected)
            refund, discount, refund_account, discount_account, ready = (
                await self._resolve_return_money(
                    total_amount,
                    refund_amount,
                    refund_account_id,
                    discount_amount,
                    discount_account_id,
                )
            )
            payload: dict[str, Any] = {
                "id": 0,
                "returnDate": effective_date,
                "sourceOrderId": str(prefill.get("sourceOrderId") or ""),
                "sourceOrderNo": str(prefill.get("sourceOrderNo") or ""),
                "customerId": str(prefill.get("customerId") or ""),
                "customerName": str(prefill.get("customerName") or ""),
                "warehouseId": str(prefill.get("warehouseId") or ""),
                "warehouseName": str(prefill.get("warehouseName") or ""),
                "handlerId": str(prefill.get("handlerId") or ""),
                "handlerName": str(prefill.get("handlerName") or ""),
                "saveType": 2,
                "remark": remark.strip(),
                "items": [
                    {
                        "productId": item["product_id"],
                        "quantity": item["quantity"],
                        "unitPrice": item["unit_price"],
                        **({"unit": item["unit"]} if item["unit"] else {}),
                    }
                    for item in selected
                ],
            }
            for passthrough in ("refundedAmount", "unrefundedAmount", "refundStatus"):
                if prefill.get(passthrough) is not None:
                    payload[passthrough] = prefill.get(passthrough)
            if refund is not None and refund > 0 and refund_account is not None:
                payload["paymentAmount"] = refund
                payload["paymentAccountId"] = str(refund_account["id"])
                payload["paymentAccountName"] = str(refund_account.get("name") or "")
            if discount is not None and discount > 0 and discount_account is not None:
                payload["discountAmount"] = discount
                payload["discountAccountId"] = str(discount_account["id"])
                payload["discountAccountName"] = str(discount_account.get("name") or "")
            preview = self._build_return_preview_summary(
                document_kind="sales_return",
                source_order_no=str(prefill.get("sourceOrderNo") or ""),
                counterparty_label="客户",
                counterparty_name=str(prefill.get("customerName") or ""),
                warehouse_name=str(prefill.get("warehouseName") or ""),
                handler_name=str(prefill.get("handlerName") or ""),
                doc_date=effective_date,
                items=selected,
                total_amount=total_amount,
                refund_label="退款金额",
                refund_amount=refund,
                refund_account=refund_account,
                discount_amount=discount,
                discount_account=discount_account,
                remark=remark.strip(),
            )
            required_actions = ["confirm_submit"] if ready else []
            preview_id = None
            if ready:
                preview_id = self.session.store_prepared_document("sales_return", payload, preview)
            return self.ok_response(
                source_order={
                    "order_id": str(prefill.get("sourceOrderId") or ""),
                    "order_no": str(prefill.get("sourceOrderNo") or ""),
                    "customer": str(prefill.get("customerName") or ""),
                    "warehouse": str(prefill.get("warehouseName") or ""),
                    "unrefunded_amount": prefill.get("unrefundedAmount"),
                },
                items=[
                    {
                        "product_id": item["product_id"],
                        "product_name": item["product_name"],
                        "quantity": item["quantity"],
                        "unit": item["unit"],
                        "unit_price": item["unit_price"],
                    }
                    for item in selected
                ],
                required_actions=required_actions,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_sales_return(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """用户明确确认预览后，把销售退货单写入真实 ERP（保存过账）。

        Args:
            preview_id: preview_sales_return 返回的预览 ID；提交成功后失效。
            idempotency_key: 可省略，默认使用 preview_id；显式指定时重试必须复用。
            confirmed_by_user: 仅在用户明确确认该预览后传 true。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            result = await self._api.create_sales_return(context, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="sales_return",
            doc_label="销售退货单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def get_sales_return(self, return_id: str) -> dict[str, Any]:
        """查询销售退货单详情。

        Args:
            return_id: 销售退货单标识，同时接受内部 ID 和业务单号 returnNo。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if not return_id.strip():
                raise DomainError("erp_sales_return_id_invalid", "销售退货单 ID 不能为空")
            result = await self._api.get_sales_return_detail(context, return_id.strip())
            return self.ok_response(document=result.document)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_sales_returns(
        self,
        page: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        refund_status: int | None = None,
        return_no: str = "",
        customer_id: str = "",
    ) -> dict[str, Any]:
        """分页查询销售退货单列表。

        Args:
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            status: 单据状态：0=草稿 2=已生效 3=已作废。
            refund_status: 退款状态：0=未退款 1=部分退款 2=已完成。
            return_no: 退货单编号模糊匹配关键词。
            customer_id: 客户 ID 或名称。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            self._validate_date_range(start_date.strip(), end_date.strip())
            customer = await self._optional_reference_id("customer", customer_id)
            result = await self._api.search_sales_returns(
                context,
                page_num=max(1, int(page or 1)),
                page_size=max(1, min(int(page_size or 20), 100)),
                start_date=start_date.strip(),
                end_date=end_date.strip(),
                status=status,
                refund_status=refund_status,
                return_no=return_no.strip(),
                customer_id=customer,
            )
            return self._document_page_response(result)
        except DomainError as exc:
            return self.error_response(exc)

    async def void_sales_return(
        self,
        return_id: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """作废销售退货单；只有用户明确确认后才能执行。

        Args:
            return_id: 销售退货单标识，同时接受内部 ID 和业务单号 returnNo。
            confirmed_by_user: 仅在用户明确确认作废后传 true。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            document_no = await self._void_document_flow(
                "sales_return",
                "销售退货单",
                return_id,
                confirmed_by_user,
                lambda resolved: self._api.void_sales_return(context, resolved),
            )
            return self.ok_response(voided=True, document_no=document_no)
        except DomainError as exc:
            return self.error_response(exc)

    # ------------------------------------------------------------------
    # 销售单继续收款 / 采购单继续付款
    # ------------------------------------------------------------------

    async def preview_sales_receipt(
        self,
        order_id: str,
        receipt_amount: float,
        receipt_account: str,
        discount_amount: float | None = None,
        discount_account: str = "",
    ) -> dict[str, Any]:
        """生成销售单继续收款预览，展示订单应收上下文与收款金额。

        Args:
            order_id: 销售单标识，同时接受内部 ID 和业务单号 orderNo。
            receipt_amount: 本次收款金额；与免账金额不能同时为 0。
            receipt_account: 收款结算账户名称、编号或候选返回的 ID。
            discount_amount: 免账金额（优惠/折让），可选。
            discount_account: 免账承担账户；免账金额大于 0 时必填。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            token = order_id.strip()
            if not token:
                raise DomainError("erp_sales_order_id_invalid", "销售单 ID 不能为空")
            amount = non_negative_amount(receipt_amount, "收款金额")
            discount = (
                non_negative_amount(discount_amount, "免账金额")
                if discount_amount is not None
                else 0.0
            )
            if amount + discount <= 0:
                raise DomainError("erp_sales_receipt_amount_invalid", "收款金额与免账金额不能同时为 0")
            detail = (await self._api.get_sales_order_detail(context, token)).order
            resolved_id = str(detail.get("id") or token)
            order_no = str(detail.get("orderNo") or "")
            unreceived = detail.get("unreceivedAmount")
            if isinstance(unreceived, (int, float)) and amount + discount > unreceived + 0.005:
                raise DomainError(
                    "erp_sales_receipt_amount_invalid",
                    "收款金额加免账金额（%.2f）不能超过未收金额 %.2f"
                    % (amount + discount, unreceived),
                )
            account_resolution = await self._resolve_reference(
                "settlement_account", receipt_account.strip(),
            )
            discount_resolution = (
                await self._resolve_reference("settlement_account", discount_account.strip())
                if discount > 0
                else None
            )
            ready = bool(
                account_resolution["status"] == "matched"
                and (discount_resolution is None or discount_resolution["status"] == "matched")
            )
            payload: dict[str, Any] | None = None
            preview: dict[str, Any] | None = None
            required_actions: list[str] = []
            if ready:
                selected_account = account_resolution["selected"]
                payload = {
                    "orderId": resolved_id,
                    "receiptAmount": amount,
                    "receiptAccountId": str(selected_account["id"]),
                }
                preview = {
                    "document_kind": "sales_receipt",
                    "order_no": order_no or resolved_id,
                    "customer": str(detail.get("customerName") or ""),
                    "total_amount": detail.get("totalAmount"),
                    "received_amount": detail.get("receivedAmount"),
                    "unreceived_amount": unreceived,
                    "receipt_amount": amount,
                    "receipt_account": str(selected_account.get("name") or ""),
                    "discount_amount": discount or None,
                }
                if discount > 0 and discount_resolution is not None:
                    payload["discountAmount"] = discount
                    payload["discountAccountId"] = str(discount_resolution["selected"]["id"])
                    preview["discount_account"] = str(
                        discount_resolution["selected"].get("name") or "",
                    )
                required_actions = ["confirm_submit"]
            else:
                required_actions = ["select_receipt_account"]
                if discount_resolution is not None and discount_resolution["status"] != "matched":
                    required_actions.append("select_discount_account")
            preview_id = None
            if ready and payload is not None and preview is not None:
                preview_id = self.session.store_prepared_document("sales_receipt", payload, preview)
            references: dict[str, dict[str, Any]] = {"receipt_account": account_resolution}
            if discount_resolution is not None:
                references["discount_account"] = discount_resolution
            return self.ok_response(
                order={
                    "order_id": resolved_id,
                    "order_no": order_no,
                    "customer": str(detail.get("customerName") or ""),
                    "total_amount": detail.get("totalAmount"),
                    "received_amount": detail.get("receivedAmount"),
                    "unreceived_amount": unreceived,
                },
                reference_resolutions=self._public_reference_resolutions(references),
                required_actions=required_actions,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_sales_receipt(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """用户明确确认预览后，把收款写入销售单（继续收款）。

        Args:
            preview_id: preview_sales_receipt 返回的预览 ID；提交成功后失效。
            idempotency_key: 可省略，默认使用 preview_id；显式指定时重试必须复用。
            confirmed_by_user: 仅在用户明确确认该预览后传 true。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            order_id = str(payload["orderId"])
            body = {key: value for key, value in payload.items() if key != "orderId"}
            await self._api.receive_sales_order(context, order_id, body)
            return order_id

        return await self._submit_prepared_document(
            kind="sales_receipt",
            doc_label="销售单收款",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def preview_purchase_payment(
        self,
        order_id: str,
        payment_amount: float,
        payment_account: str,
        discount_amount: float | None = None,
        discount_account: str = "",
    ) -> dict[str, Any]:
        """生成采购单继续付款预览，展示订单应付上下文与付款金额。

        Args:
            order_id: 采购单标识，同时接受内部 ID 和业务单号 orderNo。
            payment_amount: 本次付款金额；与免账金额不能同时为 0。
            payment_account: 付款结算账户名称、编号或候选返回的 ID。
            discount_amount: 免账金额（优惠/折让），可选。
            discount_account: 免账承担账户；免账金额大于 0 时必填。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            token = order_id.strip()
            if not token:
                raise DomainError("erp_purchase_order_id_invalid", "采购单 ID 不能为空")
            amount = non_negative_amount(payment_amount, "付款金额")
            discount = (
                non_negative_amount(discount_amount, "免账金额")
                if discount_amount is not None
                else 0.0
            )
            if amount + discount <= 0:
                raise DomainError("erp_purchase_payment_amount_invalid", "付款金额与免账金额不能同时为 0")
            detail = (await self._api.get_purchase_order_detail(context, token)).document
            resolved_id = str(detail.get("id") or token)
            order_no = str(detail.get("orderNo") or "")
            unpaid = detail.get("unpaidAmount")
            if isinstance(unpaid, (int, float)) and amount + discount > unpaid + 0.005:
                raise DomainError(
                    "erp_purchase_payment_amount_invalid",
                    "付款金额加免账金额（%.2f）不能超过未付金额 %.2f"
                    % (amount + discount, unpaid),
                )
            account_resolution = await self._resolve_reference(
                "settlement_account", payment_account.strip(),
            )
            discount_resolution = (
                await self._resolve_reference("settlement_account", discount_account.strip())
                if discount > 0
                else None
            )
            ready = bool(
                account_resolution["status"] == "matched"
                and (discount_resolution is None or discount_resolution["status"] == "matched")
            )
            payload: dict[str, Any] | None = None
            preview: dict[str, Any] | None = None
            required_actions: list[str] = []
            if ready:
                selected_account = account_resolution["selected"]
                payload = {
                    "orderId": resolved_id,
                    "paymentAmount": amount,
                    "paymentAccountId": str(selected_account["id"]),
                }
                preview = {
                    "document_kind": "purchase_payment",
                    "order_no": order_no or resolved_id,
                    "supplier": str(detail.get("supplierName") or ""),
                    "total_amount": detail.get("totalAmount"),
                    "paid_amount": detail.get("paidAmount"),
                    "unpaid_amount": unpaid,
                    "payment_amount": amount,
                    "payment_account": str(selected_account.get("name") or ""),
                    "discount_amount": discount or None,
                }
                if discount > 0 and discount_resolution is not None:
                    payload["discountAmount"] = discount
                    payload["discountAccountId"] = str(discount_resolution["selected"]["id"])
                    preview["discount_account"] = str(
                        discount_resolution["selected"].get("name") or "",
                    )
                required_actions = ["confirm_submit"]
            else:
                required_actions = ["select_payment_account"]
                if discount_resolution is not None and discount_resolution["status"] != "matched":
                    required_actions.append("select_discount_account")
            preview_id = None
            if ready and payload is not None and preview is not None:
                preview_id = self.session.store_prepared_document("purchase_payment", payload, preview)
            references: dict[str, dict[str, Any]] = {"payment_account": account_resolution}
            if discount_resolution is not None:
                references["discount_account"] = discount_resolution
            return self.ok_response(
                order={
                    "order_id": resolved_id,
                    "order_no": order_no,
                    "supplier": str(detail.get("supplierName") or ""),
                    "total_amount": detail.get("totalAmount"),
                    "paid_amount": detail.get("paidAmount"),
                    "unpaid_amount": unpaid,
                },
                reference_resolutions=self._public_reference_resolutions(references),
                required_actions=required_actions,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_purchase_payment(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """用户明确确认预览后，把付款写入采购单（继续付款）。

        Args:
            preview_id: preview_purchase_payment 返回的预览 ID；提交成功后失效。
            idempotency_key: 可省略，默认使用 preview_id；显式指定时重试必须复用。
            confirmed_by_user: 仅在用户明确确认该预览后传 true。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            order_id = str(payload["orderId"])
            body = {key: value for key, value in payload.items() if key != "orderId"}
            await self._api.pay_purchase_order(context, order_id, body)
            return order_id

        return await self._submit_prepared_document(
            kind="purchase_payment",
            doc_label="采购单付款",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    # ------------------------------------------------------------------
    # 库存调拨与其他出入库
    # ------------------------------------------------------------------

    async def preview_stock_transfer(
        self,
        order_text: str,
        from_warehouse: str,
        to_warehouse: str,
        handler: str,
        transfer_date: str = "",
        remark: str = "",
        confirmed_products: list[dict[str, str]] | None = None,
        confirmed_units: list[dict[str, Any]] | None = None,
        partial: bool = False,
    ) -> dict[str, Any]:
        """校验调拨单信息、匹配商品并生成不可变提交预览。

        Args:
            order_text: 调拨商品文本，必须包含商品和数量。
            from_warehouse: 必填，调出仓库名称、编号或候选返回的 ID。
            to_warehouse: 必填，调入仓库名称、编号或候选返回的 ID。
            handler: 必填，经手人名称、编号或候选返回的 ID。
            transfer_date: 调拨日期 YYYY-MM-DD；默认当天。
            remark: 可选备注，最多 200 个字符。
            confirmed_products: 用户确认的商品列表，元素格式为
                {"line_id": "L001", "product_id": "ERP商品ID"}。
            confirmed_units: 用户按 ERP 单位确认后的行数据。
            partial: 为 true 时只提交已匹配商品，跳过未匹配行。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            values = {
                "from_warehouse": from_warehouse.strip(),
                "to_warehouse": to_warehouse.strip(),
                "handler": handler.strip(),
                "order_text": order_text.strip(),
            }
            effective_date = transfer_date.strip() or date.today().isoformat()
            self._validate_order_date(effective_date)
            if len(remark.strip()) > 200:
                raise DomainError("erp_stock_transfer_remark_too_long", "备注最多 200 个字符")
            missing = self._missing_fields(
                values,
                (
                    ("from_warehouse", "调出仓库", "请问从哪个仓库调出？"),
                    ("to_warehouse", "调入仓库", "请问调到哪个仓库？"),
                    ("handler", "经手人", "请问本单经手人是谁？"),
                    ("order_text", "商品明细", "请提供商品、数量和单位。"),
                ),
            )

            draft = await self._match_order_products(values["order_text"], "text", confirmed_products)
            self._apply_confirmed_units(draft, confirmed_units)
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
                "from_warehouse": await self._resolve_reference("warehouse", values["from_warehouse"]),
                "to_warehouse": await self._resolve_reference("warehouse", values["to_warehouse"]),
                "handler": await self._resolve_reference("handler", values["handler"]),
            }
            references_ready = all(
                resolution["status"] == "matched"
                for resolution in reference_resolutions.values()
            )
            if (
                references_ready
                and reference_resolutions["from_warehouse"]["selected"]["id"]
                == reference_resolutions["to_warehouse"]["selected"]["id"]
            ):
                raise DomainError(
                    "erp_stock_transfer_warehouse_invalid",
                    "调出仓库不能与调入仓库相同",
                )
            unit_warnings = self._unit_warnings(draft)
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
                and (draft.status == "ready" or (partial and has_matched))
            )

            payload: dict[str, Any] | None = None
            preview: dict[str, Any] | None = None
            if ready and draft is not None:
                items, preview_items, total = self._build_stock_items(draft, partial)
                payload = {
                    "id": 0,
                    "transferDate": effective_date,
                    "fromWarehouseId": reference_resolutions["from_warehouse"]["selected"]["id"],
                    "toWarehouseId": reference_resolutions["to_warehouse"]["selected"]["id"],
                    "handlerId": reference_resolutions["handler"]["selected"]["id"],
                    "saveType": 2,
                    "remark": remark.strip(),
                    "items": items,
                }
                preview = {
                    "document_kind": "stock_transfer",
                    "transfer_date": effective_date,
                    "from_warehouse": str(reference_resolutions["from_warehouse"]["selected"].get("name") or ""),
                    "to_warehouse": str(reference_resolutions["to_warehouse"]["selected"].get("name") or ""),
                    "handler": str(reference_resolutions["handler"]["selected"].get("name") or ""),
                    "remark": remark.strip(),
                    "items": preview_items,
                    "total_quantity": total,
                }
            return self._finalize_document_preview(
                kind="stock_transfer",
                missing=missing,
                reference_resolutions=reference_resolutions,
                unit_warnings=unit_warnings,
                price_warnings=[],
                product_payload=product_payload,
                ready=ready,
                payload=payload,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_stock_transfer(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """用户明确确认预览后，把调拨单写入真实 ERP（保存过账）。

        Args:
            preview_id: preview_stock_transfer 返回的预览 ID；提交成功后失效。
            idempotency_key: 可省略，默认使用 preview_id；显式指定时重试必须复用。
            confirmed_by_user: 仅在用户明确确认该预览后传 true。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            result = await self._api.create_stock_transfer(context, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="stock_transfer",
            doc_label="调拨单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def preview_other_stock_doc(
        self,
        kind: str,
        order_text: str,
        warehouse: str,
        handler: str,
        doc_type: str,
        doc_date: str = "",
        remark: str = "",
        confirmed_products: list[dict[str, str]] | None = None,
        confirmed_units: list[dict[str, Any]] | None = None,
        partial: bool = False,
    ) -> dict[str, Any]:
        """校验其他入库/出库单（报损、报溢等）并生成不可变提交预览。

        Args:
            kind: 单据方向：inbound=其他入库，outbound=其他出库。
            order_text: 商品文本，必须包含商品和数量。
            warehouse: 必填，仓库名称、编号或候选返回的 ID。
            handler: 必填，经手人名称、编号或候选返回的 ID。
            doc_type: 必填，入库/出库类型 ID 或名称（先用
                list_stock_doc_types 查询可用类型，如报损、报溢）。
            doc_date: 单据日期 YYYY-MM-DD；默认当天。
            remark: 可选备注，最多 200 个字符。
            confirmed_products: 用户确认的商品列表。
            confirmed_units: 用户按 ERP 单位确认后的行数据。
            partial: 为 true 时只提交已匹配商品，跳过未匹配行。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            normalized_kind = kind.strip()
            if normalized_kind not in {"inbound", "outbound"}:
                raise DomainError("erp_stock_doc_type_invalid", "kind 必须是 inbound 或 outbound")
            doc_label = "其他入库单" if normalized_kind == "inbound" else "其他出库单"
            values = {
                "warehouse": warehouse.strip(),
                "handler": handler.strip(),
                "order_text": order_text.strip(),
            }
            effective_date = doc_date.strip() or date.today().isoformat()
            self._validate_order_date(effective_date)
            if len(remark.strip()) > 200:
                raise DomainError("erp_stock_doc_remark_too_long", "备注最多 200 个字符")
            missing = self._missing_fields(
                values,
                (
                    ("warehouse", "仓库", "请问在哪个仓库%s？" % ("入库" if normalized_kind == "inbound" else "出库")),
                    ("handler", "经手人", "请问本单经手人是谁？"),
                    ("order_text", "商品明细", "请提供商品、数量和单位。"),
                ),
            )

            type_resolution = await self._resolve_stock_doc_type(
                context, normalized_kind, doc_type,
            )
            draft = await self._match_order_products(values["order_text"], "text", confirmed_products)
            self._apply_confirmed_units(draft, confirmed_units)
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
                "warehouse": await self._resolve_reference("warehouse", values["warehouse"]),
                "handler": await self._resolve_reference("handler", values["handler"]),
                "doc_type": type_resolution,
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
                and (draft.status == "ready" or (partial and has_matched))
            )

            payload: dict[str, Any] | None = None
            preview: dict[str, Any] | None = None
            if ready and draft is not None:
                items, preview_items, total = self._build_stock_items(draft, partial)
                payload = {
                    "id": 0,
                    "%sDate" % ("inbound" if normalized_kind == "inbound" else "outbound"): effective_date,
                    "warehouseId": reference_resolutions["warehouse"]["selected"]["id"],
                    "%sType" % ("inbound" if normalized_kind == "inbound" else "outbound"): int(
                        reference_resolutions["doc_type"]["selected"]["id"],
                    ),
                    "handlerId": reference_resolutions["handler"]["selected"]["id"],
                    "saveType": 2,
                    "remark": remark.strip(),
                    "items": items,
                }
                preview = {
                    "document_kind": "other_stock_doc",
                    "kind": normalized_kind,
                    "doc_date": effective_date,
                    "doc_type": str(reference_resolutions["doc_type"]["selected"].get("name") or ""),
                    "warehouse": str(reference_resolutions["warehouse"]["selected"].get("name") or ""),
                    "handler": str(reference_resolutions["handler"]["selected"].get("name") or ""),
                    "remark": remark.strip(),
                    "items": preview_items,
                    "total_quantity": total,
                }
            result = self._finalize_document_preview(
                kind="other_stock_doc",
                missing=missing,
                reference_resolutions=reference_resolutions,
                unit_warnings=unit_warnings,
                price_warnings=[],
                product_payload=product_payload,
                ready=ready,
                payload=payload,
                preview=preview,
            )
            result["doc_label"] = doc_label
            return result
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_other_stock_doc(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """用户明确确认预览后，把其他入库/出库单写入真实 ERP（保存过账）。

        Args:
            preview_id: preview_other_stock_doc 返回的预览 ID；提交成功后失效。
            idempotency_key: 可省略，默认使用 preview_id；显式指定时重试必须复用。
            confirmed_by_user: 仅在用户明确确认该预览后传 true。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            kind = "inbound" if "inboundType" in payload else "outbound"
            result = await self._api.create_other_stock_doc(context, kind, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="other_stock_doc",
            doc_label="其他出入库单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    # ------------------------------------------------------------------
    # 收款单与付款单
    # ------------------------------------------------------------------

    async def preview_receipt_order(
        self,
        receipt_amount: float,
        account: str,
        handler: str,
        customer: str = "",
        supplier: str = "",
        order_date: str = "",
        discount_amount: float | None = None,
        discount_account: str = "",
        fund_type: str = "",
        writeoff_details: list[dict[str, Any]] | None = None,
        remark: str = "",
    ) -> dict[str, Any]:
        """生成收款单预览；不关联销售单的独立收款（含核销）用此工具。

        销售单上的继续收款请用 preview_sales_receipt。

        Args:
            receipt_amount: 收款总额，必须大于 0。
            account: 必填，收款结算账户名称、编号或候选返回的 ID。
            handler: 必填，经手人名称、编号或候选返回的 ID。
            customer: 客户名称或 ID；核销销售单/销售退货单时必填。
            supplier: 供应商名称或 ID；核销采购退货单时使用。
            order_date: 单据日期 YYYY-MM-DD；默认当天。
            discount_amount: 优惠（抹零）金额，可选。
            discount_account: 免账承担账户；优惠金额大于 0 时必填。
            fund_type: 款项类型名称、编号或 ID；带核销时默认按场景选
                系统类型（核销销售单=销售收款，核销采购退货单=采购退款
                收款），无核销时必填，缺省时返回候选列表。
            writeoff_details: 核销明细，每行含 biz_type（sales_order、
                purchase_order、sales_return、purchase_return）、biz_id、
                writeoff_amount；核销总额不能超过收款金额。
            remark: 备注。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            amount = non_negative_amount(receipt_amount, "收款金额")
            if amount <= 0:
                raise DomainError("erp_receipt_order_amount_invalid", "收款金额必须大于 0")
            payload, preview, references, ready, required_actions = (
                await self._build_financial_order_preview(
                    context=context,
                    direction="receipt",
                    amount=amount,
                    account=account,
                    handler=handler,
                    customer=customer,
                    supplier=supplier,
                    order_date=order_date,
                    discount_amount=discount_amount,
                    discount_account=discount_account,
                    fund_type=fund_type,
                    writeoff_details=writeoff_details,
                    remark=remark,
                )
            )
            preview_id = None
            if ready:
                preview_id = self.session.store_prepared_document("receipt_order", payload, preview)
            return self.ok_response(
                reference_resolutions=self._public_reference_resolutions(references),
                required_actions=required_actions,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_receipt_order(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """用户明确确认预览后，把收款单写入真实 ERP（保存生效）。

        Args:
            preview_id: preview_receipt_order 返回的预览 ID；提交成功后失效。
            idempotency_key: 可省略，默认使用 preview_id；显式指定时重试必须复用。
            confirmed_by_user: 仅在用户明确确认该预览后传 true。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            result = await self._api.create_receipt_order(context, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="receipt_order",
            doc_label="收款单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def get_receipt_order(self, order_id: str) -> dict[str, Any]:
        """查询收款单详情，含核销明细。

        Args:
            order_id: 收款单标识，同时接受内部 ID 和业务单号 orderNo。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if not order_id.strip():
                raise DomainError("erp_financial_order_id_invalid", "收款单 ID 不能为空")
            result = await self._api.get_financial_order_detail(context, "receipt", order_id.strip())
            return self.ok_response(document=result.document)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_receipt_orders(
        self,
        page: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        order_no: str = "",
        counterparty_id: str = "",
        account_id: str = "",
    ) -> dict[str, Any]:
        """分页查询收款单列表。

        Args:
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            status: 单据状态：0=草稿 2=已生效 3=已作废。
            order_no: 单据编号模糊匹配关键词。
            counterparty_id: 客户或供应商 ID、名称。
            account_id: 结算账户 ID 或名称。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            params = await self._financial_order_params(
                context, start_date, end_date, status, order_no, counterparty_id, account_id,
                customer_field="customerId", supplier_field="supplierId",
            )
            result = await self._api.list_financial_orders(
                context, "receipt",
                self._with_page(params, page, page_size),
            )
            return self._document_page_response(result)
        except DomainError as exc:
            return self.error_response(exc)

    async def void_receipt_order(
        self,
        order_id: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """作废收款单；只有用户明确确认后才能执行。

        Args:
            order_id: 收款单标识，同时接受内部 ID 和业务单号 orderNo。
            confirmed_by_user: 仅在用户明确确认作废后传 true。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            document_no = await self._void_financial_flow(
                "receipt", "收款单", order_id, confirmed_by_user,
            )
            return self.ok_response(voided=True, document_no=document_no)
        except DomainError as exc:
            return self.error_response(exc)

    async def preview_payment_order(
        self,
        payment_amount: float,
        account: str,
        handler: str,
        supplier: str = "",
        customer: str = "",
        order_date: str = "",
        discount_amount: float | None = None,
        discount_account: str = "",
        fund_type: str = "",
        writeoff_details: list[dict[str, Any]] | None = None,
        remark: str = "",
    ) -> dict[str, Any]:
        """生成付款单预览；不关联采购单的独立付款（含核销）用此工具。

        采购单上的继续付款请用 preview_purchase_payment。

        Args:
            payment_amount: 付款总额，必须大于 0。
            account: 必填，付款结算账户名称、编号或候选返回的 ID。
            handler: 必填，经手人名称、编号或候选返回的 ID。
            supplier: 供应商名称或 ID；核销采购单/采购退货单时必填。
            customer: 客户名称或 ID；核销销售退货单时使用。
            order_date: 单据日期 YYYY-MM-DD；默认当天。
            discount_amount: 优惠（抹零）金额，可选。
            discount_account: 免账承担账户；优惠金额大于 0 时必填。
            fund_type: 款项类型名称、编号或 ID；带核销时默认按场景选
                系统类型（核销采购单=采购付款，核销销售退货单=销售退款
                付款），无核销时必填，缺省时返回候选列表。
            writeoff_details: 核销明细，每行含 biz_type、biz_id、writeoff_amount；
                核销总额不能超过付款金额。
            remark: 备注。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            amount = non_negative_amount(payment_amount, "付款金额")
            if amount <= 0:
                raise DomainError("erp_payment_order_amount_invalid", "付款金额必须大于 0")
            payload, preview, references, ready, required_actions = (
                await self._build_financial_order_preview(
                    context=context,
                    direction="payment",
                    amount=amount,
                    account=account,
                    handler=handler,
                    customer=customer,
                    supplier=supplier,
                    order_date=order_date,
                    discount_amount=discount_amount,
                    discount_account=discount_account,
                    fund_type=fund_type,
                    writeoff_details=writeoff_details,
                    remark=remark,
                )
            )
            preview_id = None
            if ready:
                preview_id = self.session.store_prepared_document("payment_order", payload, preview)
            return self.ok_response(
                reference_resolutions=self._public_reference_resolutions(references),
                required_actions=required_actions,
                ready_to_submit=ready,
                preview_id=preview_id,
                preview=preview,
            )
        except DomainError as exc:
            return self.error_response(exc)

    async def submit_payment_order(
        self,
        preview_id: str,
        idempotency_key: str | None = None,
        confirmed_by_user: bool = False,
    ) -> dict[str, Any]:
        """用户明确确认预览后，把付款单写入真实 ERP（保存生效）。

        Args:
            preview_id: preview_payment_order 返回的预览 ID；提交成功后失效。
            idempotency_key: 可省略，默认使用 preview_id；显式指定时重试必须复用。
            confirmed_by_user: 仅在用户明确确认该预览后传 true。
        """

        async def create(context: Any, payload: dict[str, Any]) -> str:
            result = await self._api.create_payment_order(context, payload)
            return result.document_id

        return await self._submit_prepared_document(
            kind="payment_order",
            doc_label="付款单",
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            confirmed_by_user=confirmed_by_user,
            create=create,
        )

    async def get_payment_order(self, order_id: str) -> dict[str, Any]:
        """查询付款单详情，含核销明细。

        Args:
            order_id: 付款单标识，同时接受内部 ID 和业务单号 orderNo。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            if not order_id.strip():
                raise DomainError("erp_financial_order_id_invalid", "付款单 ID 不能为空")
            result = await self._api.get_financial_order_detail(context, "payment", order_id.strip())
            return self.ok_response(document=result.document)
        except DomainError as exc:
            return self.error_response(exc)

    async def list_payment_orders(
        self,
        page: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        status: int | None = None,
        order_no: str = "",
        counterparty_id: str = "",
        account_id: str = "",
    ) -> dict[str, Any]:
        """分页查询付款单列表。

        Args:
            page: 页码，从 1 开始。
            page_size: 每页数量，范围 1 到 100。
            start_date: 开始日期，格式 YYYY-MM-DD。
            end_date: 结束日期，格式 YYYY-MM-DD。
            status: 单据状态：0=草稿 2=已生效 3=已作废。
            order_no: 单据编号模糊匹配关键词。
            counterparty_id: 供应商或客户 ID、名称。
            account_id: 结算账户 ID 或名称。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:read")
            params = await self._financial_order_params(
                context, start_date, end_date, status, order_no, counterparty_id, account_id,
                customer_field="customerId", supplier_field="supplierId",
            )
            result = await self._api.list_financial_orders(
                context, "payment",
                self._with_page(params, page, page_size),
            )
            return self._document_page_response(result)
        except DomainError as exc:
            return self.error_response(exc)

    async def void_payment_order(
        self,
        order_id: str,
        confirmed_by_user: bool,
    ) -> dict[str, Any]:
        """作废付款单；只有用户明确确认后才能执行。

        Args:
            order_id: 付款单标识，同时接受内部 ID 和业务单号 orderNo。
            confirmed_by_user: 仅在用户明确确认作废后传 true。
        """
        try:
            context = self._contexts.get()
            context.require_scope("billing:write")
            document_no = await self._void_financial_flow(
                "payment", "付款单", order_id, confirmed_by_user,
            )
            return self.ok_response(voided=True, document_no=document_no)
        except DomainError as exc:
            return self.error_response(exc)

    # ------------------------------------------------------------------
    # 内部辅助：预览装配
    # ------------------------------------------------------------------

    @staticmethod
    def _missing_fields(
        values: dict[str, str],
        required: tuple[tuple[str, str, str], ...],
    ) -> list[dict[str, str]]:
        return [
            {"field": field, "label": label, "prompt": prompt}
            for field, label, prompt in required
            if not values.get(field)
        ]

    def _finalize_document_preview(
        self,
        *,
        kind: str,
        missing: list[dict[str, str]],
        reference_resolutions: dict[str, dict[str, Any]],
        unit_warnings: list[dict[str, Any]],
        price_warnings: list[dict[str, Any]],
        product_payload: dict[str, Any],
        ready: bool,
        payload: dict[str, Any] | None,
        preview: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """收敛各类预览校验结果：存储不可变预览并输出待办。"""
        preview_id = None
        if ready and payload is not None and preview is not None:
            preview_id = self.session.store_prepared_document(kind, payload, preview)
        required_actions = ["confirm_submit"] if ready else []
        if not ready:
            required_actions.extend(
                "provide_%s" % item["field"] for item in missing
            )
            for field, resolution in reference_resolutions.items():
                if resolution["status"] == "ambiguous":
                    required_actions.append("select_%s" % field)
                elif resolution["status"] == "unmatched":
                    required_actions.append("replace_%s" % field)
            if product_payload.get("recommended_products"):
                required_actions.append("select_products")
            if product_payload.get("unmatched_products"):
                required_actions.append("resolve_unmatched_products")
            if unit_warnings:
                required_actions.append("confirm_units")
            if price_warnings:
                required_actions.append("confirm_prices")
        result = self.ok_response(
            missing_required_fields=missing,
            reference_resolutions=self._public_reference_resolutions(reference_resolutions),
            unit_warnings=unit_warnings,
            price_warnings=price_warnings,
            required_actions=required_actions,
            **product_payload,
            ready_to_submit=ready,
            preview_id=preview_id,
            preview=preview,
        )
        return result

    def _public_reference_resolutions(
        self,
        resolutions: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """候选保留机器可用 ID；用户界面由 Agent 隐藏内部标识。"""
        return {
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
            for field, resolution in resolutions.items()
        }

    def _build_purchase_order_preview(
        self,
        *,
        draft: Any,
        references: dict[str, dict[str, Any]],
        order_date: str,
        remark: str,
        prices: dict[str, float],
        partial: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """构建采购单提交 payload 与用户预览摘要。"""
        supplier = references["supplier"]["selected"]
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
                    "erp_purchase_order_product_unconfirmed",
                    "采购单仍有未确认商品，不能生成提交预览",
                )
            unit_price = prices.get(line.order_line.line_id, line.product.purchase_price)
            if unit_price is None or unit_price <= 0:
                raise DomainError(
                    "erp_purchase_order_price_missing",
                    "商品 %s 缺少有效采购价，请通过 confirmed_prices 确认单价" % line.product.name,
                )
            item: dict[str, Any] = {
                "productId": line.product.product_id,
                "quantity": line.order_line.quantity,
                "unitPrice": positive_amount(unit_price, "采购单价"),
            }
            if line.product.unit:
                item["unit"] = line.product.unit
            if line.order_line.note:
                item["remark"] = line.order_line.note
            items.append(item)
            amount = line_amount(line.order_line.quantity, float(unit_price))
            preview_item = {
                "name": line.product.name,
                "quantity": line.order_line.quantity,
                "unit": line.product.unit or line.order_line.unit,
                "unit_price": float(unit_price),
            }
            if amount is None:
                has_complete_amount = False
            else:
                preview_item["line_amount"] = float(amount)
                total_amount += amount
            preview_items.append(preview_item)
        payload = {
            "id": 0,
            "orderDate": order_date,
            "supplierId": supplier["id"],
            "warehouseId": warehouse["id"],
            "handlerId": handler["id"],
            "saveType": 2,
            "remark": remark,
            "items": items,
        }
        preview = {
            "document_kind": "purchase_order",
            "order_date": order_date,
            "supplier": str(supplier.get("name") or ""),
            "warehouse": str(warehouse.get("name") or ""),
            "handler": str(handler.get("name") or ""),
            "remark": remark,
            "items": preview_items,
        }
        if preview_items and has_complete_amount:
            preview["total_amount"] = float(total_amount)
        return payload, preview

    def _purchase_price_warnings(
        self,
        draft: Any,
        prices: dict[str, float],
    ) -> list[dict[str, Any]]:
        """目录无有效采购价（缺价或 0 价）且未经确认的行，要求补充单价。

        ERP 拒绝 unitPrice 为 0 的采购行（上游错误 50303），测试目录中
        商品最近采购价常为 0，必须先经 confirmed_prices 确认才能提交。
        """
        warnings: list[dict[str, Any]] = []
        if draft is None:
            return warnings
        for line in draft.lines:
            if line.status != "matched" or line.product is None:
                continue
            if prices.get(line.order_line.line_id) is not None:
                continue
            catalog_price = line.product.purchase_price
            if catalog_price is None or catalog_price <= 0:
                warnings.append(
                    {
                        "line_id": line.order_line.line_id,
                        "product": line.product.name,
                        "product_id": line.product.product_id,
                        "prompt": "商品缺少有效采购价（目录价为空或 0），请通过 confirmed_prices 确认单价后重新预览。",
                    },
                )
        return warnings

    @staticmethod
    def _normalize_confirmed_prices(
        draft: Any,
        confirmed_prices: list[dict[str, Any]] | None,
    ) -> dict[str, float]:
        """按行确认采购价；行必须已匹配商品且价格大于 0。"""
        if confirmed_prices is None:
            return {}
        if not isinstance(confirmed_prices, list):
            raise DomainError("erp_price_confirmation_invalid", "confirmed_prices 必须是数组")
        lines = {line.order_line.line_id: line for line in draft.lines} if draft else {}
        result: dict[str, float] = {}
        seen: set[str] = set()
        for entry in confirmed_prices:
            if not isinstance(entry, dict):
                raise DomainError("erp_price_confirmation_invalid", "价格确认必须是对象")
            line_id = entry.get("line_id")
            if not isinstance(line_id, str) or line_id in seen or line_id not in lines:
                raise DomainError("erp_price_confirmation_invalid", "价格确认行不存在或重复")
            seen.add(line_id)
            line = lines[line_id]
            if (line.product is None or line.status != "matched"
                    or entry.get("product_id") != line.product.product_id):
                raise DomainError("erp_price_confirmation_invalid", "请对已匹配商品的行确认价格")
            result[line_id] = positive_amount(entry.get("unit_price"), "确认单价")
        return result

    def _build_stock_items(
        self,
        draft: Any,
        partial: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
        """构建调拨/其他出入库明细：无单价，数量合计供预览。"""
        items: list[dict[str, Any]] = []
        preview_items: list[dict[str, Any]] = []
        total_quantity = 0.0
        for line in draft.lines:
            if line.product is None:
                if partial:
                    continue
                raise DomainError(
                    "erp_stock_doc_product_unconfirmed",
                    "单据仍有未确认商品，不能生成提交预览",
                )
            item: dict[str, Any] = {
                "productId": line.product.product_id,
                "quantity": line.order_line.quantity,
            }
            if line.product.unit:
                item["unit"] = line.product.unit
            if line.order_line.note:
                item["remark"] = line.order_line.note
            items.append(item)
            preview_items.append(
                {
                    "name": line.product.name,
                    "quantity": line.order_line.quantity,
                    "unit": line.product.unit or line.order_line.unit,
                },
            )
            total_quantity += float(line.order_line.quantity)
        return items, preview_items, total_quantity

    async def _resolve_stock_doc_type(
        self,
        context: Any,
        kind: str,
        value: str,
    ) -> dict[str, Any]:
        """解析入库/出库类型：数字视为类型 ID，其余按名称匹配。"""
        token = (value or "").strip()
        if not token:
            return {
                "status": "missing",
                "query": "",
                "selected": None,
                "candidates": [],
            }
        if token.isdigit():
            return {
                "status": "matched",
                "query": token,
                "selected": {"id": token, "name": "类型ID %s" % token, "is_default": False},
                "candidates": [],
            }
        result = await self._api.list_stock_doc_types(context, kind)
        rows = result.data if isinstance(result.data, list) else []
        options = [row for row in rows if isinstance(row, dict)]
        normalized = normalize_name(token)
        exact = [
            option
            for option in options
            if normalized
            in {
                normalize_name(str(option.get("id") or "")),
                normalize_name(str(option.get("name") or "")),
            }
        ]
        if len(exact) == 1:
            return {"status": "matched", "query": token, "selected": exact[0], "candidates": []}
        contains = [
            option
            for option in options
            if normalized in normalize_name(str(option.get("name") or ""))
        ]
        candidates = (contains or options)[:5]
        return {
            "status": "ambiguous" if candidates else "unmatched",
            "query": token,
            "selected": None,
            "candidates": candidates,
        }

    # ------------------------------------------------------------------
    # 内部辅助：退货预览
    # ------------------------------------------------------------------

    @staticmethod
    def _quick_return_items(prefill: dict[str, Any]) -> list[dict[str, Any]]:
        """归一化快捷退货预填明细，保留原数量与单价。"""
        items: list[dict[str, Any]] = []
        for row in prefill.get("items") or []:
            if not isinstance(row, dict):
                continue
            product_id = str(row.get("productId") or "").strip()
            if not product_id:
                continue
            try:
                quantity = float(row.get("quantity") or 0)
                unit_price = float(row.get("unitPrice") or 0)
            except (TypeError, ValueError):
                continue
            items.append(
                {
                    "product_id": product_id,
                    "product_name": str(row.get("productName") or ""),
                    "unit": str(row.get("unit") or ""),
                    "quantity": quantity,
                    "unit_price": unit_price,
                },
            )
        return items

    def _select_return_items(
        self,
        source_items: list[dict[str, Any]],
        items: list[dict[str, Any]] | None,
        doc_label: str,
    ) -> list[dict[str, Any]]:
        """应用用户退货明细覆盖；不传默认整单退货。

        退货数量不能超过源单数量（快捷退货预填已扣除历史退货）。
        """
        if items is None:
            return [dict(item) for item in source_items]
        if not isinstance(items, list) or not items:
            raise DomainError("erp_return_items_invalid", "退货明细不能为空")
        by_product = {item["product_id"]: item for item in source_items}
        selected: list[dict[str, Any]] = []
        for index, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                raise DomainError("erp_return_items_invalid", "第%d行退货明细不是 JSON 对象" % index)
            product_id = str(raw.get("product_id") or "").strip()
            if not product_id:
                raise DomainError("erp_return_items_invalid", "第%d行退货明细缺少 product_id" % index)
            source = by_product.get(product_id)
            if source is None:
                available = "、".join(
                    item["product_name"] or item["product_id"] for item in source_items[:5]
                )
                raise DomainError(
                    "erp_return_items_invalid",
                    "商品 %s 不在%s明细中；可退商品：%s" % (product_id, doc_label, available),
                )
            quantity = non_negative_amount(raw.get("quantity"), "退货数量")
            if quantity < 0.0001:
                raise DomainError("erp_return_items_invalid", "退货数量必须大于 0")
            if quantity > source["quantity"] + 0.0001:
                raise DomainError(
                    "erp_return_items_invalid",
                    "商品 %s 退货数量 %.4f 超过可退数量 %.4f"
                    % (source["product_name"] or product_id, quantity, source["quantity"]),
                )
            unit_price = source["unit_price"]
            if raw.get("unit_price") is not None:
                unit_price = non_negative_amount(raw.get("unit_price"), "退货单价")
            selected.append(
                {
                    "product_id": product_id,
                    "product_name": source["product_name"],
                    "unit": source["unit"],
                    "quantity": quantity,
                    "unit_price": unit_price,
                },
            )
        return selected

    @staticmethod
    def _return_total_amount(items: list[dict[str, Any]]) -> Decimal:
        total = Decimal("0")
        for item in items:
            amount = line_amount(item["quantity"], item["unit_price"])
            if amount is not None:
                total += amount
        return total

    async def _resolve_return_money(
        self,
        total_amount: Decimal,
        refund_amount: float | None,
        refund_account_id: str,
        discount_amount: float | None,
        discount_account_id: str,
    ) -> tuple[
        float | None,
        float | None,
        dict[str, Any] | None,
        dict[str, Any] | None,
        bool,
    ]:
        """解析退货同时发生的退款与优惠，返回金额、账户与就绪状态。"""
        refund = (
            non_negative_amount(refund_amount, "退款金额")
            if refund_amount is not None
            else None
        )
        discount = (
            non_negative_amount(discount_amount, "优惠金额")
            if discount_amount is not None
            else None
        )
        if refund is not None and discount is not None and refund + discount > float(total_amount) + 0.005:
            raise DomainError(
                "erp_return_money_invalid",
                "退款金额加优惠金额不能超过退货单总额 %.2f" % float(total_amount),
            )
        refund_account: dict[str, Any] | None = None
        discount_account: dict[str, Any] | None = None
        ready = True
        if refund is not None and refund > 0:
            if not refund_account_id.strip():
                raise DomainError(
                    "erp_return_money_invalid",
                    "退款金额大于 0 时必须提供退款账户",
                )
            resolution = await self._resolve_reference(
                "settlement_account", refund_account_id.strip(),
            )
            if resolution["status"] != "matched":
                ready = False
            else:
                refund_account = resolution["selected"]
        if discount is not None and discount > 0:
            if not discount_account_id.strip():
                raise DomainError(
                    "erp_return_money_invalid",
                    "优惠金额大于 0 时必须提供优惠承担账户",
                )
            resolution = await self._resolve_reference(
                "settlement_account", discount_account_id.strip(),
            )
            if resolution["status"] != "matched":
                ready = False
            else:
                discount_account = resolution["selected"]
        return refund, discount, refund_account, discount_account, ready

    def _build_return_preview_summary(
        self,
        *,
        document_kind: str,
        source_order_no: str,
        counterparty_label: str,
        counterparty_name: str,
        warehouse_name: str,
        handler_name: str,
        doc_date: str,
        items: list[dict[str, Any]],
        total_amount: Decimal,
        refund_label: str,
        refund_amount: float | None,
        refund_account: dict[str, Any] | None,
        discount_amount: float | None,
        discount_account: dict[str, Any] | None,
        remark: str,
    ) -> dict[str, Any]:
        preview: dict[str, Any] = {
            "document_kind": document_kind,
            "source_order_no": source_order_no,
            "counterparty_label": counterparty_label,
            counterparty_label: counterparty_name,
            "warehouse": warehouse_name,
            "handler": handler_name,
            "return_date": doc_date,
            "remark": remark,
            "items": [
                {
                    "name": item["product_name"],
                    "quantity": item["quantity"],
                    "unit": item["unit"],
                    "unit_price": item["unit_price"],
                }
                for item in items
            ],
            "total_amount": float(total_amount),
        }
        if refund_amount is not None and refund_amount > 0:
            preview[refund_label] = refund_amount
            preview["refund_account"] = str((refund_account or {}).get("name") or "")
        if discount_amount is not None and discount_amount > 0:
            preview["discount_amount"] = discount_amount
            preview["discount_account"] = str((discount_account or {}).get("name") or "")
        return preview

    # ------------------------------------------------------------------
    # 内部辅助：资金单据预览
    # ------------------------------------------------------------------

    async def _resolve_fund_type(
        self,
        direction: str,
        value: str,
        writeoffs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """解析收付款单款项类型；ERP 要求 typeId 必填。

        未提供时按核销场景自动选择系统类型；既未提供也无法自动选择时
        返回该方向全部候选（系统销售收款类型强制核销，无核销收款必须
        选自定义类型），由模型引导用户补充 fund_type 后重新预览。
        """
        context = self._contexts.get()
        effective = value.strip() or self._auto_fund_type_code(direction, writeoffs)
        snapshot = await self._api.search_fund_types(
            context, _FUND_TYPE_DIRECTION_CODES[direction],
        )
        options = list(snapshot.options)
        if not effective:
            return {
                "status": "missing",
                "query": "",
                "selected": None,
                "candidates": options[:5],
            }
        deduped = self._deduplicate_reference_options(options, effective)
        normalized = normalize_name(effective)
        exact = [
            option
            for option in deduped
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
                "query": effective,
                "selected": exact[0],
                "candidates": [],
            }
        candidates = deduped[:5]
        return {
            "status": "ambiguous" if candidates else "unmatched",
            "query": effective,
            "selected": None,
            "candidates": candidates,
        }

    @staticmethod
    def _auto_fund_type_code(
        direction: str,
        writeoffs: list[dict[str, Any]],
    ) -> str:
        """按核销场景推断系统款项类型编码；无核销时返回空。"""
        biz_types = {item["bizType"] for item in writeoffs}
        if direction == "receipt":
            if "sales_order" in biz_types:
                return "sales"
            if "purchase_return" in biz_types:
                return "purchase_refund"
            return ""
        if "purchase_order" in biz_types:
            return "purchase"
        if "sales_return" in biz_types:
            return "sales_refund"
        return ""

    async def _build_financial_order_preview(
        self,
        *,
        context: Any,
        direction: str,
        amount: float,
        account: str,
        handler: str,
        customer: str,
        supplier: str,
        order_date: str,
        discount_amount: float | None,
        discount_account: str,
        fund_type: str,
        writeoff_details: list[dict[str, Any]] | None,
        remark: str,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], bool, list[str]]:
        """构建收款/付款单预览：解析账户、往来单位与款项类型并校验核销。"""
        is_receipt = direction == "receipt"
        amount_label = "收款金额" if is_receipt else "付款金额"
        if customer.strip() and supplier.strip():
            raise DomainError(
                "erp_financial_order_counterparty_invalid",
                "客户与供应商只能提供其一",
            )
        effective_date = order_date.strip() or date.today().isoformat()
        self._validate_order_date(effective_date)
        if len(remark.strip()) > 200:
            raise DomainError("erp_financial_order_remark_too_long", "备注最多 200 个字符")
        account_resolution = await self._resolve_reference(
            "settlement_account", account.strip(),
        )
        handler_resolution = await self._resolve_reference("handler", handler.strip())
        references: dict[str, dict[str, Any]] = {
            "account": account_resolution,
            "handler": handler_resolution,
        }
        customer_resolution = None
        supplier_resolution = None
        if customer.strip():
            customer_resolution = await self._resolve_reference("customer", customer.strip())
            references["customer"] = customer_resolution
        if supplier.strip():
            supplier_resolution = await self._resolve_reference("supplier", supplier.strip())
            references["supplier"] = supplier_resolution
        discount = (
            non_negative_amount(discount_amount, "优惠金额")
            if discount_amount is not None
            else None
        )
        discount_resolution = None
        if discount is not None and discount > 0:
            discount_resolution = await self._resolve_reference(
                "settlement_account", discount_account.strip(),
            )
            references["discount_account"] = discount_resolution

        writeoffs = self._normalize_writeoff_details(writeoff_details)
        if writeoffs:
            writeoff_total = sum(item["writeoffAmount"] for item in writeoffs)
            if writeoff_total > amount + 0.005:
                raise DomainError(
                    "erp_writeoff_details_invalid",
                    "核销总额 %.2f 不能超过%s %.2f" % (writeoff_total, amount_label, amount),
                )
            self._validate_writeoff_counterparty(writeoffs, customer_resolution, supplier_resolution)

        fund_type_resolution = await self._resolve_fund_type(direction, fund_type, writeoffs)
        references["fund_type"] = fund_type_resolution

        ready = bool(
            account_resolution["status"] == "matched"
            and handler_resolution["status"] == "matched"
            and (discount_resolution is None or discount_resolution["status"] == "matched")
            and fund_type_resolution["status"] == "matched"
        )
        required_actions: list[str] = []
        if ready:
            required_actions = ["confirm_submit"]
        else:
            for field, resolution in references.items():
                if resolution["status"] == "ambiguous":
                    required_actions.append("select_%s" % field)
                elif resolution["status"] == "unmatched":
                    required_actions.append("replace_%s" % field)
            if fund_type_resolution["status"] == "missing":
                required_actions.append("provide_fund_type")

        counterparty_label = ""
        counterparty_name = ""
        payload: dict[str, Any] = {
            "id": 0,
            "orderDate": effective_date,
            "accountId": "",
            "handlerId": "",
            "%sAmount" % ("receipt" if is_receipt else "payment"): amount,
            "saveType": 2,
        }
        if ready:
            selected_account = account_resolution["selected"]
            payload["accountId"] = str(selected_account["id"])
            payload["handlerId"] = str(handler_resolution["selected"]["id"])
            payload["typeId"] = str(fund_type_resolution["selected"]["id"])
            if customer_resolution is not None and customer_resolution["status"] == "matched":
                payload["counterpartyType"] = "customer"
                payload["customerId"] = str(customer_resolution["selected"]["id"])
                counterparty_label = "客户"
                counterparty_name = str(customer_resolution["selected"].get("name") or "")
            elif supplier_resolution is not None and supplier_resolution["status"] == "matched":
                payload["counterpartyType"] = "supplier"
                payload["supplierId"] = str(supplier_resolution["selected"]["id"])
                counterparty_label = "供应商"
                counterparty_name = str(supplier_resolution["selected"].get("name") or "")
            else:
                payload[
                    "skip%sValidation" % ("Customer" if is_receipt else "Supplier")
                ] = True
            if discount is not None and discount > 0 and discount_resolution is not None:
                payload["discountAmount"] = discount
                payload["discountAccountId"] = str(discount_resolution["selected"]["id"])
            if writeoffs:
                payload["writeoffDetails"] = writeoffs
            if remark.strip():
                payload["remark"] = remark.strip()

        preview: dict[str, Any] = {
            "document_kind": "%s_order" % direction,
            "order_date": effective_date,
            "amount_label": amount_label,
            "amount": amount,
            "fund_type": (
                str(fund_type_resolution["selected"].get("name") or "")
                if fund_type_resolution["selected"] is not None
                else ""
            ),
            "account": (
                str(account_resolution["selected"].get("name") or "")
                if account_resolution["selected"] is not None
                else ""
            ),
            "handler": (
                str(handler_resolution["selected"].get("name") or "")
                if handler_resolution["selected"] is not None
                else ""
            ),
            "remark": remark.strip(),
        }
        if counterparty_label:
            preview["counterparty_label"] = counterparty_label
            preview[counterparty_label] = counterparty_name
        if discount is not None and discount > 0:
            preview["discount_amount"] = discount
            if discount_resolution is not None and discount_resolution["selected"] is not None:
                preview["discount_account"] = str(discount_resolution["selected"].get("name") or "")
        if writeoffs:
            preview["writeoff_details"] = [
                {
                    "biz_type": item["bizType"],
                    "biz_id": item["bizId"],
                    "writeoff_amount": item["writeoffAmount"],
                }
                for item in writeoffs
            ]
        return payload, preview, references, ready, required_actions

    @staticmethod
    def _normalize_writeoff_details(
        writeoff_details: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """清洗核销明细：逐行校验类型、标识与金额合法性。"""
        if writeoff_details is None:
            return []
        if not isinstance(writeoff_details, list):
            raise DomainError("erp_writeoff_details_invalid", "writeoff_details 必须是数组")
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(writeoff_details, start=1):
            if not isinstance(raw, dict):
                raise DomainError("erp_writeoff_details_invalid", "第%d行核销明细不是 JSON 对象" % index)
            biz_type = str(raw.get("biz_type") or "").strip()
            if biz_type not in {"sales_order", "purchase_order", "sales_return", "purchase_return"}:
                raise DomainError(
                    "erp_writeoff_details_invalid",
                    "第%d行核销明细 biz_type 必须是 sales_order、purchase_order、sales_return 或 purchase_return" % index,
                )
            biz_id = str(raw.get("biz_id") or "").strip()
            if not biz_id:
                raise DomainError("erp_writeoff_details_invalid", "第%d行核销明细缺少 biz_id" % index)
            amount = non_negative_amount(raw.get("writeoff_amount"), "核销金额")
            if amount < 0.0001:
                raise DomainError("erp_writeoff_details_invalid", "核销金额必须大于 0")
            result.append(
                {"bizType": biz_type, "bizId": biz_id, "writeoffAmount": amount},
            )
        return result

    @staticmethod
    def _validate_writeoff_counterparty(
        writeoffs: list[dict[str, Any]],
        customer_resolution: dict[str, Any] | None,
        supplier_resolution: dict[str, Any] | None,
    ) -> None:
        """核销销售类单据需客户、采购类单据需供应商已解析。"""
        for item in writeoffs:
            biz_type = item["bizType"]
            needs_customer = biz_type in {"sales_order", "sales_return"}
            needs_supplier = biz_type in {"purchase_order", "purchase_return"}
            resolution = customer_resolution if needs_customer else supplier_resolution
            label = "客户" if needs_customer else "供应商"
            if (needs_customer or needs_supplier) and (
                resolution is None or resolution.get("status") != "matched"
            ):
                raise DomainError(
                    "erp_financial_order_counterparty_required",
                    "核销%s需要先提供%s" % (
                        {"sales_order": "销售单", "sales_return": "销售退货单",
                         "purchase_order": "采购单", "purchase_return": "采购退货单"}[biz_type],
                        label,
                    ),
                )

    async def _financial_order_params(
        self,
        context: Any,
        start_date: str,
        end_date: str,
        status: int | None,
        order_no: str,
        counterparty_id: str,
        account_id: str,
        *,
        customer_field: str,
        supplier_field: str,
    ) -> dict[str, Any]:
        self._validate_date_range(start_date.strip(), end_date.strip())
        params: dict[str, Any] = {}
        if start_date.strip():
            params["startDate"] = start_date.strip()
        if end_date.strip():
            params["endDate"] = end_date.strip()
        if status is not None:
            params["status"] = int(status)
        if order_no.strip():
            params["orderNo"] = order_no.strip()
        if counterparty_id.strip():
            customer = await self._resolve_reference("customer", counterparty_id.strip())
            if customer["status"] == "matched" and customer["selected"] is not None:
                params[customer_field] = str(customer["selected"]["id"])
            else:
                supplier = await self._resolve_reference("supplier", counterparty_id.strip())
                if supplier["status"] == "matched" and supplier["selected"] is not None:
                    params[supplier_field] = str(supplier["selected"]["id"])
                else:
                    raise DomainError(
                        "erp_reference_unmatched",
                        "未找到与“%s”匹配的客户或供应商" % counterparty_id.strip(),
                    )
        if account_id.strip():
            account = await self._resolve_reference("settlement_account", account_id.strip())
            if account["status"] == "matched" and account["selected"] is not None:
                params["accountId"] = str(account["selected"]["id"])
            else:
                raise DomainError(
                    "erp_reference_unmatched",
                    "未找到与“%s”匹配的结算账户" % account_id.strip(),
                )
        return params

    # ------------------------------------------------------------------
    # 内部辅助：作废与列表
    # ------------------------------------------------------------------

    async def _void_document_flow(
        self,
        kind: str,
        doc_label: str,
        document_id: str,
        confirmed_by_user: bool,
        void: Callable[[str], Any],
    ) -> str:
        """作废单据通用流程：确认、回查编号、执行、回显编号。"""
        if confirmed_by_user is not True:
            raise DomainError(
                "erp_document_confirmation_required",
                "必须先向用户展示%s详情并取得明确确认" % doc_label,
            )
        token = document_id.strip()
        if not token:
            raise DomainError("erp_%s_id_invalid" % kind, "%s ID 不能为空" % doc_label)
        document_no = await self._lookup_document_no(kind, token)
        await void(token)
        return document_no or token

    async def _void_financial_flow(
        self,
        kind: str,
        doc_label: str,
        order_id: str,
        confirmed_by_user: bool,
    ) -> str:
        context = self._contexts.get()
        if confirmed_by_user is not True:
            raise DomainError(
                "erp_document_confirmation_required",
                "必须先向用户展示%s详情并取得明确确认" % doc_label,
            )
        token = order_id.strip()
        if not token:
            raise DomainError("erp_financial_order_id_invalid", "%s ID 不能为空" % doc_label)
        document_no = ""
        try:
            detail = await self._api.get_financial_order_detail(context, kind, token)
            document_no = str(detail.document.get("orderNo") or "").strip()
        except DomainError:
            document_no = ""
        await self._api.void_financial_order(context, kind, token)
        return document_no or token

    def _document_page_response(self, result: Any) -> dict[str, Any]:
        return self.ok_response(
            page=result.page_num,
            page_size=result.page_size,
            total=result.total,
            has_more=(result.page_num * result.page_size) < result.total,
            documents=list(result.rows),
        )

    def _build_purchase_modify_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """清洗采购单修改明细：必填 product_id、quantity、unit_price。"""
        if not items:
            raise DomainError("erp_purchase_order_items_empty", "商品明细不能为空")
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                raise DomainError("erp_purchase_order_item_invalid", "第%d行商品明细不是 JSON 对象" % index)
            product_id = str(raw.get("product_id") or raw.get("productId") or "").strip()
            if not product_id:
                raise DomainError("erp_purchase_order_item_invalid", "第%d行商品明细缺少 product_id" % index)
            quantity = raw.get("quantity")
            if quantity is None:
                raise DomainError("erp_purchase_order_item_invalid", "第%d行商品明细缺少 quantity" % index)
            item: dict[str, Any] = {
                "productId": product_id,
                "quantity": non_negative_amount(quantity, "采购数量"),
            }
            if item["quantity"] < 0.0001:
                raise DomainError("erp_purchase_order_item_invalid", "第%d行商品明细数量必须大于 0" % index)
            if raw.get("unit_price") is None and raw.get("unitPrice") is None:
                raise DomainError("erp_purchase_order_item_invalid", "第%d行商品明细缺少 unit_price" % index)
            item["unitPrice"] = non_negative_amount(
                raw.get("unit_price") if raw.get("unit_price") is not None else raw.get("unitPrice"),
                "采购单价",
            )
            for camel_key, snake_key in (
                ("unit", "unit"),
                ("remark", "remark"),
                ("orderItemId", "order_item_id"),
            ):
                value = raw.get(snake_key)
                if value not in (None, ""):
                    item[camel_key] = value
            result.append(item)
        return result


def build_document_tools(host: DocumentTools) -> list[SessionFunctionTool]:
    """把采购、退货、库存与资金单据工具注册为 MCP 工具。"""
    return [
        # 采购单
        SessionFunctionTool(
            host.preview_purchase_order,
            is_read_only=True,
            is_concurrency_safe=False,
            output_schema=_TEXT_DOCUMENT_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_PURCHASE_ORDER_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_purchase_order,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_purchase_order,
            is_read_only=True,
            output_schema=_DOCUMENT_GET_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_purchase_orders,
            is_read_only=True,
            output_schema=_DOCUMENT_LIST_OUTPUT_SCHEMA,
            input_schema_override=_LIST_PURCHASE_ORDERS_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.void_purchase_order,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_VOID_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.update_purchase_order,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_UPDATE_OUTPUT_SCHEMA,
            input_schema_override=_UPDATE_PURCHASE_ORDER_INPUT_SCHEMA,
        ),
        # 采购退货单
        SessionFunctionTool(
            host.preview_purchase_return,
            is_read_only=True,
            is_concurrency_safe=False,
            output_schema=_RETURN_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_PURCHASE_RETURN_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_purchase_return,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_purchase_return,
            is_read_only=True,
            output_schema=_DOCUMENT_GET_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_purchase_returns,
            is_read_only=True,
            output_schema=_DOCUMENT_LIST_OUTPUT_SCHEMA,
            input_schema_override=_LIST_PURCHASE_RETURNS_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.void_purchase_return,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_VOID_OUTPUT_SCHEMA,
        ),
        # 销售退货单
        SessionFunctionTool(
            host.preview_sales_return,
            is_read_only=True,
            is_concurrency_safe=False,
            output_schema=_RETURN_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_SALES_RETURN_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_sales_return,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_sales_return,
            is_read_only=True,
            output_schema=_DOCUMENT_GET_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_sales_returns,
            is_read_only=True,
            output_schema=_DOCUMENT_LIST_OUTPUT_SCHEMA,
            input_schema_override=_LIST_SALES_RETURNS_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.void_sales_return,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_VOID_OUTPUT_SCHEMA,
        ),
        # 销售单继续收款 / 采购单继续付款
        SessionFunctionTool(
            host.preview_sales_receipt,
            is_read_only=True,
            is_concurrency_safe=False,
            output_schema=_MONEY_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_SALES_RECEIPT_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_sales_receipt,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.preview_purchase_payment,
            is_read_only=True,
            is_concurrency_safe=False,
            output_schema=_MONEY_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_PURCHASE_PAYMENT_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_purchase_payment,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        # 库存调拨与其他出入库
        SessionFunctionTool(
            host.preview_stock_transfer,
            is_read_only=True,
            is_concurrency_safe=False,
            output_schema=_TEXT_DOCUMENT_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_STOCK_TRANSFER_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_stock_transfer,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.preview_other_stock_doc,
            is_read_only=True,
            is_concurrency_safe=False,
            output_schema=_TEXT_DOCUMENT_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_OTHER_STOCK_DOC_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_other_stock_doc,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        # 收款单
        SessionFunctionTool(
            host.preview_receipt_order,
            is_read_only=True,
            is_concurrency_safe=False,
            output_schema=_MONEY_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_RECEIPT_ORDER_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_receipt_order,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_receipt_order,
            is_read_only=True,
            output_schema=_DOCUMENT_GET_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_receipt_orders,
            is_read_only=True,
            output_schema=_DOCUMENT_LIST_OUTPUT_SCHEMA,
            input_schema_override=_LIST_FINANCIAL_ORDERS_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.void_receipt_order,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_VOID_OUTPUT_SCHEMA,
        ),
        # 付款单
        SessionFunctionTool(
            host.preview_payment_order,
            is_read_only=True,
            is_concurrency_safe=False,
            output_schema=_MONEY_PREVIEW_OUTPUT_SCHEMA,
            input_schema_override=_PREVIEW_PAYMENT_ORDER_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.submit_payment_order,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_SUBMIT_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.get_payment_order,
            is_read_only=True,
            output_schema=_DOCUMENT_GET_OUTPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.list_payment_orders,
            is_read_only=True,
            output_schema=_DOCUMENT_LIST_OUTPUT_SCHEMA,
            input_schema_override=_LIST_FINANCIAL_ORDERS_INPUT_SCHEMA,
        ),
        SessionFunctionTool(
            host.void_payment_order,
            is_concurrency_safe=False,
            output_schema=_DOCUMENT_VOID_OUTPUT_SCHEMA,
        ),
    ]

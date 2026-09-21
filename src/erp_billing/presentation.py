"""ERP MCP 用户展示投影。

工具内部结果同时承担两类职责：

* 结构化结果供 Agent 继续完成匹配、确认和提交；
* 文本 content 供用户界面和纯文本客户端展示。

本模块只处理第二类结果，不修改业务 ToolSet、Port 或 ERP API 的数据契约。
内部控制字段和 ERP 技术字段从展示投影中剥离，业务字段保留并使用中文标签。
"""

from __future__ import annotations

import json
import re
from typing import Any


_HIDDEN_KEYS = frozenset(
    {
        "ok",
        "code",
        "details",
        "error",
        "preview_id",
        "document_id",
        "line_id",
        "selection_token",
        "idempotency_key",
        "catalog_version",
        "required_actions",
        "confirmed_products",
        "confirmed_units",
        "confirmed_prices",
        "partial",
        "source",
        "raw",
        "payload",
        "page",
        "page_size",
        "query",
        "reference_type",
        "total",
    },
)

_HIDDEN_KEY_PARTS = (
    "trace",
    "token",
    "credential",
    "authorization",
    "password",
    "secret",
    "version",
)

_LABELS = {
    "product_name": "商品名称",
    "name": "名称",
    "unit": "单位",
    "specification": "规格型号",
    "price": "价格",
    "purchase_price": "采购价",
    "sales_price": "销售价",
    "stock": "库存",
    "quantity": "数量",
    "requested_quantity": "原数量",
    "requested_unit": "原单位",
    "erp_unit": "ERP单位",
    "unit_price": "单价",
    "amount": "金额",
    "total_amount": "合计金额",
    "customer": "客户",
    "supplier": "供应商",
    "warehouse": "仓库",
    "handler": "经手人",
    "settlement_account": "结算账户",
    "order_no": "单据编号",
    "document_no": "单据编号",
    "return_no": "退货单号",
    "order_date": "单据日期",
    "date": "日期",
    "remark": "备注",
    "status": "状态",
    "payment_status": "付款状态",
    "return_status": "退货状态",
    "save_type": "保存类型",
    "kind": "类型",
    "type": "类型",
    "view": "视图",
    "is_default": "默认项",
    "missing_required_fields": "待补充信息",
    "reference_resolutions": "基础资料匹配",
    "selected": "已选择",
    "candidates": "候选项",
    "options": "候选项",
    "preview": "单据预览",
    "items": "明细",
    "documents": "单据列表",
    "orders": "单据列表",
    "products": "商品列表",
    "data": "查询结果",
    "source_order": "源单",
    "types": "类型列表",
    "sample_products": "商品示例",
    "product": "商品",
    "similar_products": "相似商品",
    "recommendations": "候选商品",
    "prompt": "说明",
    "message": "说明",
    "submitted": "提交结果",
    "voided": "作废结果",
    "modified": "修改结果",
    "ready_to_submit": "提交状态",
    "has_more": "还有更多",
    "is_system": "系统项",
    "unmatched_products": "未匹配商品",
    "recommended_products": "推荐商品",
    "unit_warnings": "单位提示",
    "price_warnings": "价格提示",
    "error_message": "业务提示",
}

_TECHNICAL_ERROR_CODES = frozenset(
    {
        "tool_arguments_invalid",
        "erp_document_idempotency_key_invalid",
        "erp_document_idempotency_key_conflict",
        "erp_document_confirmation_required",
    },
)

_TECHNICAL_ERROR_FIELDS = (
    "preview_id",
    "document_id",
    "order_id",
    "product_id",
    "customer_id",
    "supplier_id",
    "warehouse_id",
    "handler_id",
    "order_item_id",
    "line_id",
    "save_type",
    "confirmed_by_user",
    "idempotency_key",
    "page_size",
)


def _snake_key(key: str) -> str:
    """把常见 camelCase/缩写字段归一为 snake_case，便于统一过滤。"""
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return value.replace("-", "_").lower()


def _is_hidden_key(key: str) -> bool:
    normalized = _snake_key(key)
    if normalized in _HIDDEN_KEYS:
        return True
    if normalized == "id" or normalized.endswith("_id"):
        return True
    return any(part in normalized for part in _HIDDEN_KEY_PARTS)


def _display_label(key: str) -> str:
    normalized = _snake_key(key)
    return _LABELS.get(normalized, str(key))


def _business_error(result: dict[str, Any]) -> str:
    error = result.get("error")
    if not isinstance(error, dict):
        return "业务处理未完成。"
    code = str(error.get("code") or "")
    message = str(error.get("message") or "业务处理未完成").strip()
    if code in _TECHNICAL_ERROR_CODES or "未知参数" in message:
        return "业务处理未完成：请求格式不正确，请按当前业务场景重新提交。"
    for field in _TECHNICAL_ERROR_FIELDS:
        message = message.replace(field, "相关业务信息")
    # 错误消息中的“标识：内部 ID”不能进入用户展示内容；保留前半段业务原因。
    if (
        "_id_" in code
        or code.endswith("_not_found")
        or "标识" in message
        or " ID" in message
    ):
        message = re.split(r"[：:]", message, maxsplit=1)[0].strip()
    return "业务处理未完成：%s" % message


def _project_value(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if _is_hidden_key(raw_key):
                continue
            normalized = _snake_key(raw_key)
            if normalized == "is_default":
                if raw_value:
                    projected["说明"] = "默认项"
                continue
            if normalized == "is_system":
                if raw_value:
                    projected["说明"] = "系统项"
                continue
            if normalized == "has_more":
                if raw_value:
                    projected["提示"] = "还有更多结果"
                continue
            child = _project_value(raw_value, key=normalized)
            if child in (None, "", [], {}):
                continue
            projected[_display_label(raw_key)] = child
        return projected
    if isinstance(value, (list, tuple)):
        return [item for item in (_project_value(item, key=key) for item in value) if item not in (None, "", [], {})]
    if isinstance(value, bool):
        if key == "ready_to_submit":
            return "待确认提交" if value else "尚未就绪"
        if key in {"submitted", "voided", "modified"}:
            return "是" if value else "否"
        return value
    return value


def render_billing_result(tool_name: str, result: dict[str, Any]) -> str:
    """把工具结果转换为只含业务信息的文本 content。

    ``structuredContent`` 仍保持原结果，确保 Agent 的已有执行链和旧客户端
    兼容；本函数只用于 MCP 协议的文本展示通道。
    """
    if result.get("ok") is False:
        return _business_error(result)

    projected = _project_value(result)
    if not projected:
        return "业务操作已完成。"
    return "业务结果：\n%s" % json.dumps(
        projected,
        ensure_ascii=False,
        indent=2,
    )


__all__ = ["render_billing_result"]

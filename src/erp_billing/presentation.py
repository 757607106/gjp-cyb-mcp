"""ERP MCP 双通道白名单：业务文本与保留必要执行字段的结构化结果。"""

from __future__ import annotations

import re
from typing import Any


_COMMON_EXECUTION_FIELDS = frozenset({
    "ok", "page", "page_size", "total", "query", "keyword", "reference_type",
    "kind", "type", "view", "is_system", "ready_to_submit", "save_type",
    # 这些字段由后续确认/提交工具继续使用，必须保留在结构化通道中。
    "document_kind", "save_type_label", "orderDate", "documentNo",
})

_EXECUTION_FIELDS_BY_TOOL = {
    "search_products": frozenset({"product_id"}),
    "search_billing_references": frozenset({"id", "code"}),
    "list_stock_doc_types": frozenset({"id"}),
    "get_sales_order": frozenset({
        "id", "product_id", "order_item_id", "biz_id", "unit_id",
        "handler_id", "customer_id", "warehouse_id", "supplier_id",
    }),
    **dict.fromkeys(
        (
            "preview_sales_order", "preview_purchase_order", "preview_sales_return", "preview_purchase_return",
            "preview_sales_receipt", "preview_purchase_payment", "preview_stock_transfer", "preview_other_stock_doc",
            "preview_receipt_order", "preview_payment_order",
        ),
        frozenset({
            "preview_id", "required_actions", "confirmed_products", "field", "code",
            "id", "product_id", "line_id", "order_id", "source_order_id", "biz_id",
        }),
    ),
    **dict.fromkeys(
        (
            "list_sales_orders", "get_purchase_order", "list_purchase_orders",
            "get_sales_return", "list_sales_returns", "get_purchase_return", "list_purchase_returns",
            "get_receipt_order", "list_receipt_orders", "get_payment_order", "list_payment_orders",
        ),
        frozenset({"id", "product_id", "order_item_id", "biz_id"}),
    ),
    **dict.fromkeys(
        (
            "submit_sales_order", "submit_purchase_order", "submit_sales_return", "submit_purchase_return",
            "submit_sales_receipt", "submit_purchase_payment", "submit_stock_transfer", "submit_other_stock_doc",
            "submit_receipt_order", "submit_payment_order",
        ),
        frozenset({"document_id", "idempotent_replay"}),
    ),
    **dict.fromkeys(
        ("query_stock", "get_stock_by_product", "query_stock_logs", "list_stock_alerts", "get_purchase_suggestions"),
        frozenset({"product_id", "warehouse_id", "biz_id"}),
    ),
    "list_receivables": frozenset({"id", "customer_id", "customerId", "biz_id"}),
    "list_payables": frozenset({"id", "supplier_id", "supplierId", "biz_id"}),
}

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
    "line_amount": "金额",
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
    "is_default": "默认项",
    "missing_required_fields": "待补充信息",
    "reference_resolutions": "业务资料",
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
    "has_more": "还有更多",
    "unmatched_products": "待确认商品",
    "recommended_products": "推荐商品",
    "unit_warnings": "单位提示",
    "price_warnings": "价格提示",
    "error_message": "业务提示",
    # 商品与单位
    "spec": "规格型号",
    "product_code": "商品编号",
    "unit_name": "单位",
    "units": "单位列表",
    "retail_price": "零售价",
    "conversion_rate": "换算率",
    "stock_quantity": "库存数量",
    "base_quantity": "基本数量",
    "returned_qty": "已退数量",
    "returnable_qty": "可退数量",
    "avg_cost": "平均成本",
    # 仓库与库存
    "warehouse_name": "仓库",
    "warehouse_code": "仓库编号",
    "warehouse_address": "仓库地址",
    "warehouse_stocks": "各仓库库存",
    "opening_quantity": "期初数量",
    "allocated_qty": "已分配数量",
    "available_qty": "可用数量",
    "current_quantity": "当前库存",
    "min_stock": "最低库存",
    "max_stock": "最高库存",
    "sku_count": "商品数",
    "negative_stock_count": "负库存商品数",
    "zero_stock_count": "零库存商品数",
    "low_stock_count": "库存不足商品数",
    "over_stock_count": "库存积压商品数",
    "severity": "预警级别",
    "severity_name": "预警级别",
    "alert_type": "预警类型",
    "alert_type_name": "预警类型",
    "change_qty": "变动数量",
    "change_amount": "变动金额",
    "before_qty": "变动前数量",
    "after_qty": "变动后数量",
    "log_date": "记录日期",
    "suggested_quantity": "建议采购数量",
    "suggested_amount": "建议采购金额",
    "total_quantity": "合计数量",
    "total_qty": "合计数量",
    # 客户/供应商/往来单位
    "customer_name": "客户",
    "customer_code": "客户编号",
    "customer_address": "客户地址",
    "customer_contact": "客户联系人",
    "customer_phone": "客户电话",
    "supplier_name": "供应商",
    "supplier_code": "供应商编号",
    "supplier_address": "供应商地址",
    "supplier_contact": "供应商联系人",
    "supplier_phone": "供应商电话",
    "counterparty_name": "往来单位",
    "counterparty_code": "往来单位编号",
    "counterparty_type": "往来单位类型",
    "counterparty_balance_before": "交易前余额",
    "counterparty_balance_after": "交易后余额",
    "handler_name": "经手人",
    "contact_person": "联系人",
    "contact_phone": "联系电话",
    # 单据
    "status_name": "状态",
    "payment_status_name": "付款状态",
    "return_status_name": "退货状态",
    "biz_date": "业务日期",
    "biz_no": "业务单号",
    "biz_type": "业务类型",
    "biz_type_name": "业务类型",
    "document_type": "单据类型",
    "last_transaction_date": "最近交易日期",
    "order_count": "单据数",
    "financial_status": "结算状态",
    # 金额与财务
    "net_amount": "净额",
    "discount_amount": "折扣金额",
    "paid_amount": "已付金额",
    "payable_amount": "应付金额",
    "unpaid_amount": "未付金额",
    "exempt_payment_amount": "免付金额",
    "exempt_receipt_amount": "免收金额",
    "real_payment_amount": "实付金额",
    "real_receipt_amount": "实收金额",
    "receivable_amount": "应收金额",
    "receivable_balance": "应收余额",
    "received_amount": "已收金额",
    "unreceived_amount": "未收金额",
    "payable_balance": "应付余额",
    "initial_amount": "期初金额",
    "initial_completed_amount": "期初已结金额",
    "initial_remaining_as_of_end": "期初未结金额",
    "pre_received": "预收金额",
    "prepaid": "预付金额",
    "balance": "余额",
    "opening_balance": "期初余额",
    "closing_balance": "期末余额",
    "total_assets": "资产总额",
    "total_liabilities": "负债总额",
    "net_assets": "净资产",
    "assets": "资产",
    "liabilities": "负债",
    "account_details": "账户明细",
    "account_name": "账户名称",
    "account_type": "账户类型",
    "account_settlements": "账户结算",
    "payment_amount": "付款金额",
    "receipt_amount": "收款金额",
    "profit": "利润",
    "profit_amount": "利润",
    "profit_rate": "利润率",
    "gross_profit": "毛利",
    "gross_profit_rate": "毛利率",
    "total_income": "总收入",
    "total_expense": "总支出",
    "income_items": "收入项目",
    "expense_items": "支出项目",
    "total_profit": "利润总额",
    "total_receipt": "收款合计",
    "total_payment": "付款合计",
    "total_cost": "总成本",
    "total_receivable": "应收总额",
    "total_payable": "应付总额",
    "total_occurrence_amount": "发生金额",
    "discount_settlements": "折扣结算",
    "cost_amount": "成本金额",
    "cost_price": "成本单价",
    # 报表
    "monthly_data": "月度数据",
    "month": "月份",
    "rank": "排名",
    "sales_count": "销售笔数",
    "sales_amount": "销售金额",
    "sales_qty": "销售数量",
    "sales_quantity": "销售数量",
    "purchase_amount": "采购金额",
    "purchase_count": "采购笔数",
    "purchase_qty": "采购数量",
    "return_amount": "退货金额",
    "return_count": "退货笔数",
    "return_qty": "退货数量",
    "return_cost_amount": "退货成本金额",
    "net_cost_amount": "净成本金额",
    "net_qty": "净数量",
    # 其他
    "results": "搜索结果",
    "type_code": "类型编号",
    "type_name": "类型名称",
    "product_count": "商品数",
    "label": "名称",
    "order": "单据",
    "document": "单据",
    "list": "明细",
    "children": "子项目",
    "discount_rate": "折扣率",
    "stock_status": "库存状态",
    "invoice_status": "开票状态",
    "receipt_account_name": "收款账户",
    "payment_account_name": "付款账户",
    "discount_account_name": "优惠账户",
    "receipt_records": "收款记录",
    "payment_records": "付款记录",
    "return_date": "退货日期",
    "source_order_no": "源单号",
    "purchase_order_no": "采购单号",
    "refund_amount": "应退金额",
    "refunded_amount": "已退金额",
    "unrefunded_amount": "未退金额",
    "real_refund_amount": "实账退款金额",
    "exempt_refund_amount": "免账退款金额",
    "refund_status": "退款状态",
    "refund_status_name": "退款状态",
    "refund_account_name": "退款账户",
    "refund_flows": "退款流水",
    "refund_records": "退款记录",
    "writeoff_payment_orders": "关联付款单",
    "writeoff_receipt_orders": "关联收款单",
    "source_paid_amount": "原单已付金额",
    "source_payable_amount": "原单应付金额",
    "is_base": "基本单位",
    "preset_price1": "预设售价一",
    "preset_price2": "预设售价二",
    "receipt_no": "收款单号",
    "receipt_date": "收款日期",
    "receipt_time": "收款时间",
    "payment_no": "付款单号",
    "payment_date": "付款日期",
    "payment_time": "付款时间",
    "refund_no": "退款单号",
    "refund_date": "退款日期",
    "refund_time": "退款时间",
    "source_type": "收付来源",
    "source_type_name": "收付来源",
    "real_amount": "实账金额",
    "real_account_name": "实账账户",
    "flow_date": "流水日期",
    "document_date": "单据日期",
    "business_no": "业务单号",
    "business_type": "业务类型",
    "business_type_name": "业务类型",
    "flow_direction": "收支方向",
    "flow_direction_name": "收支方向",
    "before_balance": "变动前余额",
    "after_balance": "变动后余额",
    "payment_method": "支付方式",
    "counterparty_type_name": "往来单位类型",
    "account_type_name": "账户类型",
    "is_virtual": "虚拟免账流水",
    "payment_order_no": "付款单号",
    "receipt_order_no": "收款单号",
    "writeoff_amount": "核销金额",
    "writeoff_time": "核销时间",
    "writeoff_details": "核销明细",
    "settled_amount": "已结金额",
    "unsettled_amount": "未结金额",
    "has_writeoff": "已核销",
    "order_type": "单据类型",
    "out_account_name": "付款账户",
    "direction": "收付款性质",
    "direction_name": "收付款性质",
    "category_name": "商品分类",
    "recent_logs": "最近流水",
    "source_biz_type": "原单业务类型",
    "transfer_no": "调拨单号",
    "transfer_date": "调拨日期",
    "from_warehouse_name": "调出仓库",
    "to_warehouse_name": "调入仓库",
    "inbound_no": "入库单号",
    "inbound_date": "入库日期",
    "inbound_type_name": "入库类型",
    "outbound_no": "出库单号",
    "outbound_date": "出库日期",
    "outbound_type_name": "出库类型",
    "range_receivable": "期间应收发生额",
    "range_payable": "期间应付发生额",
    "range_received": "期间已收金额",
    "range_paid": "期间已付金额",
    "range_unreceived": "期间应收净变动",
    "range_unpaid": "期间应付净变动",
    "range_occurrence_amount": "期间业务发生额",
    "range_initial_completed_amount": "期间期初结清金额",
    "range_settlement_amount": "期间结清金额",
    "initial_as_of_start": "起日前余额",
    "closing_as_of_end": "截止日余额",
    "initial_arrears": "期初欠款",
    "business_arrears": "业务欠款",
    "total_arrears": "合计欠款",
    "total_received": "当前页已收合计",
    "total_unreceived": "当前页欠款合计",
    "from_warehouse": "调出仓库",
    "to_warehouse": "调入仓库",
    "doc_date": "单据日期",
    "doc_type": "单据类型",
    "fund_type": "款项类型",
    "account": "账户",
    "receipt_account": "收款账户",
    "payment_account": "付款账户",
    "refund_account": "退款账户",
    "discount_account": "优惠账户",
    "客户": "客户",
    "供应商": "供应商",
    "退款金额": "退款金额",
}

_ENUM_FIELDS = frozenset({
    "status", "payment_status", "return_status", "stock_status", "invoice_status", "refund_status",
    "source_type", "business_type", "biz_type", "flow_direction", "counterparty_type", "account_type",
    "order_type", "direction", "severity", "alert_type", "source_biz_type", "financial_status", "document_type",
})
_WARNING_PROMPTS = {
    "unit_warnings": "请按ERP单位确认数量；系统不会猜测换算。",
    "price_warnings": "商品缺少有效采购价，请确认单价后重新预览。",
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


def _filter_value(value: Any, allowed_fields: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _filter_value(child, allowed_fields)
            for key, child in value.items()
            if _snake_key(key) in allowed_fields
        }
    if isinstance(value, (list, tuple)):
        return [_filter_value(child, allowed_fields) for child in value]
    return value


# 数量/金额/价格类字段：上游 ERP 常把数值序列化为字符串，展示前归一为数字。
# 名称、编号、电话等同样可能是纯数字文本，不能误转，因此只按字段语义后缀触发。
_NUMERIC_KEY_SUFFIXES = (
    "count",
    "qty",
    "quantity",
    "amount",
    "price",
    "rate",
    "balance",
    "cost",
    "profit",
    "stock",
)
_NUMERIC_TEXT_RE = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?$")


def _coerce_numeric_text(key: str, value: str) -> Any:
    """数值语义字段的数字文本转 int/float；其余原样返回。

    前导零文本（如 "007"）是编号写法，正则不匹配，保持字符串。
    """
    if not key.endswith(_NUMERIC_KEY_SUFFIXES):
        return value
    if not _NUMERIC_TEXT_RE.fullmatch(value):
        return value
    return float(value) if "." in value else int(value)


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
        normalized_keys = {_snake_key(raw_key) for raw_key in value}
        projected: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            normalized = _snake_key(raw_key)
            if normalized not in _LABELS:
                continue
            # 状态码与中文名称成对出现时（status + statusName）只保留名称
            if normalized + "_name" in normalized_keys:
                continue
            if normalized in _ENUM_FIELDS:
                if not isinstance(raw_value, str):
                    continue
                if raw_value.isascii():
                    continue
            if normalized == "is_default":
                if raw_value:
                    projected["说明"] = "默认项"
                continue
            if normalized == "is_base":
                if raw_value:
                    projected["说明"] = "基本单位"
                continue
            if normalized == "has_more":
                if raw_value:
                    projected["提示"] = "还有更多结果"
                continue
            if isinstance(raw_value, str):
                if normalized == "prompt" and key in _WARNING_PROMPTS:
                    child = _WARNING_PROMPTS[key]
                else:
                    child = _coerce_numeric_text(normalized, raw_value)
            else:
                child = _project_value(raw_value, key=normalized)
            if child in (None, "", [], {}):
                continue
            projected[_LABELS[normalized]] = child
        return projected
    if isinstance(value, (list, tuple)):
        return [item for item in (_project_value(item, key=key) for item in value) if item not in (None, "", [], {})]
    if isinstance(value, bool):
        if key in {"submitted", "voided", "modified"}:
            return "是" if value else "否"
        return value
    return value


def _is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list, tuple))


def _cell(value: Any) -> str:
    """把业务值转换为安全的 Markdown 表格单元格。"""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)) and all(_is_scalar(item) for item in value):
        value = "、".join(_cell(item) for item in value) or "—"
    text = str(value).replace("\r", " ").replace("\n", "、")
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    rendered_headers = [_cell(header) for header in headers]
    lines = [
        "| %s |" % " | ".join(rendered_headers),
        "| %s |" % " | ".join("---" for _ in rendered_headers),
    ]
    lines.extend("| %s |" % " | ".join(_cell(value) for value in row) for row in rows)
    return lines


def _heading(title: str, level: int) -> str:
    return "%s %s" % ("#" * min(max(level, 1), 6), title)


def _render_markdown_block(value: Any, title: str, level: int = 2) -> list[str]:
    lines = [_heading(title, level), ""]
    if isinstance(value, dict):
        scalar_items = [(key, child) for key, child in value.items() if _is_scalar(child)]
        complex_items = [(key, child) for key, child in value.items() if not _is_scalar(child)]
        if scalar_items:
            lines.extend(_table(
                ["项目", "内容"],
                [[key, child] for key, child in scalar_items],
            ))
        elif not complex_items:
            lines.append("暂无数据。")
        for key, child in complex_items:
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend(_render_markdown_block(child, key, level + 1))
        return lines

    if isinstance(value, (list, tuple)):
        if not value:
            lines.append("暂无数据。")
            return lines
        if all(isinstance(item, dict) for item in value):
            columns: list[str] = []
            for item in value:
                for key, child in item.items():
                    if _is_scalar(child) and key not in columns:
                        columns.append(key)
            if columns:
                lines.extend(_table(
                    columns,
                    [[item.get(column) for column in columns] for item in value],
                ))
            for index, item in enumerate(value, start=1):
                for key, child in item.items():
                    if _is_scalar(child):
                        continue
                    if lines and lines[-1] != "":
                        lines.append("")
                    nested_title = "%s（第%d项）" % (key, index) if len(value) > 1 else key
                    lines.extend(_render_markdown_block(child, nested_title, level + 1))
            return lines
        lines.extend("- %s" % _cell(item) for item in value)
        return lines

    lines.append(_cell(value))
    return lines


def render_billing_result(tool_name: str, result: dict[str, Any]) -> str:
    if result.get("ok") is False:
        return _business_error(result)

    projected = _project_value(result)
    if not projected:
        return "业务操作已完成。"
    lines = _render_markdown_block(projected, "业务结果", 2)
    if _snake_key(tool_name).startswith("preview_") and result.get("ready_to_submit") is True:
        lines.extend(["", "> 请核对以上业务信息；确认无误后方可提交。"])
    return "\n".join(lines).rstrip()


def filter_billing_result(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    execution_fields = _EXECUTION_FIELDS_BY_TOOL.get(_snake_key(tool_name), frozenset())
    allowed_fields = frozenset(_LABELS) | _COMMON_EXECUTION_FIELDS | execution_fields
    projected = _filter_value(result, allowed_fields)
    if isinstance(result.get("error"), dict):
        projected["error"] = {
            "code": str(result["error"].get("code") or ""),
            "message": _business_error(result).removeprefix("业务处理未完成："),
        }
    return projected


def present_billing_result(tool_name: str, result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return render_billing_result(tool_name, result), filter_billing_result(tool_name, result)


__all__ = ["filter_billing_result", "present_billing_result", "render_billing_result"]

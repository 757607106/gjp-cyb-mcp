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
        "sort_order",
        "biz_flag",
        "image_url",
        "image_urls",
        "field",
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
    "create_time": "创建时间",
    "update_time": "更新时间",
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
            if _is_hidden_key(raw_key):
                continue
            normalized = _snake_key(raw_key)
            # 状态码与中文名称成对出现时（status + statusName）只保留名称
            if normalized + "_name" in normalized_keys:
                continue
            if normalized == "is_default":
                if raw_value:
                    projected["说明"] = "默认项"
                continue
            if normalized == "is_base":
                if raw_value:
                    projected["说明"] = "基本单位"
                continue
            if normalized == "is_system":
                if raw_value:
                    projected["说明"] = "系统项"
                continue
            if normalized == "has_more":
                if raw_value:
                    projected["提示"] = "还有更多结果"
                continue
            if isinstance(raw_value, str):
                child = _coerce_numeric_text(normalized, raw_value)
            else:
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

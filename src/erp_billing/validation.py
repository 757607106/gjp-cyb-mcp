"""单据金额、数量与单位的共享校验函数。

供销售单工具与扩展业务域工具共用，纯函数、不持有状态。
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from gjp_common.errors import DomainError

from .catalog import normalize_name

_MONEY_QUANTUM = Decimal("0.01")


def normalized_unit(value: str) -> str:
    """归一化常用单位别名，单位比对用。"""
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


def line_amount(quantity: float, unit_price: float | None) -> Decimal | None:
    """按实际提交单价计算两位小数的预览行金额。"""
    if unit_price is None:
        return None
    amount = Decimal(str(quantity)) * Decimal(str(unit_price))
    if not amount.is_finite():
        return None
    try:
        return amount.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise DomainError("erp_sales_order_item_invalid", "商品数量或金额超出可处理范围") from exc


def non_negative_amount(value: Any, label: str) -> float:
    """统一校验 API 金额下界，保留正常金额数值。"""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DomainError("erp_sales_order_item_invalid", label + "必须是有效金额") from exc
    if not math.isfinite(number) or number < 0:
        raise DomainError("erp_sales_order_item_invalid", label + "必须为非负有限数")
    return number


def positive_amount(value: Any, label: str) -> float:
    """校验必须大于 0 的金额，如采购单价（ERP 拒绝 0 价采购行）。"""
    number = non_negative_amount(value, label)
    if number <= 0:
        raise DomainError("erp_sales_order_item_invalid", label + "必须大于 0")
    return number


def valid_quantity(value: Any, label: str) -> float:
    """校验数量为不小于 0.0001 的有限数。"""
    try:
        quantity = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DomainError("erp_sales_order_item_invalid", label + "必须是数字") from exc
    if not math.isfinite(quantity) or quantity < 0.0001:
        raise DomainError("erp_sales_order_item_invalid", label + "必须为不小于 0.0001 的有限数")
    return quantity

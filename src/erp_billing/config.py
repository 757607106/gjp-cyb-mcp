"""ERP AI 开单领域配置：只读商品目录、别名和推荐阈值。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from gjp_common.config import get_env_value
from gjp_common.errors import DomainError
from gjp_common.paths import resolve_input_path


def _optional_input_path(name: str) -> Optional[Path]:
    value = get_env_value(name).strip()
    return resolve_input_path(value) if value else None


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    value = get_env_value(name, str(default)).strip()
    try:
        parsed = float(value)
    except ValueError as exc:
        raise DomainError("ERP_BILLING_CONFIG_INVALID", "%s 必须是数字" % name) from exc
    if parsed < minimum or parsed > maximum:
        raise DomainError(
            "ERP_BILLING_CONFIG_INVALID",
            "%s 必须在 %.2f 到 %.2f 之间" % (name, minimum, maximum),
        )
    return parsed


def _bool_env(name: str, default: bool) -> bool:
    value = get_env_value(name, "true" if default else "false").strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise DomainError("ERP_BILLING_CONFIG_INVALID", "%s 必须是 true 或 false" % name)


@dataclass(frozen=True)
class ErpBillingSettings:
    """ERP 开单的商品目录与推荐阈值配置。"""

    product_catalog_path: Optional[Path]
    alias_path: Optional[Path]
    recommendation_score: float
    use_default_fresh_aliases: bool
    category_path: Optional[Path]
    use_default_categories: bool

    @classmethod
    def from_env(cls) -> "ErpBillingSettings":
        recommendation_score = _float_env(
            "ERP_BILLING_RECOMMENDATION_SCORE",
            0.60,
            0,
            1,
        )
        return cls(
            product_catalog_path=_optional_input_path("ERP_BILLING_PRODUCT_CATALOG"),
            alias_path=_optional_input_path("ERP_BILLING_ALIAS_FILE"),
            recommendation_score=recommendation_score,
            use_default_fresh_aliases=_bool_env(
                "ERP_BILLING_USE_DEFAULT_FRESH_ALIASES",
                True,
            ),
            category_path=_optional_input_path("ERP_BILLING_CATEGORY_FILE"),
            use_default_categories=_bool_env(
                "ERP_BILLING_USE_DEFAULT_CATEGORIES",
                True,
            ),
        )

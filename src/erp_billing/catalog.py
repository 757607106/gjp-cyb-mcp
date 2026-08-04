"""商品目录：管理 ERP API 行、可选只读启动目录和别名映射。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from collections import Counter
from pathlib import Path
from typing import Any

from gjp_common.errors import DomainError
from .config import ErpBillingSettings
from .models import Product


DEFAULT_FRESH_ALIASES = {
    # 薯类
    "土豆": "马铃薯",
    "洋芋": "马铃薯",
    "洋山芋": "马铃薯",
    "薯仔": "马铃薯",
    # 茄果
    "西红柿": "番茄",
    "圣女果": "小番茄",
    # 瓜类
    "胡瓜": "黄瓜",
    "青瓜": "黄瓜",
    "番瓜": "南瓜",
    "倭瓜": "南瓜",
    "金瓜": "南瓜",
    # 豆苗
    "碗豆尖": "豌豆尖",
    # 甘蓝类
    "包菜": "卷心菜",
    "洋白菜": "卷心菜",
    "莲花白": "卷心菜",
    "包心菜": "卷心菜",
    "花菜": "花椰菜",
    "菜花": "花椰菜",
    # 根茎
    "茨菇": "慈菇",
    # 猪副
    "肚子": "猪肚",
}

# 品类词兜底：泛称词 → 部位子串关键词，覆盖上下位关系（牛肉 ⊃ 牛腱子），
# 仅供 category_contains 推荐匹配，不进入同义词组（等同关系）。
DEFAULT_FRESH_CATEGORIES = {
    "牛肉": ["牛腱", "牛腩", "牛里脊", "牛腿", "牛筋", "牛肚", "牛舌", "牛尾", "牛杂", "牛皮", "牛肉"],
    "猪肉": ["排骨", "五花", "猪蹄", "猪肝", "猪肚", "瘦肉", "夹心", "猪肉"],
    "鸡肉": ["鸡腿", "鸡翅", "鸡胸", "鸡爪", "鸡架", "鸡杂", "鸡肉"],
    "羊肉": ["羊腿", "羊排", "羊腩", "羊肉"],
    "鱼": ["草鱼", "鲤鱼", "鳙鱼", "黑鱼", "鳜鱼", "鲅鱼", "鲳鱼", "鱿鱼", "墨鱼", "章鱼"],
}

_DROP_CHARS = re.compile(r"[\s\-_./\\()（）【】\[\]{}<>《》,，.;；:：'\"“”]+")


class ProductCatalog:
    def __init__(
        self,
        products: list[Product],
        aliases: dict[str, str],
        categories: dict[str, list[str]] | None = None,
    ):
        self.products = products
        self.aliases = aliases
        self.categories = dict(categories or {})
        self.by_id = {item.product_id: item for item in products}
        self._equivalents, self._canonical_names = _build_alias_groups(
            _merge_product_aliases(products, aliases),
        )
        self._category_keywords = _build_category_keywords(self.categories)

    @classmethod
    def from_settings(cls, settings: ErpBillingSettings) -> "ProductCatalog":
        products = (
            _load_products(settings.product_catalog_path)
            if settings.product_catalog_path
            else []
        )
        return cls(
            products=products,
            aliases=_configured_aliases(settings),
            categories=_configured_categories(settings),
        )

    @classmethod
    def from_product_rows(
        cls,
        rows: Iterable[dict[str, Any]],
        aliases: dict[str, str],
        categories: dict[str, list[str]] | None = None,
    ) -> "ProductCatalog":
        """从 ERP API 返回行构建当前会话内存目录，不写本地文件。"""
        products = [Product.from_mapping(row) for row in rows]
        products = [
            item
            for item in products
            if item.name or item.code or item.barcode
        ]
        if not products:
            raise DomainError(
                "erp_live_product_empty",
                "当前账号没有返回可用于开单的商品",
            )
        return cls(products=products, aliases=dict(aliases), categories=dict(categories or {}))

    def require_product(self, product_id: str) -> Product:
        product = self.by_id.get(product_id)
        if product is None:
            raise DomainError("erp_product_not_found", "商品不存在：%s" % product_id)
        return product

    def equivalent_names(self, value: str) -> tuple[str, ...]:
        """返回名称所在同义词组，支持别名之间的双向精确匹配。"""
        normalized = normalize_name(value)
        return self._equivalents.get(normalized, ())

    def canonical_name(self, value: str) -> str:
        """返回同义词组的标准名；无别名配置时保留原始名称。"""
        normalized = normalize_name(value)
        return self._canonical_names.get(normalized, value.strip())

    def category_terms(self, value: str) -> set[str]:
        """返回泛称词对应的部位关键词集（normalize 后），无命中返回空集。

        用于 category_contains 推荐匹配：泛称词（如"牛肉"）扩展为部位子串
        关键词，对商品名做包含探测，覆盖上下位关系。
        """
        return self._category_keywords.get(normalize_name(value), set())


def _configured_aliases(settings: ErpBillingSettings) -> dict[str, str]:
    """按默认配置在前、外部同键覆盖的优先级加载别名。"""
    aliases: dict[str, str] = {}
    if settings.use_default_fresh_aliases:
        aliases.update(DEFAULT_FRESH_ALIASES)
    if settings.alias_path is not None:
        aliases.update(_load_aliases(settings.alias_path))
    return aliases


def _configured_categories(settings: ErpBillingSettings) -> dict[str, list[str]]:
    """按默认配置在前、外部同键覆盖的优先级加载品类词。"""
    categories: dict[str, list[str]] = {}
    if settings.use_default_categories:
        categories.update(DEFAULT_FRESH_CATEGORIES)
    if settings.category_path is not None:
        categories.update(_load_categories(settings.category_path))
    return categories


def normalize_live_product_rows(
    rows: list[Any],
    leaf_only: bool = True,
) -> list[dict[str, Any]]:
    """把 ERP 商品接口行转换为稳定目录格式，并过滤分组、停用和删除记录。"""
    products: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        product = Product.from_mapping(row)
        if not product.product_id or product.product_id == "00000" or not product.name:
            continue
        deleted = str(
            row.get("pdeleted") or row.get("ptypeid_pdeleted") or "0",
        )
        if deleted not in {"", "0"}:
            continue
        stopped = str(
            row.get("isstop") or row.get("ptypeid_isstop") or "0",
        )
        if stopped not in {"", "0"}:
            continue
        if "status" in row and _int_value(row.get("status")) != 1:
            continue
        if leaf_only and _int_value(row.get("sonnum") or row.get("ptypeid_sonnum")) != 0:
            continue
        if product.product_id in seen:
            continue
        seen.add(product.product_id)
        products.append(product.to_payload())
    products.sort(key=lambda item: (str(item.get("name") or ""), str(item.get("code") or "")))
    return products


def _load_products(path: Path) -> list[Product]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DomainError("erp_product_catalog_not_found", "商品目录不存在：%s" % path) from exc
    except json.JSONDecodeError as exc:
        raise DomainError("erp_product_catalog_invalid", "商品目录不是合法 JSON：%s" % path) from exc
    rows = _extract_rows(parsed)
    products = [Product.from_mapping(item) for item in rows if isinstance(item, dict)]
    products = [item for item in products if item.name or item.code or item.barcode]
    if not products:
        raise DomainError("erp_product_catalog_invalid", "商品目录中没有可用商品：%s" % path)
    return products


def _extract_rows(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    for key in ("products", "items", "data", "rows", "result"):
        rows = value.get(key)
        if isinstance(rows, list):
            return rows
        if isinstance(rows, dict):
            nested = _extract_rows(rows)
            if nested:
                return nested
    return []


def _load_aliases(path: Path) -> dict[str, str]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DomainError("erp_alias_file_not_found", "别名文件不存在：%s" % path) from exc
    except json.JSONDecodeError as exc:
        raise DomainError("erp_alias_file_invalid", "别名文件不是合法 JSON：%s" % path) from exc
    if isinstance(parsed, dict):
        source = parsed.get("aliases", parsed)
        if isinstance(source, dict):
            return {
                str(key).strip(): str(value).strip()
                for key, value in source.items()
                if str(key).strip() and str(value).strip()
            }
        if isinstance(source, list):
            return _alias_rows(source)
    if isinstance(parsed, list):
        return _alias_rows(parsed)
    raise DomainError("erp_alias_file_invalid", "别名文件必须是对象或数组：%s" % path)


def _load_categories(path: Path) -> dict[str, list[str]]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DomainError(
            "erp_category_file_not_found",
            "品类词文件不存在：%s" % path,
        ) from exc
    except json.JSONDecodeError as exc:
        raise DomainError(
            "erp_category_file_invalid",
            "品类词文件不是合法 JSON：%s" % path,
        ) from exc
    if not isinstance(parsed, dict):
        raise DomainError(
            "erp_category_file_invalid",
            "品类词文件必须是对象：%s" % path,
        )
    categories: dict[str, list[str]] = {}
    for key, value in parsed.items():
        category = str(key).strip()
        if not category or not isinstance(value, list):
            continue
        terms = [str(item).strip() for item in value if str(item).strip()]
        if terms:
            categories[category] = terms
    return categories


def _build_category_keywords(
    categories: dict[str, list[str]],
) -> dict[str, set[str]]:
    """把 {泛称词: [关键词]} 转为 {normalize(泛称词): {normalize(关键词)}}。"""
    result: dict[str, set[str]] = {}
    for category, terms in categories.items():
        key = normalize_name(category)
        if not key:
            continue
        terms_set = {normalize_name(term) for term in terms if normalize_name(term)}
        if terms_set:
            result[key] = terms_set
    return result


def _alias_rows(rows: list[Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        alias = str(row.get("alias") or row.get("别名") or "").strip()
        target = str(
            row.get("canonical")
            or row.get("target")
            or row.get("productName")
            or row.get("商品全名")
            or row.get("productId")
            or "",
        ).strip()
        if alias and target:
            aliases[alias] = target
    return aliases


def _int_value(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def normalize_name(value: str) -> str:
    """统一名称比较规则，不改变最终写入 ERP 的真实商品名称。"""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _DROP_CHARS.sub("", normalized)


def _merge_product_aliases(
    products: list[Product],
    aliases: dict[str, str],
) -> dict[str, str]:
    """合并外部别名表和商品自带 aliases，外部配置优先。"""
    merged = dict(aliases)
    for product in products:
        name = product.name.strip()
        if not name:
            continue
        for alias in product.aliases:
            alias_str = alias.strip()
            if alias_str and alias_str not in merged:
                merged[alias_str] = name
    return merged


def _build_alias_groups(
    aliases: dict[str, str],
) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    """把 alias→canonical 关系构造成完整无向同义词组。"""
    adjacency: dict[str, set[str]] = {}
    labels: dict[str, str] = {}
    target_counts: Counter[str] = Counter()
    target_order: dict[str, int] = {}

    for alias, target in aliases.items():
        alias_name = alias.strip()
        target_name = target.strip()
        alias_key = normalize_name(alias_name)
        target_key = normalize_name(target_name)
        if not alias_key or not target_key:
            continue
        labels.setdefault(alias_key, alias_name)
        labels.setdefault(target_key, target_name)
        adjacency.setdefault(alias_key, set()).add(target_key)
        adjacency.setdefault(target_key, set()).add(alias_key)
        target_counts[target_key] += 1
        target_order.setdefault(target_key, len(target_order))

    equivalents: dict[str, tuple[str, ...]] = {}
    canonical_names: dict[str, str] = {}
    visited: set[str] = set()
    for start in adjacency:
        if start in visited:
            continue
        pending = [start]
        component: list[str] = []
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            component.append(current)
            pending.extend(adjacency.get(current, ()))

        component_set = set(component)
        canonical_key = max(
            (
                key
                for key in target_order
                if key in component_set
            ),
            key=lambda key: (target_counts[key], -target_order[key]),
            default=component[0],
        )
        names = tuple(
            labels[key]
            for key in sorted(component, key=lambda key: labels[key])
        )
        canonical = labels[canonical_key]
        for key in component:
            equivalents[key] = names
            canonical_names[key] = canonical
    return equivalents, canonical_names

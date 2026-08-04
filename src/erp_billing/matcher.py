"""商品确定性匹配：精确名称和同义词组自动命中，模糊结果仅用于推荐。"""

from __future__ import annotations

from difflib import SequenceMatcher

from .catalog import ProductCatalog, normalize_name
from .config import ErpBillingSettings
from .models import DraftLine, MatchCandidate, OrderLine, Product


_DIRECT_MATCH_TYPES = frozenset(
    {
        "product_id_exact",
        "code_exact",
        "barcode_exact",
        "name_exact",
        "product_alias_exact",
        "customer_code_exact",
    },
)

# 口味修饰后缀：仅当紧跟在关键词后面时才视为口味描述（如"番茄味"中
# "番茄"仅是口味而非商品本身），用于过滤子串匹配的语义误命中。
_FLAVOR_SUFFIXES = frozenset({"味"})

# 加工食品类型词：出现在商品名任意位置即表明产品类型与生鲜原料不同
# （如"牛肉面"是面食而非生鲜肉，"番茄酱"是调味品而非蔬菜）。
# 单字"面"在食品语境中几乎总是面食；不收录"干""丝""片"等形态词，
# 因为"牛肉干""牛肉丝""牛肉片"仍是该生鲜品类的加工形态。
_PROCESSED_TYPE_WORDS = frozenset(
    {
        # 面食
        "面",
        # 零食/膨化
        "薯片",
        "脆片",
        "锅巴",
        "膨化",
        # 饼干糕点
        "饼干",
        "威化",
        "曲奇",
        # 调味品
        "酱",
        "调料",
        "调味",
        # 糖果
        "糖果",
        "果冻",
        "布丁",
        # 饮品
        "饮料",
        "果汁",
        "汽水",
        # 茶饮
        "奶茶",
        "果茶",
        # 罐头
        "罐头",
        # 酒类
        "酒",
        # 加工肉制品
        "肠",
        "丸",
        "饺",
    },
)


def _is_likely_mismatch(keyword: str, product_name: str) -> bool:
    """检测生鲜关键词在商品名中的子串匹配是否为语义误命中。

    两条判定规则：
    1. 关键词紧跟口味修饰后缀（如"番茄味"），说明关键词仅是口味描述；
    2. 商品名包含不属于关键词本身的加工食品类型词（如"牛肉"→"牛肉面"
       中的"面"），说明产品类型与生鲜原料不同。

    满足任一条件即返回 True，使该子串匹配被跳过并降级到模糊匹配。
    """
    if any(keyword + suffix in product_name for suffix in _FLAVOR_SUFFIXES):
        return True
    return any(
        word in product_name and word not in keyword
        for word in _PROCESSED_TYPE_WORDS
    )


class ProductMatcher:
    def __init__(self, catalog: ProductCatalog, settings: ErpBillingSettings):
        self.catalog = catalog
        self.settings = settings

    def match_line(self, line: OrderLine, limit: int = 5) -> DraftLine:
        candidates = self._candidates(line.requested_name)
        selected, recommendations = self._resolve_candidates(candidates)
        if selected is not None:
            return DraftLine(
                order_line=line,
                status="matched",
                product=selected.product,
                match_type=selected.match_type,
                message=selected.reason,
            )
        if recommendations:
            return DraftLine(
                order_line=line,
                status="ambiguous",
                candidates=recommendations[:limit],
                message="存在多个或非精确匹配商品，请从推荐列表确认",
            )
        return DraftLine(
            order_line=line,
            status="unmatched",
            message="没有可推荐的系统商品",
        )

    def search(self, keyword: str, limit: int = 10) -> list[MatchCandidate]:
        return self._candidates(keyword)[:limit]

    def resolve(
        self,
        keyword: str,
        limit: int = 10,
    ) -> tuple[MatchCandidate | None, list[MatchCandidate]]:
        """返回唯一确定性商品；否则返回供前端确认的推荐候选。"""
        candidates = self._candidates(keyword)
        selected, recommendations = self._resolve_candidates(candidates)
        return selected, recommendations[:limit]

    @staticmethod
    def _resolve_candidates(
        candidates: list[MatchCandidate],
    ) -> tuple[MatchCandidate | None, list[MatchCandidate]]:
        direct = [
            candidate
            for candidate in candidates
            if candidate.match_type in _DIRECT_MATCH_TYPES
        ]
        if direct:
            return (direct[0], []) if len(direct) == 1 else (None, direct)
        aliases = [
            candidate
            for candidate in candidates
            if candidate.match_type == "alias_exact"
        ]
        if aliases:
            return (aliases[0], []) if len(aliases) == 1 else (None, aliases)
        return None, candidates

    def _candidates(self, keyword: str) -> list[MatchCandidate]:
        normalized_keyword = normalize_name(keyword)
        if not normalized_keyword:
            return []
        equivalents = {
            normalize_name(item)
            for item in self.catalog.equivalent_names(keyword)
            if normalize_name(item)
        }
        category_terms = self.catalog.category_terms(normalized_keyword)
        scored = [
            self._score_product(
                normalized_keyword,
                equivalents,
                category_terms,
                product,
            )
            for product in self.catalog.products
        ]
        candidates = [
            candidate
            for candidate in scored
            if candidate.match_type in _DIRECT_MATCH_TYPES
            or candidate.match_type == "alias_exact"
            or candidate.score >= self.settings.recommendation_score
        ]
        candidates.sort(
            key=lambda candidate: (
                -candidate.score,
                candidate.product.name,
                candidate.product.code,
                candidate.product.product_id,
            ),
        )
        return candidates

    def _score_product(
        self,
        normalized_keyword: str,
        equivalents: set[str],
        category_terms: set[str],
        product: Product,
    ) -> MatchCandidate:
        identifiers = [
            ("product_id_exact", "商品 ID 精确匹配", product.product_id),
            ("code_exact", "商品编号精确匹配", product.code),
            ("barcode_exact", "条码精确匹配", product.barcode),
            ("name_exact", "商品全名精确匹配", product.name),
            *[
                ("product_alias_exact", "商品自带别名精确匹配", item)
                for item in product.aliases
            ],
            *[
                ("customer_code_exact", "客户货号精确匹配", item)
                for item in product.customer_codes
            ],
        ]
        for match_type, reason, value in identifiers:
            if value and normalize_name(value) == normalized_keyword:
                return MatchCandidate(product, 1.0, match_type, reason)

        product_identifiers = {
            normalize_name(item)
            for item in (
                product.product_id,
                product.code,
                product.barcode,
                product.name,
                *product.aliases,
                *product.customer_codes,
            )
            if item
        }
        if equivalents and equivalents.intersection(product_identifiers):
            return MatchCandidate(
                product,
                0.98,
                "alias_exact",
                "商品同义词组精确匹配",
            )

        product_name = normalize_name(product.name)
        # 同义词组包含匹配：别名词作为商品名子串时命中（如"山药蛋"→"袋装土豆"），
        # 弥补 alias_exact 只做精确相等、对复合命名商品失效的缺口，仅供推荐。
        # 当商品名包含与别名词无关的加工食品类型词时跳过，避免语义误命中
        # （如"番茄"→"番茄味薯片"中番茄仅是口味而非商品本身）。
        if equivalents and product_name:
            for equiv in equivalents:
                if len(equiv) >= 2 and equiv in product_name:
                    if _is_likely_mismatch(equiv, product_name):
                        continue
                    return MatchCandidate(
                        product,
                        0.92,
                        "alias_contains",
                        "同义词组包含匹配，仅供推荐",
                    )
        # 品类词包含匹配：泛称词（如"牛肉"）扩展为部位关键词，对商品名做子串探测，
        # 覆盖上下位关系（牛肉 ⊃ 牛腱子），只推荐不自动命中。
        # 同样过滤加工食品类型词导致的语义误命中（如"牛肉"→"牛肉面"）。
        if category_terms and product_name:
            for term in category_terms:
                if len(term) >= 2 and term in product_name:
                    if _is_likely_mismatch(term, product_name):
                        continue
                    return MatchCandidate(
                        product,
                        0.88,
                        "category_contains",
                        "品类词包含匹配，仅供推荐",
                    )
        if product_name and len(normalized_keyword) >= 2:
            forward = normalized_keyword in product_name
            reverse = len(product_name) >= 2 and product_name in normalized_keyword
            # 正向子串匹配时过滤加工食品类型词导致的语义误命中
            if forward and _is_likely_mismatch(normalized_keyword, product_name):
                forward = False
            if forward or reverse:
                return MatchCandidate(
                    product,
                    0.86,
                    "contains",
                    "商品名称包含匹配，仅供推荐",
                )

        alias_score = max(
            (
                SequenceMatcher(
                    None,
                    normalized_keyword,
                    normalize_name(alias),
                ).ratio()
                for alias in product.aliases
            ),
            default=0.0,
        )
        name_score = (
            SequenceMatcher(None, normalized_keyword, product_name).ratio()
            if product_name
            else 0.0
        )
        code_score = (
            SequenceMatcher(
                None,
                normalized_keyword,
                normalize_name(product.code),
            ).ratio()
            if product.code
            else 0.0
        )
        fuzzy_score = max(name_score, code_score, alias_score)
        # 子串匹配被过滤后，模糊匹配仍可能因共有字符获得高分。
        # 对含加工食品类型词的商品施加惩罚，使其降级到推荐阈值以下。
        if product_name and _is_likely_mismatch(normalized_keyword, product_name):
            fuzzy_score *= 0.5
        return MatchCandidate(
            product,
            fuzzy_score,
            "fuzzy",
            "模糊相似度匹配，仅供推荐",
        )

"""ERP 开单领域服务：解析完整订单文本并匹配当前商品目录。"""

from __future__ import annotations

from copy import deepcopy
import json
import re
from dataclasses import replace
from typing import Any
from uuid import uuid4

from gjp_common.errors import DomainError
from .catalog import ProductCatalog
from .config import ErpBillingSettings
from .matcher import ProductMatcher
from .models import BillingDraft, DraftLine, OrderLine
from .ports import MatchEvent, MatchEventLogger


_SPLIT_RE = re.compile(r"[\n\r,，;；]+")
_QUANTITY_PATTERN = r"\d+(?:\.\d+)?|[一二两三四五六七八九十百]+"
_UNIT_PATTERN = r"公斤|千克|毫升|kg|ml|斤|克|g|l|L|升|吨|t|瓶|件|箱|袋|个|颗|根|把|盒|包|只|份|条|听|提|板|盘|筐|桶|卷|打|扎"
_EACH_RE = re.compile(
    rf"^(?P<left>.+?)和(?P<right>.+?)各\s*"
    rf"(?P<qty>{_QUANTITY_PATTERN})\s*(?P<unit>[^\d\s]+)?$",
)
_QUANTITY_FIRST_RE = re.compile(
    rf"^(?:请\s*)?(?:给我\s*)?(?:(?:来|要|买|加|拿)\s*)?"
    rf"(?P<qty>{_QUANTITY_PATTERN})\s*"
    rf"(?P<unit>{_UNIT_PATTERN})\s*(?P<name>.+)$",
)
_LINE_RE = re.compile(
    rf"^(?P<name>.+?)(?P<qty>{_QUANTITY_PATTERN})\s*"
    rf"(?P<unit>{_UNIT_PATTERN})?$",
)
_REQUEST_PREFIX_RE = re.compile(
    r"^(?:请\s*)?(?:给我\s*)?(?:(?:来|要|买|加|拿)\s*)?",
)
_CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

# "各"模式（无"和"连接词）：苹果荔枝各5斤 → ["苹果","荔枝"] 各5斤
_EACH_LIST_RE = re.compile(
    rf"^(?P<names>.+?)各\s*"
    rf"(?P<qty>{_QUANTITY_PATTERN})\s*(?P<unit>[^\d\s]+)?$",
)

# 动词前缀检测：仅含前缀时跳过空格分割（保护"来5斤 洋芋"等数量前置模式）
_VERB_PREFIX_ONLY_RE = re.compile(
    r"^(?:请\s*)?(?:给我\s*)?(?:(?:来|要|买|加|拿)\s*)?$",
)

# 数字+单位+空格：鸡蛋21个 牛肉10斤 → 在"21个 "后插入换行
_DIGIT_UNIT_SPACE_RE = re.compile(
    rf"\d+(?:\.\d+)?(?:{_UNIT_PATTERN})\s+",
)


def _insert_inter_item_separators(text: str) -> str:
    """在"数字+单位+空格"边界插入换行符，跳过"动词前缀+数字+单位"的数量前置模式。"""
    segments: list[str] = []
    last_end = 0
    for match in _DIGIT_UNIT_SPACE_RE.finditer(text):
        prefix = text[: match.start()]
        if _VERB_PREFIX_ONLY_RE.match(prefix):
            continue
        segments.append(text[last_end : match.end()])
        last_end = match.end()
    segments.append(text[last_end:])
    return "\n".join(segments)


class ErpBillingSession:
    """持有租户隔离商品目录；每次开单都由完整文本重新生成 JSON。"""

    def __init__(
        self,
        settings: ErpBillingSettings,
        catalog: ProductCatalog,
        match_logger: MatchEventLogger | None = None,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.matcher = ProductMatcher(catalog, settings)
        self._match_logger = match_logger
        self._prepared_sales_orders: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self._submission_results: dict[str, dict[str, Any]] = {}

    @classmethod
    def from_settings(
        cls,
        settings: ErpBillingSettings,
        allow_missing_catalog: bool = False,
        match_logger: MatchEventLogger | None = None,
    ) -> "ErpBillingSession":
        try:
            catalog = ProductCatalog.from_settings(settings)
        except DomainError as exc:
            if not allow_missing_catalog or exc.code != "erp_product_catalog_not_found":
                raise
            settings = replace(settings, product_catalog_path=None)
            catalog = ProductCatalog.from_settings(settings)
        return cls(
            settings=settings,
            catalog=catalog,
            match_logger=match_logger,
        )

    def search_products(self, keywords: list[str], limit: int = 10) -> dict[str, Any]:
        """批量查询当前 ERP 商品；模糊结果仅作为推荐返回。

        Args:
            keywords: 一个或多个商品名称、编号或条码。
            limit: 每个关键词最多返回的推荐候选数量。
        """
        try:
            if not keywords:
                raise DomainError(
                    "erp_product_query_empty",
                    "请至少输入一个商品名称、编号或条码",
                )
            self._require_catalog()
            effective_limit = max(1, min(int(limit or 10), 20))
            results = [
                self._search_single(keyword, effective_limit)
                for keyword in keywords
            ]
            return self._ok(results=results)
        except (DomainError, TypeError, ValueError) as exc:
            error = (
                exc
                if isinstance(exc, DomainError)
                else DomainError("erp_product_limit_invalid", "limit 必须是整数")
            )
            return self._error(error)

    def _search_single(self, keyword: str, limit: int) -> dict[str, Any]:
        """查询单个关键词的确定性匹配结果和推荐候选。"""
        query = keyword.strip()
        if not query:
            raise DomainError(
                "erp_product_query_empty",
                "商品名称、编号或条码不能为空",
            )
        selected, recommendations = self.matcher.resolve(
            query,
            limit=limit,
        )
        status = (
            "matched"
            if selected is not None
            else "ambiguous"
            if recommendations
            else "unmatched"
        )
        return {
            "query": query,
            "status": status,
            "product": (
                selected.product.core_fields()
                if selected is not None
                else None
            ),
            "recommendations": [
                candidate.product.core_fields()
                for candidate in recommendations
            ],
        }

    def create_draft_from_text(
        self,
        text: str,
        *,
        source: str = "text",
        customer: str = "",
        warehouse: str = "",
        confirmed_products: list[dict[str, str]]
        | dict[str, str]
        | str
        | None = None,
    ) -> BillingDraft:
        """从完整订单文本重建草稿，并校验前端确认的候选商品。

        confirmed_products 接受三种格式，内部统一归一为 {line_id: product_id}：
        - list[dict]：[{"line_id": "L001", "product_id": "P001"}]（推荐格式）
        - dict[str, str]：{"L001": "P001"}（向后兼容）
        - str：JSON 文本（容错，LLM 可能误传字符串）
        """
        self._require_catalog()
        normalized_source = source.strip().casefold()
        if normalized_source not in {"text", "voice", "image"}:
            raise DomainError(
                "erp_order_source_invalid",
                "source 必须是 text、voice 或 image",
            )
        lines = parse_order_text(text)
        if not lines:
            raise DomainError("erp_order_text_empty", "未识别到商品行")

        confirmations = _normalize_confirmed_products(confirmed_products)
        valid_line_ids = {line.line_id for line in lines}
        unknown_line_ids = sorted(set(confirmations).difference(valid_line_ids))
        if unknown_line_ids:
            raise DomainError(
                "erp_confirmed_line_not_found",
                "确认商品对应的订单行不存在：%s；有效行 ID 为 %s"
                % ("、".join(unknown_line_ids), "、".join(sorted(valid_line_ids))),
            )

        draft_lines: list[DraftLine] = []
        for line in lines:
            matched_line = self.matcher.match_line(line)
            if line.line_id in confirmations:
                matched_line = self._confirm_recommendation(
                    matched_line,
                    confirmations[line.line_id],
                )
            draft_lines.append(matched_line)

        self._record_match_events(normalized_source, draft_lines)

        return BillingDraft(
            source=normalized_source,
            source_text=text,
            lines=draft_lines,
            customer=customer.strip(),
            warehouse=warehouse.strip(),
        )

    def _confirm_recommendation(
        self,
        line: DraftLine,
        product_id: str,
    ) -> DraftLine:
        product = self.catalog.require_product(product_id)
        # unmatched 且无候选时，允许从全目录手动指定商品
        if line.status == "unmatched" and not line.candidates:
            return DraftLine(
                order_line=line.order_line,
                status="matched",
                product=product,
                match_type="user_confirmed",
                message="用户手动指定商品",
            )
        allowed_ids = {
            candidate.product.product_id
            for candidate in line.candidates
        }
        if line.product is not None:
            allowed_ids.add(line.product.product_id)
        if product_id not in allowed_ids:
            raise DomainError(
                "erp_confirmed_product_not_recommended",
                "商品 %s 不属于第%d行的匹配结果"
                % (product_id, line.order_line.line_no),
            )
        return DraftLine(
            order_line=line.order_line,
            status="matched",
            product=product,
            match_type="user_confirmed",
            message="用户已确认推荐商品",
        )

    def _record_match_events(
        self,
        source: str,
        lines: list[DraftLine],
    ) -> None:
        """记录已确认匹配行，供离线挖掘同义词候选；不记录推荐和未匹配行。

        match_type 区分自动命中（*_exact/alias_exact）与用户确认
        （user_confirmed），后者是别名缺失最有价值的证据。匹配主流程
        不依赖本方法的写入结果。
        """
        logger = self._match_logger
        if logger is None:
            return
        for line in lines:
            if line.status != "matched" or line.product is None:
                continue
            logger.record(
                MatchEvent(
                    source=source,
                    requested_name=line.order_line.requested_name,
                    product_id=line.product.product_id,
                    product_name=line.product.name,
                    match_type=line.match_type,
                ),
            )

    def _require_catalog(self) -> None:
        if not self.catalog.products:
            raise DomainError(
                "erp_product_catalog_empty",
                "当前没有商品目录，请先调用 sync_products",
            )

    def replace_products(self, rows: tuple[dict[str, Any], ...]) -> None:
        """使用 ERP API 结果替换当前会话目录，不产生运行时文件。"""
        self.catalog = ProductCatalog.from_product_rows(
            rows,
            self.catalog.aliases,
            self.catalog.categories,
        )
        self.matcher = ProductMatcher(self.catalog, self.settings)

    def store_prepared_sales_order(
        self,
        payload: dict[str, Any],
        summary: dict[str, Any],
    ) -> str:
        """保存不可变销售单预览，供明确确认后的提交工具引用。"""
        preview_id = "sales-preview-" + uuid4().hex
        self._prepared_sales_orders[preview_id] = (
            deepcopy(payload),
            deepcopy(summary),
        )
        while len(self._prepared_sales_orders) > 20:
            self._prepared_sales_orders.pop(next(iter(self._prepared_sales_orders)))
        return preview_id

    def require_prepared_sales_order(
        self,
        preview_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """返回销售单预览副本，阻止提交阶段篡改已确认内容。"""
        stored = self._prepared_sales_orders.get(preview_id.strip())
        if stored is None:
            raise DomainError(
                "erp_sales_order_preview_not_found",
                "销售单预览不存在或已失效，请重新生成预览",
            )
        return deepcopy(stored[0]), deepcopy(stored[1])

    def submission_result(self, idempotency_key: str) -> dict[str, Any] | None:
        """查询当前会话已成功提交的幂等结果。"""
        result = self._submission_results.get(idempotency_key.strip())
        return deepcopy(result) if result is not None else None

    def remember_submission(
        self,
        idempotency_key: str,
        result: dict[str, Any],
    ) -> None:
        """记录当前会话的成功提交结果，避免 Agent 重试造成重复单据。"""
        self._submission_results[idempotency_key.strip()] = deepcopy(result)
        while len(self._submission_results) > 50:
            self._submission_results.pop(next(iter(self._submission_results)))

    @staticmethod
    def _ok(**payload: Any) -> dict[str, Any]:
        return {"ok": True, **payload}

    @staticmethod
    def _error(exc: DomainError) -> dict[str, Any]:
        return {"ok": False, "error": {"code": exc.code, "message": exc.message}}


def parse_order_text(text: str) -> list[OrderLine]:
    text = _insert_inter_item_separators(text)
    lines: list[OrderLine] = []
    for part in _SPLIT_RE.split(text):
        raw = part.strip()
        if not raw:
            continue
        lines.extend(_parse_part(raw))
    return [
        OrderLine(
            line_no=index,
            raw_text=line.raw_text,
            requested_name=line.requested_name,
            quantity=line.quantity,
            unit=line.unit,
            note=line.note,
        )
        for index, line in enumerate(lines, start=1)
    ]


def _normalize_confirmed_products(
    raw: list[dict[str, str]] | dict[str, str] | str | None,
) -> dict[str, str]:
    """将各种 confirmed_products 输入格式统一为 {line_id: product_id} 字典。

    支持 list[dict]、dict[str, str] 和 JSON 字符串三种输入；
    list 格式每个元素须包含 line_id 和 product_id（或 ptypeid）键。
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return {}
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise DomainError(
                "erp_confirmed_products_invalid",
                "confirmed_products 不是有效的 JSON 对象或数组",
            ) from exc
    if isinstance(raw, list):
        result: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            line_id = str(
                item.get("line_id") or item.get("lineId") or ""
            ).strip()
            product_id = str(
                item.get("product_id")
                or item.get("productId")
                or item.get("ptypeid")
                or ""
            ).strip()
            if not line_id or not product_id:
                raise DomainError(
                    "erp_confirmed_product_invalid",
                    "confirmed_products 的 line_id 和 product_id 不能为空",
                )
            result[line_id] = product_id
        return result
    if isinstance(raw, dict):
        result = {}
        for raw_line_id, raw_product_id in raw.items():
            line_id = str(raw_line_id).strip()
            product_id = str(raw_product_id).strip()
            if not line_id or not product_id:
                raise DomainError(
                    "erp_confirmed_product_invalid",
                    "confirmed_products 的 line_id 和 product_id 不能为空",
                )
            result[line_id] = product_id
        return result
    raise DomainError(
        "erp_confirmed_products_invalid",
        "confirmed_products 必须是 JSON 对象或数组",
    )


def _parse_part(raw: str) -> list[OrderLine]:
    each = _EACH_RE.match(raw)
    if each:
        quantity = _parse_quantity(each.group("qty"))
        unit = (each.group("unit") or "").strip()
        return [
            OrderLine(
                0,
                raw,
                _clean_requested_name(each.group("left")),
                quantity,
                unit,
            ),
            OrderLine(
                0,
                raw,
                _clean_requested_name(each.group("right")),
                quantity,
                unit,
            ),
        ]
    each_list = _EACH_LIST_RE.match(raw)
    if each_list:
        quantity = _parse_quantity(each_list.group("qty"))
        unit = (each_list.group("unit") or "").strip()
        names_str = re.sub(r"\s+", "", each_list.group("names"))
        if len(names_str) >= 4 and len(names_str) % 2 == 0:
            names = [names_str[i : i + 2] for i in range(0, len(names_str), 2)]
        else:
            names = [names_str]
        return [
            OrderLine(0, raw, _clean_requested_name(name), quantity, unit)
            for name in names
        ]
    quantity_first = _QUANTITY_FIRST_RE.match(raw)
    if quantity_first:
        return [
            OrderLine(
                0,
                raw,
                _clean_requested_name(quantity_first.group("name")),
                _parse_quantity(quantity_first.group("qty")),
                quantity_first.group("unit").strip(),
            ),
        ]
    matched = _LINE_RE.match(raw)
    if matched:
        return [
            OrderLine(
                0,
                raw,
                _clean_requested_name(matched.group("name")),
                _parse_quantity(matched.group("qty")),
                (matched.group("unit") or "").strip(),
            ),
        ]
    return [OrderLine(0, raw, _clean_requested_name(raw), 1, "")]


def _clean_requested_name(value: str) -> str:
    original = value.strip()
    cleaned = _REQUEST_PREFIX_RE.sub("", original, count=1).strip()
    return cleaned or original


def _parse_quantity(value: str) -> float:
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value)
    if value in _CHINESE_NUMBERS:
        return float(_CHINESE_NUMBERS[value])
    if value.startswith("十") and len(value) == 2:
        return float(10 + _CHINESE_NUMBERS.get(value[1], 0))
    if value.endswith("十") and len(value) == 2:
        return float(_CHINESE_NUMBERS.get(value[0], 0) * 10)
    if "十" in value and len(value) == 3:
        return float(
            _CHINESE_NUMBERS.get(value[0], 0) * 10
            + _CHINESE_NUMBERS.get(value[2], 0),
        )
    return 1.0

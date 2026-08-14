"""ERP 开单数据模型：系统商品、客户下单行、匹配候选和销售单草稿。"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first_present(value: dict[str, Any], *keys: str) -> Any:
    """返回首个非空字段，同时保留合法的数值 0。"""
    for key in keys:
        candidate = value.get(key)
        if candidate not in (None, ""):
            return candidate
    return None


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    code: str = ""
    unit: str = ""
    barcode: str = ""
    specification: str = ""
    aliases: tuple[str, ...] = ()
    customer_codes: tuple[str, ...] = ()
    price: Optional[float] = None
    purchase_price: Optional[float] = None
    stock: Optional[float] = None
    status: Optional[int] = None
    image_urls: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "Product":
        product_id = _clean_string(
            value.get("productId")
            or value.get("product_id")
            or value.get("ptypeid")
            or value.get("id")
        )
        name = _clean_string(
            value.get("name")
            or value.get("pfullname")
            or value.get("ptypeid_pfullname")
            or value.get("fullname")
            or value.get("商品全名")
            or value.get("商品名称")
        )
        code = _clean_string(
            value.get("code")
            or value.get("pusercode")
            or value.get("ptypeid_pusercode")
            or value.get("usercode")
            or value.get("商品编号")
        )
        barcode = _clean_string(
            value.get("barcode")
            or value.get("pbarcode")
            or value.get("basebarcode")
            or value.get("ptypeid_basebarcode")
            or value.get("ubarcode")
            or value.get("条码")
        )
        unit = _clean_string(
            value.get("unit")
            or value.get("uname")
            or value.get("unit1")
            or value.get("ptypeid_unit1")
            or value.get("单位")
        )
        aliases = _string_tuple(value.get("aliases") or value.get("别名") or value.get("alias"))
        customer_codes = _string_tuple(
            value.get("customerCodes")
            or value.get("customer_codes")
            or value.get("bcode")
            or value.get("对方商品编号")
            or value.get("multpusercode")
        )
        image_urls = _url_list(
            _first_present(
                value,
                "imageUrls",
                "image_urls",
                "ptypeid_imageurls",
                "图片",
            )
        )
        specification = _clean_string(
            _first_present(
                value,
                "specification",
                "spec",
                "pspec",
                "ptypeid_pspec",
                "规格型号",
            )
        )
        return cls(
            product_id=product_id or code or barcode or name,
            name=name,
            code=code,
            unit=unit,
            barcode=barcode,
            specification=specification,
            aliases=aliases,
            customer_codes=customer_codes,
            price=_optional_float(
                _first_present(
                    value,
                    "price",
                    "salesPrice",
                    "preprice1",
                    "recprice",
                    "单价",
                ),
            ),
            purchase_price=_optional_float(
                _first_present(
                    value,
                    "purchasePrice",
                    "purchase_price",
                    "purprice",
                    "preprice2",
                    "lastpurprice",
                    "采购价",
                ),
            ),
            stock=_optional_float(
                _first_present(
                    value,
                    "stock",
                    "stockQuantity",
                    "qty",
                    "库存",
                ),
            ),
            status=_optional_int(
                _first_present(
                    value,
                    "status",
                    "isstop",
                ),
            ),
            image_urls=image_urls,
            raw=dict(value),
        )

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "productId": self.product_id,
            "code": self.code,
            "name": self.name,
            "unit": self.unit,
            "barcode": self.barcode,
            "specification": self.specification,
            "aliases": list(self.aliases),
            "customerCodes": list(self.customer_codes),
            "price": self.price,
            "purchasePrice": self.purchase_price,
            "stock": self.stock,
            "status": self.status,
        }
        if self.image_urls:
            payload["imageUrls"] = list(self.image_urls)
        return payload

    def core_fields(self) -> dict[str, Any]:
        """返回工具输出统一使用的商品核心字段：product_id、product_name、unit。

        image_urls 仅在商品有图片时附带，无图片时不出现该键。
        """
        fields: dict[str, Any] = {
            "product_id": self.product_id,
            "product_name": self.name,
            "unit": self.unit,
        }
        if self.image_urls:
            fields["image_urls"] = list(self.image_urls)
        return fields

    def listing_fields(self) -> dict[str, Any]:
        """返回商品列表展示所需的完整业务字段集。

        不含 product_id 和 code 等系统标识，只返回用户关心的业务字段：
        商品名称、单位、规格型号、采购价、销售价、当前库存和状态。
        供 listProducts 和 syncProducts 的 sample 使用。数值类字段
        为空时不出现该键，避免输出大量 null。
        """
        fields: dict[str, Any] = {
            "product_name": self.name,
            "unit": self.unit,
        }
        if self.image_urls:
            fields["image_urls"] = list(self.image_urls)
        if self.specification:
            fields["specification"] = self.specification
        if self.purchase_price is not None:
            fields["purchase_price"] = self.purchase_price
        if self.price is not None:
            fields["sales_price"] = self.price
        if self.stock is not None:
            fields["stock_quantity"] = self.stock
        if self.status is not None:
            fields["status"] = self.status
        return fields


@dataclass(frozen=True)
class OrderLine:
    line_no: int
    raw_text: str
    requested_name: str
    quantity: float
    unit: str = ""
    note: str = ""

    @property
    def line_id(self) -> str:
        """返回本次完整订单文本中的稳定行标识。"""
        return "L%03d" % self.line_no


@dataclass(frozen=True)
class MatchCandidate:
    product: Product
    score: float
    match_type: str
    reason: str


@dataclass
class DraftLine:
    order_line: OrderLine
    status: str
    candidates: list[MatchCandidate] = field(default_factory=list)
    product: Optional[Product] = None
    match_type: str = ""
    message: str = ""


@dataclass
class BillingDraft:
    source: str
    source_text: str
    lines: list[DraftLine]
    customer: str = ""
    warehouse: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def status(self) -> str:
        return (
            "ready"
            if self.lines and all(line.status == "matched" for line in self.lines)
            else "needs_confirmation"
        )

    def billing_products_payload(self) -> dict[str, list[dict[str, Any]]]:
        """按确认、推荐和未匹配三类返回前端可直接取值的商品。"""
        confirmed_products: list[dict[str, Any]] = []
        recommended_products: list[dict[str, Any]] = []
        unmatched_products: list[dict[str, Any]] = []

        for line in self.lines:
            if line.status == "matched" and line.product is not None:
                confirmed_products.append(
                    _billing_product_fields(line.product, line.order_line),
                )
                continue

            if line.candidates:
                best_candidate, *similar_candidates = line.candidates
                recommended_products.append(
                    {
                        **_billing_product_fields(
                            best_candidate.product,
                            line.order_line,
                        ),
                        "similar_products": [
                            _billing_product_fields(
                                candidate.product,
                                line.order_line,
                            )
                            for candidate in similar_candidates
                        ],
                    },
                )
                continue

            unmatched_products.append(
                _billing_product_fields(None, line.order_line),
            )

        return {
            "confirmed_products": confirmed_products,
            "recommended_products": recommended_products,
            "unmatched_products": unmatched_products,
        }


def _billing_product_fields(
    product: Product | None,
    order_line: OrderLine,
) -> dict[str, Any]:
    """构建开单和推荐商品共用的最小字段集合，商品字段统一经由 core_fields。

    line_id 始终出现在输出中，使调用方无需猜测 confirmed_products 的键格式。
    """
    if product is not None:
        fields = dict(product.core_fields())
        if not fields["product_name"]:
            fields["product_name"] = order_line.requested_name
        if not fields["unit"]:
            fields["unit"] = order_line.unit
    else:
        fields = {
            "product_id": None,
            "product_name": order_line.requested_name,
            "unit": order_line.unit,
        }
    fields["line_id"] = order_line.line_id
    fields["quantity"] = order_line.quantity
    return fields


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [item.strip() for item in value.replace("，", ",").split(",")]
    elif isinstance(value, list):
        items = [_clean_string(item) for item in value]
    else:
        items = [_clean_string(value)]
    return tuple(item for item in items if item)


def _url_list(value: Any) -> tuple[str, ...]:
    """解析商品图片 URL 列表，保留原始顺序并去除空项。

    字符串视为单个 URL（URL 可能含逗号，不做逗号拆分）；列表逐项清洗。
    """
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    else:
        items = [str(value).strip()]
    return tuple(item for item in items if item)

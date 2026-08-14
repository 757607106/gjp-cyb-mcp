import asyncio
import json
from dataclasses import fields

import jsonschema
import pytest

from erp_billing.adapters import (
    JsonlMatchEventLogger,
    NullMatchEventLogger,
    UnavailableBillingApi,
    create_match_logger_from_env,
)
from erp_billing.catalog import normalize_live_product_rows
from erp_billing.config import ErpBillingSettings
from erp_billing.models import Product
from erp_billing.ports import (
    BillingProductSnapshot,
    BillingReferenceSnapshot,
    BillingSalesOrderDetailResult,
    BillingSalesOrderPageResult,
    BillingSalesOrderResult,
)
from erp_billing.session import ErpBillingSession, parse_order_text
from erp_billing.toolset import BILLING_MCP_TOOL_NAMES, BillingToolSet
from gjp_common.context import InvocationContext, InvocationContextStore


def _settings(
    tmp_path,
    *,
    catalog_path=None,
    alias_path=None,
    use_default_fresh_aliases=True,
    category_path=None,
    use_default_categories=True,
    recommendation_score=0.60,
):
    return ErpBillingSettings(
        product_catalog_path=catalog_path,
        alias_path=alias_path,
        recommendation_score=recommendation_score,
        use_default_fresh_aliases=use_default_fresh_aliases,
        category_path=category_path,
        use_default_categories=use_default_categories,
    )


def _write_catalog(tmp_path, products):
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps({"products": products}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _session(tmp_path, products, **settings_overrides):
    catalog_path = _write_catalog(tmp_path, products)
    return ErpBillingSession.from_settings(
        _settings(
            tmp_path,
            catalog_path=catalog_path,
            **settings_overrides,
        ),
    )


def _billing_toolset(session, api=None):
    context = InvocationContext(
        tenant_id="tenant-test",
        subject_id="user-test",
        account_id="billing-test",
        session_id="session-test",
        scopes=frozenset({"billing:read", "billing:write"}),
    )
    return BillingToolSet(
        session,
        api or UnavailableBillingApi(),
        InvocationContextStore(default=context),
    )


def _product_payload(result):
    """从完整销售单准备结果中提取商品匹配部分。"""
    return {
        "ok": result["ok"],
        "confirmed_products": result["confirmed_products"],
        "recommended_products": result["recommended_products"],
        "unmatched_products": result["unmatched_products"],
    }


def test_parse_order_text_splits_each_quantity_phrase():
    lines = parse_order_text("百事可乐和可口可乐各5瓶\n土豆2斤")

    assert [
        (line.line_id, line.requested_name, line.quantity, line.unit)
        for line in lines
    ] == [
        ("L001", "百事可乐", 5, "瓶"),
        ("L002", "可口可乐", 5, "瓶"),
        ("L003", "土豆", 2, "斤"),
    ]


@pytest.mark.parametrize(
    ("text", "name", "quantity", "unit"),
    [
        ("来十斤马铃薯", "马铃薯", 10, "斤"),
        ("给我来5斤洋芋", "洋芋", 5, "斤"),
        ("来土豆2斤", "土豆", 2, "斤"),
    ],
)
def test_parse_order_text_accepts_spoken_quantity_order(
    text,
    name,
    quantity,
    unit,
):
    line = parse_order_text(text)[0]

    assert (
        line.requested_name,
        line.quantity,
        line.unit,
    ) == (name, quantity, unit)


def test_parse_order_text_splits_on_inter_item_space():
    """数字+单位后空格应拆分为独立订单行。"""
    lines = parse_order_text("鸡蛋21个 牛肉10斤")

    assert [
        (line.requested_name, line.quantity, line.unit)
        for line in lines
    ] == [
        ("鸡蛋", 21, "个"),
        ("牛肉", 10, "斤"),
    ]


def test_parse_order_text_preserves_quantity_first_with_space():
    """动词前缀+数字+单位+空格+名称不应被空格拆分。"""
    lines = parse_order_text("来5斤 洋芋")

    assert len(lines) == 1
    assert (lines[0].requested_name, lines[0].quantity, lines[0].unit) == (
        "洋芋",
        5,
        "斤",
    )


def test_parse_order_text_splits_each_without_conjunction():
    """"各"模式无需"和"连接词，按 2 字符切分名称。"""
    lines = parse_order_text("苹果荔枝各5斤\n土豆2斤")

    assert [
        (line.requested_name, line.quantity, line.unit)
        for line in lines
    ] == [
        ("苹果", 5, "斤"),
        ("荔枝", 5, "斤"),
        ("土豆", 2, "斤"),
    ]


def test_parse_order_text_accepts_latin_unit():
    """Latin 单位 kg 应被识别。"""
    lines = parse_order_text("洋芋100kg")

    assert len(lines) == 1
    assert (lines[0].requested_name, lines[0].quantity, lines[0].unit) == (
        "洋芋",
        100,
        "kg",
    )


def test_spoken_alias_phrase_matches_erp_product(tmp_path):
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )

    result = asyncio.run(_billing_toolset(session).preview_sales_order(
        "来十斤马铃薯",
        source="voice",
    ))

    assert _product_payload(result) == {
        "ok": True,
        "confirmed_products": [
            {
                "line_id": "L001",
                "product_id": "P001",
                "product_name": "土豆",
                "unit": "斤",
                "quantity": 10,
            },
        ],
        "recommended_products": [],
        "unmatched_products": [],
    }


@pytest.mark.parametrize(
    ("erp_name", "requested_name"),
    [
        ("土豆", "马铃薯"),
        ("土豆", "洋芋"),
        ("洋芋", "土豆"),
        ("马铃薯", "洋芋"),
    ],
)
def test_complete_alias_group_matches_any_member(
    tmp_path,
    erp_name,
    requested_name,
):
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": erp_name, "unit": "斤"}],
    )

    draft = session.create_draft_from_text(
        "%s10斤" % requested_name,
    )

    assert draft.status == "ready"
    assert draft.lines[0].product is not None
    assert draft.lines[0].product.name == erp_name
    assert draft.lines[0].match_type == "alias_exact"


def test_external_aliases_form_complete_equivalence_group(tmp_path):
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text(
        json.dumps(
            {
                "花菜": "花椰菜",
                "菜花": "花椰菜",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "菜花", "unit": "斤"}],
        alias_path=alias_path,
        use_default_fresh_aliases=False,
    )

    draft = session.create_draft_from_text("花菜2斤")

    assert draft.status == "ready"
    assert draft.lines[0].product is not None
    assert draft.lines[0].product.name == "菜花"
    assert draft.lines[0].match_type == "alias_exact"


def test_alias_file_overrides_same_default_alias_key(tmp_path):
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text(
        json.dumps({"土豆": "地豆"}, ensure_ascii=False),
        encoding="utf-8",
    )
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
        alias_path=alias_path,
    )

    replaced_default = session.create_draft_from_text("马铃薯2斤")
    configured_alias = session.create_draft_from_text("地豆2斤")

    assert replaced_default.status == "needs_confirmation"
    assert replaced_default.lines[0].product is None
    assert configured_alias.status == "ready"
    assert configured_alias.lines[0].product is not None
    assert configured_alias.lines[0].product.name == "土豆"


def test_direct_exact_name_wins_over_alias_product(tmp_path):
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "马铃薯", "unit": "斤"},
            {"ptypeid": "P002", "pfullname": "土豆", "unit": "斤"},
        ],
    )

    draft = session.create_draft_from_text("马铃薯2斤")

    assert draft.status == "ready"
    assert draft.lines[0].product is not None
    assert draft.lines[0].product.product_id == "P001"
    assert draft.lines[0].match_type == "name_exact"


def test_multiple_alias_exact_products_require_confirmation(tmp_path):
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"},
            {"ptypeid": "P002", "pfullname": "洋芋", "unit": "斤"},
        ],
    )

    draft = session.create_draft_from_text("马铃薯2斤")

    assert draft.status == "needs_confirmation"
    assert draft.lines[0].product is None
    assert [item.product.product_id for item in draft.lines[0].candidates] == [
        "P001",
        "P002",
    ]
    assert all(
        item.match_type == "alias_exact"
        for item in draft.lines[0].candidates
    )


def test_alias_exact_is_not_removed_by_recommendation_threshold(tmp_path):
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
        recommendation_score=1.0,
    )

    draft = session.create_draft_from_text("洋芋2斤")

    assert draft.status == "ready"
    assert draft.lines[0].product is not None
    assert draft.lines[0].product.name == "土豆"


def test_alias_contains_matches_composite_product_name(tmp_path):
    """别名组包含匹配：别名词作为商品名子串时进入推荐，覆盖复合命名商品。

    ERP 商品常用"别名词+修饰词"命名（如袋装土豆、土豆毛料），alias_exact 只做
    精确相等无法命中；alias_contains 用同义词组里的词对商品名做子串探测，使
    "山药蛋"能命中"袋装土豆"并进入推荐待用户确认。
    """
    alias_path = tmp_path / "aliases.json"
    alias_path.write_text(
        json.dumps({"山药蛋": "马铃薯", "土豆": "马铃薯"}, ensure_ascii=False),
        encoding="utf-8",
    )
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "袋装土豆", "unit": "KG"}],
        alias_path=alias_path,
        use_default_fresh_aliases=False,
    )

    draft = session.create_draft_from_text("山药蛋2斤")

    assert draft.status == "needs_confirmation"
    assert draft.lines[0].product is None
    assert draft.lines[0].candidates
    assert draft.lines[0].candidates[0].product.name == "袋装土豆"
    assert draft.lines[0].candidates[0].match_type == "alias_contains"


def test_category_contains_matches_multiple_products(tmp_path):
    """泛称词（牛肉）扩展为部位关键词，推荐多个 category_contains 候选。"""
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pusercode": "SP001", "pfullname": "牛腱子", "unit": "斤"},
            {"ptypeid": "P002", "pusercode": "SP002", "pfullname": "牛肉-牛腩", "unit": "斤"},
            {"ptypeid": "P003", "pusercode": "SP003", "pfullname": "土豆", "unit": "斤"},
        ],
    )

    draft = session.create_draft_from_text("牛肉10斤")

    assert draft.status == "needs_confirmation"
    assert draft.lines[0].product is None
    candidate_names = {c.product.name for c in draft.lines[0].candidates}
    assert candidate_names == {"牛腱子", "牛肉-牛腩"}
    for candidate in draft.lines[0].candidates:
        assert candidate.match_type == "category_contains"


def test_category_contains_not_auto_matched(tmp_path):
    """单候选 category_contains 仍走推荐，不自动命中（不在 _DIRECT_MATCH_TYPES）。"""
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pusercode": "SP001", "pfullname": "牛腱子", "unit": "斤"},
        ],
    )

    draft = session.create_draft_from_text("牛肉10斤")

    assert draft.status == "needs_confirmation"
    assert draft.lines[0].product is None
    assert len(draft.lines[0].candidates) == 1
    assert draft.lines[0].candidates[0].match_type == "category_contains"


def test_category_contains_no_match_for_unrelated(tmp_path):
    """非泛称词（土豆）走既有 name_exact 路径，不触发 category_contains。"""
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pusercode": "SP001", "pfullname": "土豆", "unit": "斤"},
            {"ptypeid": "P002", "pusercode": "SP002", "pfullname": "牛腱子", "unit": "斤"},
        ],
    )

    draft = session.create_draft_from_text("土豆2斤")

    assert draft.status == "ready"
    assert draft.lines[0].product is not None
    assert draft.lines[0].product.name == "土豆"
    assert draft.lines[0].match_type == "name_exact"


def test_category_contains_filters_processed_food_noodle(tmp_path):
    """牛肉不应通过 category_contains 匹配到牛肉面，面食不是生鲜肉。"""
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pusercode": "SP001", "pfullname": "牛肉面", "unit": "箱"},
            {"ptypeid": "P002", "pusercode": "SP002", "pfullname": "牛腱子", "unit": "斤"},
        ],
    )

    draft = session.create_draft_from_text("牛肉10斤")

    assert draft.status == "needs_confirmation"
    candidate_ids = {c.product.product_id for c in draft.lines[0].candidates}
    assert "P001" not in candidate_ids
    assert "P002" in candidate_ids


def test_alias_contains_filters_processed_food_flavor(tmp_path):
    """西红柿（别名→番茄）不应通过 alias_contains 匹配到番茄味薯片。

    番茄味薯片中的番茄仅是口味修饰，商品本身是零食而非蔬菜。
    """
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pusercode": "SP001", "pfullname": "番茄", "unit": "斤"},
            {"ptypeid": "P002", "pusercode": "SP002", "pfullname": "番茄味薯片", "unit": "袋"},
        ],
    )

    draft = session.create_draft_from_text("西红柿5斤")

    assert draft.status == "ready"
    assert draft.lines[0].product is not None
    assert draft.lines[0].product.name == "番茄"
    assert draft.lines[0].match_type == "alias_exact"


def test_contains_filters_processed_food_for_ingredient_keyword(tmp_path):
    """一般关键词“牛肉”不应通过任何子串匹配命中牛肉肠。

    子串匹配被过滤后，模糊匹配也施加惩罚使其降级到阈值以下。
    """
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pusercode": "SP001", "pfullname": "牛肉肠", "unit": "根"},
        ],
    )

    draft = session.create_draft_from_text("牛肉10斤")

    assert draft.status == "needs_confirmation"
    assert not draft.lines[0].candidates


def test_confirmed_line_not_found_error_includes_valid_ids(tmp_path):
    """erp_confirmed_line_not_found 错误信息应附带有效行 ID 列表。"""
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "牛肉1", "unit": "斤"},
            {"ptypeid": "P002", "pfullname": "牛肉2", "unit": "斤"},
        ],
    )

    result = asyncio.run(_billing_toolset(session).preview_sales_order(
        "牛肉10斤",
        confirmed_products=[{"line_id": "L999", "product_id": "P001"}],
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == "erp_confirmed_line_not_found"
    assert "L001" in result["error"]["message"]


def test_search_products_returns_unique_alias_match(tmp_path):
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pusercode": "SP001", "pfullname": "土豆", "unit": "斤"}],
    )

    result = session.search_products(["洋芋"])

    assert result == {
        "ok": True,
        "results": [
            {
                "query": "洋芋",
                "status": "matched",
                "product": {
                    "product_id": "P001",
                    "product_name": "土豆",
                    "unit": "斤",
                },
                "recommendations": [],
            },
        ],
    }


def test_search_products_batch_returns_multiple_results(tmp_path):
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pusercode": "SP001", "pfullname": "土豆", "unit": "斤"},
            {"ptypeid": "P002", "pusercode": "SP002", "pfullname": "西红柿", "unit": "斤"},
        ],
    )

    result = session.search_products(["洋芋", "番茄"])

    assert result["ok"] is True
    assert len(result["results"]) == 2
    assert result["results"][0]["query"] == "洋芋"
    assert result["results"][0]["status"] == "matched"
    assert result["results"][0]["product"]["product_name"] == "土豆"
    assert result["results"][1]["query"] == "番茄"
    assert result["results"][1]["status"] == "matched"
    assert result["results"][1]["product"]["product_name"] == "西红柿"


def test_create_draft_puts_best_match_outside_and_similar_products_inside(
    tmp_path,
):
    products = [
        {
            "ptypeid": "P%d" % index,
            "pusercode": code,
            "pfullname": name,
            "unit": "斤",
        }
        for index, (code, name) in enumerate(
            [
                ("000501", "牛肉-牛皮"),
                ("000502", "牛肉-牛蹄"),
                ("r0004", "牛肉1"),
                ("r0005", "牛肉2"),
                ("r0006", "牛肉3"),
            ],
            start=1,
        )
    ]
    session = _session(tmp_path, products)
    result = asyncio.run(_billing_toolset(session).preview_sales_order("牛肉10斤"))

    assert _product_payload(result) == {
        "ok": True,
        "confirmed_products": [],
        "recommended_products": [
            {
                "line_id": "L001",
                "product_id": "P1",
                "product_name": "牛肉-牛皮",
                "unit": "斤",
                "quantity": 10,
                "similar_products": [
                    {
                        "line_id": "L001",
                        "product_id": "P2",
                        "product_name": "牛肉-牛蹄",
                        "unit": "斤",
                        "quantity": 10,
                    },
                    {
                        "line_id": "L001",
                        "product_id": "P3",
                        "product_name": "牛肉1",
                        "unit": "斤",
                        "quantity": 10,
                    },
                    {
                        "line_id": "L001",
                        "product_id": "P4",
                        "product_name": "牛肉2",
                        "unit": "斤",
                        "quantity": 10,
                    },
                    {
                        "line_id": "L001",
                        "product_id": "P5",
                        "product_name": "牛肉3",
                        "unit": "斤",
                        "quantity": 10,
                    },
                ],
            },
        ],
        "unmatched_products": [],
    }


def test_create_draft_returns_unmatched_products_separately(tmp_path):
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )

    result = asyncio.run(_billing_toolset(session).preview_sales_order("量子芯片2箱"))

    assert _product_payload(result) == {
        "ok": True,
        "confirmed_products": [],
        "recommended_products": [],
        "unmatched_products": [
            {
                "line_id": "L001",
                "product_id": None,
                "product_name": "量子芯片",
                "unit": "箱",
                "quantity": 2,
            },
        ],
    }


def test_create_draft_groups_three_match_results_in_one_json(tmp_path):
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"},
            {"ptypeid": "P101", "pfullname": "牛肉1", "unit": "斤"},
            {"ptypeid": "P102", "pfullname": "牛肉2", "unit": "斤"},
        ],
    )

    result = asyncio.run(_billing_toolset(session).preview_sales_order(
        "土豆5斤，牛肉10斤，量子芯片2箱",
    ))

    assert _product_payload(result) == {
        "ok": True,
        "confirmed_products": [
            {
                "line_id": "L001",
                "product_id": "P001",
                "product_name": "土豆",
                "unit": "斤",
                "quantity": 5,
            },
        ],
        "recommended_products": [
            {
                "line_id": "L002",
                "product_id": "P101",
                "product_name": "牛肉1",
                "unit": "斤",
                "quantity": 10,
                "similar_products": [
                    {
                        "line_id": "L002",
                        "product_id": "P102",
                        "product_name": "牛肉2",
                        "unit": "斤",
                        "quantity": 10,
                    },
                ],
            },
        ],
        "unmatched_products": [
            {
                "line_id": "L003",
                "product_id": None,
                "product_name": "量子芯片",
                "unit": "箱",
                "quantity": 2,
            },
        ],
    }


def test_create_draft_validates_frontend_confirmation(tmp_path):
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "牛肉1", "unit": "斤"},
            {"ptypeid": "P002", "pfullname": "牛肉2", "unit": "斤"},
        ],
    )
    toolset = _billing_toolset(session)

    result = asyncio.run(toolset.preview_sales_order(
        "牛肉10斤",
        confirmed_products=[{"line_id": "L001", "product_id": "P002"}],
    ))

    assert _product_payload(result) == {
        "ok": True,
        "confirmed_products": [
            {
                "line_id": "L001",
                "product_id": "P002",
                "product_name": "牛肉2",
                "unit": "斤",
                "quantity": 10,
            },
        ],
        "recommended_products": [],
        "unmatched_products": [],
    }


@pytest.mark.parametrize(
    ("confirmed_products", "error_code"),
    [
        ([{"line_id": "L999", "product_id": "P001"}], "erp_confirmed_line_not_found"),
        ([{"line_id": "L001", "product_id": ""}], "erp_confirmed_product_invalid"),
        ([{"line_id": "L001", "product_id": "P404"}], "erp_product_not_found"),
        ([{"line_id": "L001", "product_id": "P003"}], "erp_confirmed_product_not_recommended"),
        # 向后兼容：dict 格式仍可使用
        ({"L999": "P001"}, "erp_confirmed_line_not_found"),
        ({"L001": "P404"}, "erp_product_not_found"),
        ({"L001": "P003"}, "erp_confirmed_product_not_recommended"),
        # 字符串容错：JSON 文本
        ('[{"lineId": "L999", "productId": "P001"}]', "erp_confirmed_line_not_found"),
    ],
)
def test_create_draft_rejects_invalid_confirmation(
    tmp_path,
    confirmed_products,
    error_code,
):
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "牛肉1", "unit": "斤"},
            {"ptypeid": "P002", "pfullname": "牛肉2", "unit": "斤"},
            {"ptypeid": "P003", "pfullname": "苹果", "unit": "斤"},
        ],
    )

    result = asyncio.run(_billing_toolset(session).preview_sales_order(
        "牛肉10斤",
        confirmed_products=confirmed_products,
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == error_code


def test_multi_turn_modification_rebuilds_complete_text(tmp_path):
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"},
            {"ptypeid": "P002", "pfullname": "西红柿", "unit": "斤"},
        ],
    )
    toolset = _billing_toolset(session)

    first = asyncio.run(toolset.preview_sales_order("马铃薯5斤"))
    second = asyncio.run(toolset.preview_sales_order("马铃薯8斤，番茄3斤", source="voice"))

    assert [
        (line["product_name"], line["quantity"])
        for line in first["confirmed_products"]
    ] == [("土豆", 5)]
    assert [
        (line["product_name"], line["quantity"])
        for line in second["confirmed_products"]
    ] == [
        ("土豆", 8),
        ("西红柿", 3),
    ]


def test_create_draft_rejects_unsupported_source(tmp_path):
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )

    result = asyncio.run(_billing_toolset(session).preview_sales_order(
        "土豆2斤",
        source="audio",
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == "erp_order_source_invalid"


def test_product_accepts_live_erp_fields():
    product = Product.from_mapping(
        {
            "ptypeid": "0000100001",
            "ptypeid_pusercode": "000101",
            "ptypeid_pfullname": "豌豆尖",
            "ptypeid_unit1": "斤",
            "ptypeid_basebarcode": "000101",
            "preprice1": 1.1,
            "qty": 12,
        },
    )

    assert product.product_id == "0000100001"
    assert product.code == "000101"
    assert product.name == "豌豆尖"
    assert product.unit == "斤"
    assert product.barcode == "000101"
    assert product.price == 1.1
    assert product.stock == 12


def test_product_image_urls_present_when_erp_row_has_imageurls():
    """有 image_urls 的商品应在 core_fields 和 to_payload 中附带图片 URL。"""
    product = Product.from_mapping(
        {
            "ptypeid": "P001",
            "pfullname": "番茄",
            "unit": "斤",
            "imageUrls": ["https://cdn.example.com/a.jpg"],
        },
    )

    assert product.image_urls == ("https://cdn.example.com/a.jpg",)
    assert product.core_fields()["image_urls"] == ["https://cdn.example.com/a.jpg"]
    assert product.to_payload()["imageUrls"] == ["https://cdn.example.com/a.jpg"]


def test_product_image_urls_absent_when_erp_row_has_no_imageurls():
    """无 image_urls 的商品在 core_fields 和 to_payload 中不出现 image_urls 键。"""
    product = Product.from_mapping(
        {"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"},
    )

    assert product.image_urls == ()
    assert "image_urls" not in product.core_fields()
    assert "imageUrls" not in product.to_payload()


def test_list_products_includes_image_urls_when_present(tmp_path):
    """list_products 输出商品时，有图片带 image_urls，无图片不带该键。"""
    session = _session(
        tmp_path,
        [
            {
                "ptypeid": "P001",
                "pfullname": "番茄",
                "unit": "斤",
                "imageUrls": ["https://cdn.example.com/a.jpg"],
            },
            {"ptypeid": "P002", "pfullname": "土豆", "unit": "斤"},
        ],
    )
    toolset = _billing_toolset(session)

    result = asyncio.run(toolset.list_products())

    products = {item["product_name"]: item for item in result["products"]}
    assert products["番茄"]["image_urls"] == ["https://cdn.example.com/a.jpg"]
    assert "image_urls" not in products["土豆"]


def test_live_product_normalization_keeps_leaf_products_only():
    rows = [
        {
            "ptypeid": "00000",
            "pfullname": "全部商品",
            "pusercode": "000",
            "sonnum": 10,
        },
        {
            "ptypeid": "00001",
            "pfullname": "方便面",
            "pusercode": "0001",
            "sonnum": 2,
        },
        {
            "ptypeid": "0000100001",
            "pfullname": "豌豆尖",
            "pusercode": "000101",
            "sonnum": 0,
            "uname": "斤",
        },
        {
            "ptypeid": "0000100002",
            "pfullname": "停用菜",
            "pusercode": "000102",
            "sonnum": 0,
            "isstop": 1,
        },
    ]

    products = normalize_live_product_rows(rows, leaf_only=True)

    assert [item["name"] for item in products] == ["豌豆尖"]


def test_billing_toolset_exposes_complete_sales_order_tools(tmp_path):
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session)

    assert {tool.name for tool in toolset.local_tools()} == {
        "sync_products",
        "list_products",
        "search_products",
        "search_billing_references",
        "preview_sales_order",
        "submit_sales_order",
        "get_sales_order",
        "list_sales_orders",
        "void_sales_order",
        "update_sales_order",
    }
    assert toolset.get("sync_products").is_read_only is False
    assert toolset.get("list_products").is_read_only is True
    assert toolset.get("search_products").is_read_only is True
    assert toolset.get("search_billing_references").is_read_only is True
    assert toolset.get("preview_sales_order").is_read_only is True
    assert toolset.get("submit_sales_order").is_read_only is False
    assert toolset.get("get_sales_order").is_read_only is True
    assert toolset.get("list_sales_orders").is_read_only is True
    assert toolset.get("void_sales_order").is_read_only is False
    assert toolset.get("update_sales_order").is_read_only is False
    for tool in toolset.local_tools():
        schema_text = json.dumps(tool.input_schema, ensure_ascii=False)
        assert "output_path" not in schema_text
        assert "catalog_path" not in schema_text
        assert "file_path" not in schema_text
    schema = toolset.get("preview_sales_order").input_schema
    confirmed = schema["properties"]["confirmed_products"]
    assert confirmed["type"] == "array"
    assert confirmed["items"]["type"] == "object"
    assert set(confirmed["items"]["properties"]) == {"line_id", "product_id"}
    assert set(confirmed["items"]["required"]) == {"line_id", "product_id"}


def test_billing_settings_contain_no_runtime_output_path():
    assert {item.name for item in fields(ErpBillingSettings)} == {
        "product_catalog_path",
        "alias_path",
        "recommendation_score",
        "use_default_fresh_aliases",
        "category_path",
        "use_default_categories",
    }


def test_sync_products_replaces_in_memory_catalog_without_writing_file(tmp_path):
    session = ErpBillingSession.from_settings(
        _settings(tmp_path),
        allow_missing_catalog=True,
    )

    class FakeBillingApi:
        async def fetch_products(self, context, limit=None):
            assert context.account_id == "billing-test"
            assert limit == 1
            return BillingProductSnapshot(
                products=(
                    {
                        "productId": "P001",
                        "code": "SP001",
                        "name": "土豆",
                        "unit": "斤",
                    },
                ),
            )

    result = asyncio.run(_billing_toolset(session, FakeBillingApi()).sync_products(limit=1))

    assert result["ok"] is True
    assert result["product_count"] == 1
    assert "catalogPath" not in result
    assert result["sample_products"][0]["product_name"] == "土豆"
    assert session.catalog.products[0].name == "土豆"
    assert list(tmp_path.iterdir()) == []


def test_in_memory_sync_preserves_aliases_replaces_rows_and_is_not_shared(
    tmp_path,
):
    session = ErpBillingSession.from_settings(
        _settings(tmp_path),
        allow_missing_catalog=True,
    )

    class SequenceBillingApi:
        def __init__(self):
            self.snapshots = [
                BillingProductSnapshot(
                    products=(
                        {
                            "productId": "P001",
                            "name": "土豆",
                            "unit": "斤",
                        },
                    ),
                ),
                BillingProductSnapshot(
                    products=(
                        {
                            "productId": "P002",
                            "name": "苹果",
                            "unit": "斤",
                        },
                    ),
                ),
            ]

        async def fetch_products(self, context, limit=None):
            return self.snapshots.pop(0)

    toolset = _billing_toolset(session, SequenceBillingApi())

    assert asyncio.run(toolset.sync_products())["ok"] is True
    alias_draft = asyncio.run(toolset.preview_sales_order("来十斤马铃薯", source="voice"))
    assert alias_draft["confirmed_products"][0]["product_name"] == "土豆"

    assert asyncio.run(toolset.sync_products())["ok"] is True
    assert [product.product_id for product in session.catalog.products] == [
        "P002",
    ]
    assert session.search_products(["土豆"])["results"][0]["status"] == "unmatched"
    assert (
        asyncio.run(toolset.preview_sales_order("苹果2斤"))["confirmed_products"][0]["product_id"]
        == "P002"
    )

    new_session = ErpBillingSession.from_settings(
        _settings(tmp_path),
        allow_missing_catalog=True,
    )
    assert new_session.catalog.products == []
    assert list(tmp_path.iterdir()) == []


def test_failed_sync_keeps_previous_in_memory_catalog(tmp_path):
    session = ErpBillingSession.from_settings(
        _settings(tmp_path),
        allow_missing_catalog=True,
    )

    class FailingSecondSyncApi:
        def __init__(self):
            self.call_count = 0

        async def fetch_products(self, context, limit=None):
            self.call_count += 1
            if self.call_count == 1:
                return BillingProductSnapshot(
                    products=(
                        {
                            "productId": "P001",
                            "name": "土豆",
                            "unit": "斤",
                        },
                    ),
                )
            return BillingProductSnapshot(products=())

    toolset = _billing_toolset(session, FailingSecondSyncApi())

    assert asyncio.run(toolset.sync_products())["ok"] is True
    failed = asyncio.run(toolset.sync_products())

    assert failed["ok"] is False
    assert failed["error"]["code"] == "erp_live_product_empty"
    assert [product.product_id for product in session.catalog.products] == [
        "P001",
    ]
    assert list(tmp_path.iterdir()) == []


def test_create_draft_auto_syncs_when_catalog_empty(tmp_path):
    """目录为空时 preview_sales_order 自动同步一次，后续调用不重复拉取。"""
    session = ErpBillingSession.from_settings(
        _settings(tmp_path),
        allow_missing_catalog=True,
    )

    class CountingBillingApi:
        def __init__(self):
            self.calls = 0

        async def fetch_products(self, context, limit=None):
            self.calls += 1
            return BillingProductSnapshot(
                products=(
                    {
                        "productId": "P001",
                        "name": "土豆",
                        "unit": "斤",
                    },
                ),
            )

    api = CountingBillingApi()
    toolset = _billing_toolset(session, api)

    first = asyncio.run(toolset.preview_sales_order("土豆2斤"))
    second = asyncio.run(toolset.preview_sales_order("土豆3斤"))

    assert first["ok"] is True
    assert first["confirmed_products"][0]["product_id"] == "P001"
    assert second["ok"] is True
    assert api.calls == 1


def test_create_draft_returns_error_when_auto_sync_fails(tmp_path):
    """目录为空且自动同步失败时，preview_sales_order 返回底层错误而非目录为空提示。"""
    session = ErpBillingSession.from_settings(
        _settings(tmp_path),
        allow_missing_catalog=True,
    )

    result = asyncio.run(_billing_toolset(session).preview_sales_order("土豆2斤"))

    assert result["ok"] is False
    assert result["error"]["code"] == "billing_api_not_configured"


class CompleteSalesOrderApi:
    """完整销售单流程使用的确定性业务端口。"""

    def __init__(self):
        self.created_payloads = []

    @staticmethod
    async def fetch_products(context, limit=None):
        return BillingProductSnapshot(products=())

    @staticmethod
    async def search_customers(context, keyword, limit=10):
        return BillingReferenceSnapshot(
            options=(
                {"id": "CUS-1", "code": "C001", "name": "客户甲", "isDefault": False},
            ),
        )

    @staticmethod
    async def search_warehouses(context, keyword, limit=10):
        return BillingReferenceSnapshot(
            options=(
                {"id": "WH-1", "code": "W001", "name": "一号仓", "isDefault": True},
            ),
        )

    @staticmethod
    async def search_staff(context, keyword, limit=10):
        return BillingReferenceSnapshot(
            options=(
                {"id": "STAFF-1", "code": "S001", "name": "张三", "isDefault": True},
            ),
        )

    async def create_sales_order(self, context, payload):
        self.created_payloads.append(payload)
        return BillingSalesOrderResult(order_id="SO-20260804-1")

    @staticmethod
    async def get_sales_order_detail(context, order_id):
        return BillingSalesOrderDetailResult(
            order={
                "id": order_id,
                "orderNo": "SO20260804001",
                "orderDate": "2026-08-04",
                "customerId": "CUS-1",
                "customerName": "客户甲",
                "warehouseId": "WH-1",
                "warehouseName": "一号仓",
                "handlerId": "STAFF-1",
                "handlerName": "张三",
                "status": 2,
                "statusName": "已生效",
                "totalAmount": 7.0,
                "items": [
                    {
                        "productId": "P001",
                        "productName": "土豆",
                        "unit": "斤",
                        "quantity": 2,
                        "unitPrice": 3.5,
                        "amount": 7.0,
                    },
                ],
            },
        )

    @staticmethod
    async def search_sales_orders(
        context,
        *,
        page_num=1,
        page_size=20,
        sort_by="",
        order_type="",
        start_date="",
        end_date="",
        status=None,
        payment_status=None,
        return_status=None,
        order_no="",
        customer_id="",
    ):
        return BillingSalesOrderPageResult(
            total=1,
            page_num=page_num,
            page_size=page_size,
            orders=(
                {
                    "id": "208457406331712307",
                    "orderNo": "SO20260804001",
                    "orderDate": "2026-08-04",
                    "customerName": "客户甲",
                    "status": 2,
                    "statusName": "已生效",
                    "totalAmount": 7.0,
                },
            ),
        )

    async def void_sales_order(self, context, order_id):
        self.voided_order_ids = getattr(self, "voided_order_ids", [])
        self.voided_order_ids.append(order_id)

    async def update_sales_order(self, context, order_id, payload):
        self.updated_payloads = getattr(self, "updated_payloads", [])
        self.updated_payloads.append((order_id, payload))
        return BillingSalesOrderResult(order_id=order_id)


def test_preview_sales_order_distinguishes_required_optional_and_system_fields(tmp_path):
    session = _session(
        tmp_path,
        [{"id": "P001", "name": "土豆", "unit": "斤"}],
    )

    result = asyncio.run(_billing_toolset(session).preview_sales_order(order_text="土豆2斤"))

    assert result["ok"] is True
    assert [item["field"] for item in result["missing_required_fields"]] == [
        "customer",
        "warehouse",
        "handler",
        "order_date",
    ]
    assert result["field_requirements"] == {
        "required": ["customer", "warehouse", "handler", "order_date", "order_text"],
        "optional": ["remark"],
        "system_managed": ["id", "save_type"],
    }
    assert result["ready_to_submit"] is False
    assert result["preview_id"] is None


def test_complete_sales_order_preview_confirmation_submit_and_idempotency(tmp_path):
    session = _session(
        tmp_path,
        [{"id": "P001", "code": "SP001", "name": "土豆", "unit": "斤", "salesPrice": 3.5}],
    )
    api = CompleteSalesOrderApi()
    toolset = _billing_toolset(session, api)

    prepared = asyncio.run(toolset.preview_sales_order(
        order_text="土豆2斤",
        customer="C001",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-08-04",
        remark="下午送达",
    ))

    assert prepared["ok"] is True
    assert prepared["ready_to_submit"] is True
    assert prepared["missing_required_fields"] == []
    assert prepared["needs_confirmation"] == []
    assert prepared["preview"]["save_type_label"] == "正式"
    assert prepared["preview"]["customer"]["id"] == "CUS-1"
    assert prepared["preview"]["items"] == [
        {
            "name": "土豆",
            "quantity": 2,
            "unit": "斤",
            "unit_price": 3.5,
        },
    ]

    rejected = asyncio.run(toolset.submit_sales_order(
        prepared["preview_id"],
        "business-request-1",
        confirmed_by_user=False,
    ))
    assert rejected["error"]["code"] == "erp_sales_order_confirmation_required"
    assert api.created_payloads == []

    submitted = asyncio.run(toolset.submit_sales_order(
        prepared["preview_id"],
        "business-request-1",
        confirmed_by_user=True,
    ))
    assert submitted == {
        "ok": True,
        "submitted": True,
        "order_id": "SO-20260804-1",
        "preview_id": prepared["preview_id"],
        "save_type": "final",
        "idempotent_replay": False,
    }
    assert api.created_payloads == [
        {
            "id": 0,
            "orderDate": "2026-08-04",
            "customerId": "CUS-1",
            "warehouseId": "WH-1",
            "handlerId": "STAFF-1",
            "saveType": 2,
            "remark": "下午送达",
            "items": [
                {
                    "productId": "P001",
                    "quantity": 2,
                    "unit": "斤",
                    "unitPrice": 3.5,
                },
            ],
        },
    ]

    replayed = asyncio.run(toolset.submit_sales_order(
        prepared["preview_id"],
        "business-request-1",
        confirmed_by_user=True,
    ))
    assert replayed["idempotent_replay"] is True
    assert len(api.created_payloads) == 1


@pytest.mark.parametrize(
    ("save_type", "expected_code"),
    [("draft", 0), ("pre_receipt", 1), ("final", 2)],
)
def test_sales_order_save_type_matches_real_frontend_mapping(
    tmp_path,
    save_type,
    expected_code,
):
    session = _session(
        tmp_path,
        [{"id": "P001", "name": "土豆", "unit": "斤"}],
    )
    api = CompleteSalesOrderApi()
    toolset = _billing_toolset(session, api)
    prepared = asyncio.run(toolset.preview_sales_order(
        order_text="土豆2斤",
        customer="客户甲",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-08-04",
        save_type=save_type,
    ))

    assert prepared["ready_to_submit"] is True
    submitted = asyncio.run(toolset.submit_sales_order(
        prepared["preview_id"],
        "save-type-" + save_type,
        confirmed_by_user=True,
    ))

    assert submitted["ok"] is True
    assert api.created_payloads[0]["saveType"] == expected_code


def test_preview_sales_order_rejects_invalid_date_long_remark_and_unit_guessing(tmp_path):
    session = _session(
        tmp_path,
        [{"id": "P001", "name": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session, CompleteSalesOrderApi())

    invalid_date = asyncio.run(toolset.preview_sales_order(order_date="2026/08/04"))
    long_remark = asyncio.run(toolset.preview_sales_order(remark="备" * 201))
    unit_mismatch = asyncio.run(toolset.preview_sales_order(
        order_text="土豆2kg",
        customer="客户甲",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-08-04",
    ))

    assert invalid_date["error"]["code"] == "erp_sales_order_date_invalid"
    assert long_remark["error"]["code"] == "erp_sales_order_remark_too_long"
    assert unit_mismatch["ready_to_submit"] is False
    assert unit_mismatch["unit_warnings"][0]["requested_unit"] == "kg"
    assert unit_mismatch["unit_warnings"][0]["erp_unit"] == "斤"


def test_match_logger_records_auto_matched_alias_lines(tmp_path):
    """别名精确命中的匹配行应被记入 JSONL，供离线挖掘同义词候选。"""
    catalog_path = _write_catalog(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    log_path = tmp_path / "match.jsonl"
    session = ErpBillingSession.from_settings(
        _settings(tmp_path, catalog_path=catalog_path),
        match_logger=JsonlMatchEventLogger(log_path),
    )

    session.create_draft_from_text("马铃薯10斤", source="voice")

    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == 1
    assert events[0]["requestedName"] == "马铃薯"
    assert events[0]["productId"] == "P001"
    assert events[0]["productName"] == "土豆"
    assert events[0]["matchType"] == "alias_exact"
    assert events[0]["source"] == "voice"


def test_match_logger_records_user_confirmed_lines(tmp_path):
    """用户从推荐列表确认的商品应记为 user_confirmed，这是别名缺失最有价值的证据。"""
    catalog_path = _write_catalog(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "牛肉1", "unit": "斤"},
            {"ptypeid": "P002", "pfullname": "牛肉2", "unit": "斤"},
        ],
    )
    log_path = tmp_path / "match.jsonl"
    session = ErpBillingSession.from_settings(
        _settings(tmp_path, catalog_path=catalog_path),
        match_logger=JsonlMatchEventLogger(log_path),
    )

    asyncio.run(_billing_toolset(session).preview_sales_order(
        "牛肉10斤",
        confirmed_products=[{"lineId": "L001", "productId": "P002"}],
    ))

    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == 1
    assert events[0]["requestedName"] == "牛肉"
    assert events[0]["productId"] == "P002"
    assert events[0]["productName"] == "牛肉2"
    assert events[0]["matchType"] == "user_confirmed"


def test_match_logger_skips_unmatched_and_recommendation_lines(tmp_path):
    """未匹配和仅推荐的行不产生匹配事件日志。"""
    catalog_path = _write_catalog(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "牛肉1", "unit": "斤"},
            {"ptypeid": "P002", "pfullname": "牛肉2", "unit": "斤"},
        ],
    )
    log_path = tmp_path / "match.jsonl"
    session = ErpBillingSession.from_settings(
        _settings(tmp_path, catalog_path=catalog_path),
        match_logger=JsonlMatchEventLogger(log_path),
    )

    session.create_draft_from_text("量子芯片2箱")

    assert not log_path.exists()


def test_no_match_logger_does_not_write(tmp_path):
    """未注入匹配日志器时不应产生任何文件，现有调用链保持向后兼容。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )

    session.create_draft_from_text("土豆2斤")

    assert [p for p in tmp_path.iterdir() if p.suffix == ".jsonl"] == []


def test_create_match_logger_from_env_returns_null_when_unset(monkeypatch):
    """环境变量未配置时返回 NullMatchEventLogger，不产生文件 IO。"""
    monkeypatch.delenv("ERP_BILLING_MATCH_LOG", raising=False)
    logger = create_match_logger_from_env()

    assert isinstance(logger, NullMatchEventLogger)


# ---------------------------------------------------------------------------
# confirmed_products 多格式输入测试
# ---------------------------------------------------------------------------


def test_confirmed_products_list_format_success(tmp_path):
    """list[dict] 格式的 confirmed_products 应正确确认推荐商品。"""
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "牛肉1", "unit": "斤"},
            {"ptypeid": "P002", "pfullname": "牛肉2", "unit": "斤"},
        ],
    )
    toolset = _billing_toolset(session)

    result = asyncio.run(toolset.preview_sales_order(
        "牛肉10斤",
        confirmed_products=[{"line_id": "L001", "product_id": "P002"}],
    ))

    assert _product_payload(result) == {
        "ok": True,
        "confirmed_products": [
            {
                "line_id": "L001",
                "product_id": "P002",
                "product_name": "牛肉2",
                "unit": "斤",
                "quantity": 10,
            },
        ],
        "recommended_products": [],
        "unmatched_products": [],
    }


def test_confirmed_products_dict_format_backward_compat(tmp_path):
    """dict[str, str] 格式的 confirmed_products 仍可正常使用（向后兼容）。"""
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "牛肉1", "unit": "斤"},
            {"ptypeid": "P002", "pfullname": "牛肉2", "unit": "斤"},
        ],
    )
    toolset = _billing_toolset(session)

    result = asyncio.run(toolset.preview_sales_order(
        "牛肉10斤",
        confirmed_products={"L001": "P002"},
    ))

    assert result["ok"] is True
    assert result["confirmed_products"][0]["product_id"] == "P002"


def test_confirmed_products_string_tolerance(tmp_path):
    """JSON 字符串格式的 confirmed_products 应被自动解析。"""
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "牛肉1", "unit": "斤"},
            {"ptypeid": "P002", "pfullname": "牛肉2", "unit": "斤"},
        ],
    )
    toolset = _billing_toolset(session)

    result = asyncio.run(toolset.preview_sales_order(
        "牛肉10斤",
        confirmed_products='[{"lineId": "L001", "productId": "P002"}]',
    ))

    assert result["ok"] is True
    assert result["confirmed_products"][0]["product_id"] == "P002"


def test_confirmed_products_for_unmatched_allows_manual_spec(tmp_path):
    """unmatchedProducts 中无候选的行，可通过 confirmed_products 手动指定商品。"""
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"},
            {"ptypeid": "P002", "pfullname": "量子芯片", "unit": "箱"},
        ],
    )
    toolset = _billing_toolset(session)

    result = asyncio.run(toolset.preview_sales_order(
        "土豆2斤，未知商品1箱",
        confirmed_products=[{"lineId": "L002", "productId": "P002"}],
    ))

    assert result["ok"] is True
    assert len(result["confirmed_products"]) == 2
    assert result["confirmed_products"][1]["product_id"] == "P002"


# ---------------------------------------------------------------------------
# partial 部分提交测试
# ---------------------------------------------------------------------------


def test_partial_preview_skips_unmatched_products(tmp_path):
    """partial=True 时预览只含已匹配商品，跳过未匹配行。"""
    session = _session(
        tmp_path,
        [
            {"id": "P001", "code": "SP001", "name": "土豆", "unit": "斤", "salesPrice": 3.5},
            {"id": "P002", "code": "SP002", "name": "量子芯片", "unit": "箱"},
        ],
    )
    api = CompleteSalesOrderApi()
    toolset = _billing_toolset(session, api)

    result = asyncio.run(toolset.preview_sales_order(
        order_text="土豆2斤，未知商品1箱",
        customer="客户甲",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-08-04",
        partial=True,
    ))

    assert result["ok"] is True
    assert result["ready_to_submit"] is True
    assert len(result["preview"]["items"]) == 1
    assert result["preview"]["items"][0]["name"] == "土豆"


def test_partial_false_requires_all_matched(tmp_path):
    """partial=False 时有未匹配商品不能生成预览。"""
    session = _session(
        tmp_path,
        [
            {"id": "P001", "name": "土豆", "unit": "斤"},
        ],
    )
    toolset = _billing_toolset(session, CompleteSalesOrderApi())

    result = asyncio.run(toolset.preview_sales_order(
        order_text="土豆2斤，未知商品1箱",
        customer="客户甲",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-08-04",
    ))

    assert result["ok"] is True
    assert result["ready_to_submit"] is False
    assert result["preview_id"] is None


# ---------------------------------------------------------------------------
# 扩充默认别名测试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("erp_name", "requested_name"),
    [
        ("黄瓜", "胡瓜"),
        ("黄瓜", "青瓜"),
        ("卷心菜", "包菜"),
        ("卷心菜", "洋白菜"),
        ("卷心菜", "莲花白"),
        ("花椰菜", "花菜"),
        ("花椰菜", "菜花"),
        ("猪肚", "肚子"),
        ("慈菇", "茨菇"),
    ],
)
def test_expanded_default_aliases_match(
    tmp_path,
    erp_name,
    requested_name,
):
    """扩充后的默认别名应能正确匹配 ERP 商品。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": erp_name, "unit": "斤"}],
    )

    draft = session.create_draft_from_text("%s2斤" % requested_name)

    assert draft.status == "ready"
    assert draft.lines[0].product is not None
    assert draft.lines[0].product.name == erp_name
    assert draft.lines[0].match_type == "alias_exact"


# ---------------------------------------------------------------------------
# 客户/经手人候选去重测试
# ---------------------------------------------------------------------------


def test_list_products_returns_paginated_catalog(tmp_path):
    """list_products 应分页返回当前会话商品目录。"""
    products = [
        {
            "id": "P%03d" % i,
            "code": "S%03d" % i,
            "name": "商品%d" % i,
            "unit": "斤",
        }
        for i in range(1, 26)
    ]
    session = _session(tmp_path, products)
    toolset = _billing_toolset(session)

    page1 = asyncio.run(toolset.list_products(page=1, page_size=10))
    page2 = asyncio.run(toolset.list_products(page=2, page_size=10))
    page3 = asyncio.run(toolset.list_products(page=3, page_size=10))

    assert page1["ok"] is True
    assert page1["total"] == 25
    assert page1["page"] == 1
    assert len(page1["products"]) == 10
    assert page1["products"][0]["product_name"] == "商品1"

    assert len(page2["products"]) == 10
    assert page2["products"][0]["product_name"] == "商品11"

    assert len(page3["products"]) == 5
    assert page3["products"][0]["product_name"] == "商品21"


def test_list_products_auto_syncs_when_empty(tmp_path):
    """目录为空时 list_products 自动同步后返回商品。"""
    session = ErpBillingSession.from_settings(
        _settings(tmp_path),
        allow_missing_catalog=True,
    )

    class FakeApi:
        @staticmethod
        async def fetch_products(context, limit=None):
            return BillingProductSnapshot(
                products=(
                    {"productId": "P001", "name": "土豆", "unit": "斤"},
                ),
            )

    toolset = _billing_toolset(session, FakeApi())
    result = asyncio.run(toolset.list_products())

    assert result["ok"] is True
    assert result["total"] == 1
    assert result["products"][0]["product_name"] == "土豆"


def test_sync_products_returns_sample_products(tmp_path):
    """sync_products 应返回 sample_products 样本供用户浏览。"""
    products_data = [
        {"id": "P00%d" % i, "code": "S00%d" % i, "name": "商品%d" % i, "unit": "斤"}
        for i in range(1, 11)
    ]
    session = ErpBillingSession.from_settings(
        _settings(tmp_path),
        allow_missing_catalog=True,
    )

    class SampleApi:
        @staticmethod
        async def fetch_products(context, limit=None):
            return BillingProductSnapshot(products=tuple(products_data))

    toolset = _billing_toolset(session, SampleApi())
    result = asyncio.run(toolset.sync_products())

    assert result["ok"] is True
    assert result["product_count"] == 10
    assert len(result["sample_products"]) == 5
    assert result["sample_products"][0]["product_name"] == "商品1"
    assert "product_id" not in result["sample_products"][0]
    assert "code" not in result["sample_products"][0]


def test_reference_dedup_reduces_business_type_variants(tmp_path):
    """同一客户的多业务类型变体应被去重，最多返回 5 个候选。"""
    session = _session(
        tmp_path,
        [{"id": "P001", "name": "土豆", "unit": "斤"}],
    )

    class MultiVariantApi:
        @staticmethod
        async def fetch_products(context, limit=None):
            return BillingProductSnapshot(products=())

        @staticmethod
        async def search_customers(context, keyword, limit=10):
            return BillingReferenceSnapshot(
                options=tuple(
                    {"id": "C-%d" % i, "code": "C%03d" % i, "name": name}
                    for i, name in enumerate(
                        [
                            "好又多超市西湖店-70WL（KH0104）",
                            "杭州西湖区好又多超市西湖店-70WL-COVR",
                            "杭州西湖区好又多超市西湖店-70WL-SALE",
                            "杭州西湖区好又多超市西湖店-70WL-PURC",
                            "杭州西湖区好又多超市西湖店-70WL-INVT",
                            "杭州西湖区好又多超市西湖店-70WL-FINA",
                            "杭州西湖区好又多超市西湖店-70WL-TRXN",
                            "杭州西湖区好又多超市西湖店-70WL-MSTR",
                            "好又多超市西湖店-99NK（KH0103）",
                            "杭州西湖区好又多超市西湖店-99NK-COVR",
                        ],
                        start=1,
                    )
                ),
            )

        @staticmethod
        async def search_warehouses(context, keyword, limit=10):
            return BillingReferenceSnapshot(options=())

        @staticmethod
        async def search_staff(context, keyword, limit=10):
            return BillingReferenceSnapshot(options=())

        async def create_sales_order(self, context, payload):
            return BillingSalesOrderResult(order_id="SO-1")

    toolset = _billing_toolset(session, MultiVariantApi())
    result = asyncio.run(toolset.preview_sales_order(
        order_text="土豆2斤",
        customer="好又多超市西湖店",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-08-04",
    ))

    customer_resolution = result["reference_resolutions"]["customer"]
    assert customer_resolution["status"] == "ambiguous"
    # 10 个候选去重后不超过 5 个
    assert len(customer_resolution["candidates"]) <= 5


# ---------------------------------------------------------------------------
# 工具输出 schema（outputSchema）测试
# ---------------------------------------------------------------------------


def test_billing_tools_carry_output_schema(tmp_path):
    """每个开单工具应携带 output_schema，且 required 只含必定出现的 ok。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session)
    by_name = {tool.name: tool for tool in toolset.local_tools()}

    assert set(by_name) == set(BILLING_MCP_TOOL_NAMES)
    for name in BILLING_MCP_TOOL_NAMES:
        schema = by_name[name].output_schema
        assert schema is not None
        assert schema["required"] == ["ok"]


def test_tool_outputs_validate_against_output_schema(tmp_path):
    """工具实际返回（含图片字段、可空 preview_id 和错误路径）应通过 output_schema 校验。"""
    products_data = [
        {
            "ptypeid": "P001",
            "pfullname": "土豆",
            "unit": "斤",
            "imageUrls": ["https://cdn.example.com/a.jpg"],
        },
        {"ptypeid": "P002", "pfullname": "番茄", "unit": "斤"},
    ]
    session = _session(tmp_path, products_data)

    class BillingApi:
        @staticmethod
        async def fetch_products(context, limit=None):
            return BillingProductSnapshot(products=tuple(products_data))

        @staticmethod
        async def search_customers(context, keyword, limit=10):
            return await CompleteSalesOrderApi.search_customers(context, keyword, limit)

        @staticmethod
        async def search_warehouses(context, keyword, limit=10):
            return await CompleteSalesOrderApi.search_warehouses(context, keyword, limit)

        @staticmethod
        async def search_staff(context, keyword, limit=10):
            return await CompleteSalesOrderApi.search_staff(context, keyword, limit)

        async def create_sales_order(self, context, payload):
            return await CompleteSalesOrderApi().create_sales_order(context, payload)

        @staticmethod
        async def get_sales_order_detail(context, order_id):
            return await CompleteSalesOrderApi.get_sales_order_detail(context, order_id)

        @staticmethod
        async def search_sales_orders(context, **kwargs):
            return await CompleteSalesOrderApi.search_sales_orders(context, **kwargs)

        @staticmethod
        async def void_sales_order(context, order_id):
            await CompleteSalesOrderApi().void_sales_order(context, order_id)

        @staticmethod
        async def update_sales_order(context, order_id, payload):
            return await CompleteSalesOrderApi().update_sales_order(
                context, order_id, payload,
            )

    toolset = _billing_toolset(session, BillingApi())
    by_name = {tool.name: tool for tool in toolset.local_tools()}

    synced = asyncio.run(toolset.sync_products())
    jsonschema.validate(synced, by_name["sync_products"].output_schema)
    assert any("image_urls" in item for item in synced["sample_products"])

    listed = asyncio.run(toolset.list_products())
    jsonschema.validate(listed, by_name["list_products"].output_schema)

    searched = session.search_products(["土豆", "量子芯片"])
    jsonschema.validate(searched, by_name["search_products"].output_schema)

    options = asyncio.run(toolset.search_billing_references("customer", "客户甲"))
    jsonschema.validate(options, by_name["search_billing_references"].output_schema)

    prepared = asyncio.run(toolset.preview_sales_order(order_text="土豆2斤"))
    jsonschema.validate(prepared, by_name["preview_sales_order"].output_schema)
    assert prepared["preview_id"] is None

    ready = asyncio.run(toolset.preview_sales_order(
        order_text="土豆2斤",
        customer="C001",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-08-04",
    ))
    jsonschema.validate(ready, by_name["preview_sales_order"].output_schema)
    assert ready["preview_id"] is not None

    submitted = asyncio.run(toolset.submit_sales_order(
        ready["preview_id"],
        "business-key-1",
        confirmed_by_user=True,
    ))
    jsonschema.validate(submitted, by_name["submit_sales_order"].output_schema)

    rejected = asyncio.run(toolset.submit_sales_order(
        ready["preview_id"],
        "business-key-2",
        confirmed_by_user=False,
    ))
    jsonschema.validate(rejected, by_name["submit_sales_order"].output_schema)
    assert rejected["ok"] is False

    detail = asyncio.run(toolset.get_sales_order(order_id="208457406331712307"))
    jsonschema.validate(detail, by_name["get_sales_order"].output_schema)
    assert detail["order"]["orderNo"] == "SO20260804001"

    orders = asyncio.run(toolset.list_sales_orders(
        start_date="2026-07-04",
        end_date="2026-08-04",
        sort_by="updateTime",
        order_type="desc",
    ))
    jsonschema.validate(orders, by_name["list_sales_orders"].output_schema)
    assert orders["total"] == 1

    voided = asyncio.run(toolset.void_sales_order(
        order_id="208457406331712307",
        confirmed_by_user=True,
    ))
    jsonschema.validate(voided, by_name["void_sales_order"].output_schema)

    modified = asyncio.run(toolset.update_sales_order(
        order_id="208457406331712307",
        order_date="2026-08-04",
        handler_id="STAFF-1",
        items=[{"productId": "P001", "quantity": 3}],
        confirmed_by_user=True,
    ))
    jsonschema.validate(modified, by_name["update_sales_order"].output_schema)


# ---------------------------------------------------------------------------
# 销售单详情 / 列表 / 作废 / 修改 测试
# ---------------------------------------------------------------------------


def test_get_sales_order_returns_order(tmp_path):
    """get_sales_order_detail 应返回 ERP 销售单详情。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session, CompleteSalesOrderApi())

    result = asyncio.run(toolset.get_sales_order(order_id="208457406331712307"))

    assert result["ok"] is True
    assert result["order"]["id"] == "208457406331712307"
    assert result["order"]["orderNo"] == "SO20260804001"
    assert result["order"]["items"][0]["productName"] == "土豆"


def test_get_sales_order_returns_error_when_api_not_configured(tmp_path):
    """未注入 Adapter 时 get_sales_order_detail 应返回明确错误。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session)

    result = asyncio.run(toolset.get_sales_order(order_id="208457406331712307"))

    assert result["ok"] is False
    assert result["error"]["code"] == "billing_api_not_configured"


def test_list_sales_orders_with_date_range_and_status(tmp_path):
    """list_sales_orders 应按日期范围和状态筛选销售单列表。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session, CompleteSalesOrderApi())

    result = asyncio.run(toolset.list_sales_orders(
        page=1,
        page_size=20,
        sort_by="updateTime",
        order_type="desc",
        start_date="2026-07-04",
        end_date="2026-08-04",
        status=2,
    ))

    assert result["ok"] is True
    assert result["page"] == 1
    assert result["page_size"] == 20
    assert result["total"] == 1
    assert result["orders"][0]["orderNo"] == "SO20260804001"


def test_list_sales_orders_rejects_invalid_date_format(tmp_path):
    """list_sales_orders 应拒绝非 YYYY-MM-DD 格式的日期。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session, CompleteSalesOrderApi())

    result = asyncio.run(toolset.list_sales_orders(start_date="2026/07/04"))

    assert result["ok"] is False
    assert result["error"]["code"] == "erp_sales_order_date_invalid"


def test_list_sales_orders_rejects_end_before_start(tmp_path):
    """list_sales_orders 应拒绝结束日期早于开始日期。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session, CompleteSalesOrderApi())

    result = asyncio.run(toolset.list_sales_orders(
        start_date="2026-08-04",
        end_date="2026-07-04",
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == "erp_sales_order_date_invalid"


def test_void_sales_order_requires_confirmation(tmp_path):
    """void_sales_order 未确认时应拒绝执行。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    api = CompleteSalesOrderApi()
    toolset = _billing_toolset(session, api)

    rejected = asyncio.run(toolset.void_sales_order(
        order_id="208457406331712307",
        confirmed_by_user=False,
    ))

    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "erp_sales_order_confirmation_required"
    assert not getattr(api, "voided_order_ids", [])


def test_void_sales_order_executes_after_confirmation(tmp_path):
    """void_sales_order 在用户确认后应作废单据。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    api = CompleteSalesOrderApi()
    toolset = _billing_toolset(session, api)

    result = asyncio.run(toolset.void_sales_order(
        order_id="208457406331712307",
        confirmed_by_user=True,
    ))

    assert result["ok"] is True
    assert result["voided"] is True
    assert result["order_id"] == "208457406331712307"
    assert api.voided_order_ids == ["208457406331712307"]


def test_void_sales_order_rejects_empty_id(tmp_path):
    """void_sales_order 应拒绝空的销售单 ID。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session, CompleteSalesOrderApi())

    result = asyncio.run(toolset.void_sales_order(
        order_id="",
        confirmed_by_user=True,
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == "erp_sales_order_id_invalid"


def test_update_sales_order_builds_payload_and_executes(tmp_path):
    """modify_sales_order 应构建 SalesOrderUpdateDTO 并在确认后执行。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    api = CompleteSalesOrderApi()
    toolset = _billing_toolset(session, api)

    result = asyncio.run(toolset.update_sales_order(
        order_id="208457406331712307",
        order_date="2026-08-04",
        handler_id="STAFF-1",
        items=[
            {
                "productId": "P001",
                "quantity": 3,
                "unit": "斤",
                "unitPrice": 3.5,
                "remark": "加急",
            },
        ],
        customer_id="CUS-1",
        warehouse_id="WH-1",
        save_type="draft",
        remark="下午送达",
        confirmed_by_user=True,
    ))

    assert result["ok"] is True
    assert result["modified"] is True
    assert result["order_id"] == "208457406331712307"
    order_id, payload = api.updated_payloads[0]
    assert order_id == "208457406331712307"
    assert payload["id"] == 208457406331712307
    assert payload["orderDate"] == "2026-08-04"
    assert payload["handlerId"] == "STAFF-1"
    assert payload["customerId"] == "CUS-1"
    assert payload["warehouseId"] == "WH-1"
    assert payload["saveType"] == 0
    assert payload["remark"] == "下午送达"
    assert payload["items"] == [
        {
            "productId": "P001",
            "quantity": 3.0,
            "unit": "斤",
            "unitPrice": 3.5,
            "remark": "加急",
        },
    ]


def test_update_sales_order_requires_confirmation(tmp_path):
    """modify_sales_order 未确认时应拒绝执行。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    api = CompleteSalesOrderApi()
    toolset = _billing_toolset(session, api)

    rejected = asyncio.run(toolset.update_sales_order(
        order_id="208457406331712307",
        order_date="2026-08-04",
        handler_id="STAFF-1",
        items=[{"productId": "P001", "quantity": 3}],
        confirmed_by_user=False,
    ))

    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "erp_sales_order_confirmation_required"
    assert not getattr(api, "updated_payloads", [])


def test_update_sales_order_rejects_empty_items(tmp_path):
    """modify_sales_order 应拒绝空商品明细。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session, CompleteSalesOrderApi())

    result = asyncio.run(toolset.update_sales_order(
        order_id="208457406331712307",
        order_date="2026-08-04",
        handler_id="STAFF-1",
        items=[],
        confirmed_by_user=True,
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == "erp_sales_order_items_empty"


def test_update_sales_order_rejects_item_without_product_id(tmp_path):
    """modify_sales_order 应拒绝缺少 productId 的商品明细。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session, CompleteSalesOrderApi())

    result = asyncio.run(toolset.update_sales_order(
        order_id="208457406331712307",
        order_date="2026-08-04",
        handler_id="STAFF-1",
        items=[{"quantity": 3}],
        confirmed_by_user=True,
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == "erp_sales_order_item_invalid"


def test_update_sales_order_rejects_invalid_date(tmp_path):
    """modify_sales_order 应拒绝非 YYYY-MM-DD 格式的日期。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session, CompleteSalesOrderApi())

    result = asyncio.run(toolset.update_sales_order(
        order_id="208457406331712307",
        order_date="2026/08/04",
        handler_id="STAFF-1",
        items=[{"productId": "P001", "quantity": 3}],
        confirmed_by_user=True,
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == "erp_sales_order_date_invalid"


def test_update_sales_order_rejects_zero_quantity(tmp_path):
    """modify_sales_order 应拒绝数量小于等于 0 的商品明细。"""
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session, CompleteSalesOrderApi())

    result = asyncio.run(toolset.update_sales_order(
        order_id="208457406331712307",
        order_date="2026-08-04",
        handler_id="STAFF-1",
        items=[{"productId": "P001", "quantity": 0}],
        confirmed_by_user=True,
    ))

    assert result["ok"] is False
    assert result["error"]["code"] == "erp_sales_order_item_invalid"

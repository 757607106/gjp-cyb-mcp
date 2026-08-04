import json
from dataclasses import fields

import pytest
from agentscope.agent import Agent

from erp_billing.adapters import UnavailableBillingApi
from erp_billing.catalog import normalize_live_product_rows
from erp_billing.config import ErpBillingSettings
from erp_billing.models import Product
from erp_billing.ports import (
    BillingProductSnapshot,
    BillingReferenceSnapshot,
    BillingSalesOrderResult,
)
from erp_billing.session import ErpBillingSession, parse_order_text
from erp_billing.toolset import BillingToolSet
from gjp_cli.agent import ERP_BILLING_AGENT_SPEC, build_agent
from gjp_cli.model_runtime import LLMSettings
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
        "confirmedProducts": result["confirmedProducts"],
        "recommendedProducts": result["recommendedProducts"],
        "unmatchedProducts": result["unmatchedProducts"],
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

    result = _billing_toolset(session).prepare_sales_order(
        "来十斤马铃薯",
        source="voice",
    )

    assert _product_payload(result) == {
        "ok": True,
        "confirmedProducts": [
            {
                "lineId": "L001",
                "ptypeid": "P001",
                "pfullname": "土豆",
                "unit": "斤",
                "quantity": 10,
            },
        ],
        "recommendedProducts": [],
        "unmatchedProducts": [],
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
    """ERP_CONFIRMED_LINE_NOT_FOUND 错误信息应附带有效行 ID 列表。"""
    session = _session(
        tmp_path,
        [
            {"ptypeid": "P001", "pfullname": "牛肉1", "unit": "斤"},
            {"ptypeid": "P002", "pfullname": "牛肉2", "unit": "斤"},
        ],
    )

    result = _billing_toolset(session).prepare_sales_order(
        "牛肉10斤",
        confirmed_products={"L999": "P001"},
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "ERP_CONFIRMED_LINE_NOT_FOUND"
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
                    "ptypeid": "P001",
                    "pfullname": "土豆",
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
    assert result["results"][0]["product"]["pfullname"] == "土豆"
    assert result["results"][1]["query"] == "番茄"
    assert result["results"][1]["status"] == "matched"
    assert result["results"][1]["product"]["pfullname"] == "西红柿"


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
    result = _billing_toolset(session).prepare_sales_order("牛肉10斤")

    assert _product_payload(result) == {
        "ok": True,
        "confirmedProducts": [],
        "recommendedProducts": [
            {
                "lineId": "L001",
                "ptypeid": "P1",
                "pfullname": "牛肉-牛皮",
                "unit": "斤",
                "quantity": 10,
                "similarProducts": [
                    {
                        "lineId": "L001",
                        "ptypeid": "P2",
                        "pfullname": "牛肉-牛蹄",
                        "unit": "斤",
                        "quantity": 10,
                    },
                    {
                        "lineId": "L001",
                        "ptypeid": "P3",
                        "pfullname": "牛肉1",
                        "unit": "斤",
                        "quantity": 10,
                    },
                    {
                        "lineId": "L001",
                        "ptypeid": "P4",
                        "pfullname": "牛肉2",
                        "unit": "斤",
                        "quantity": 10,
                    },
                    {
                        "lineId": "L001",
                        "ptypeid": "P5",
                        "pfullname": "牛肉3",
                        "unit": "斤",
                        "quantity": 10,
                    },
                ],
            },
        ],
        "unmatchedProducts": [],
    }


def test_create_draft_returns_unmatched_products_separately(tmp_path):
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )

    result = _billing_toolset(session).prepare_sales_order("量子芯片2箱")

    assert _product_payload(result) == {
        "ok": True,
        "confirmedProducts": [],
        "recommendedProducts": [],
        "unmatchedProducts": [
            {
                "lineId": "L001",
                "ptypeid": None,
                "pfullname": "量子芯片",
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

    result = _billing_toolset(session).prepare_sales_order(
        "土豆5斤，牛肉10斤，量子芯片2箱",
    )

    assert _product_payload(result) == {
        "ok": True,
        "confirmedProducts": [
            {
                "lineId": "L001",
                "ptypeid": "P001",
                "pfullname": "土豆",
                "unit": "斤",
                "quantity": 5,
            },
        ],
        "recommendedProducts": [
            {
                "lineId": "L002",
                "ptypeid": "P101",
                "pfullname": "牛肉1",
                "unit": "斤",
                "quantity": 10,
                "similarProducts": [
                    {
                        "lineId": "L002",
                        "ptypeid": "P102",
                        "pfullname": "牛肉2",
                        "unit": "斤",
                        "quantity": 10,
                    },
                ],
            },
        ],
        "unmatchedProducts": [
            {
                "lineId": "L003",
                "ptypeid": None,
                "pfullname": "量子芯片",
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

    result = toolset.prepare_sales_order(
        "牛肉10斤",
        confirmed_products={"L001": "P002"},
    )

    assert _product_payload(result) == {
        "ok": True,
        "confirmedProducts": [
            {
                "lineId": "L001",
                "ptypeid": "P002",
                "pfullname": "牛肉2",
                "unit": "斤",
                "quantity": 10,
            },
        ],
        "recommendedProducts": [],
        "unmatchedProducts": [],
    }


@pytest.mark.parametrize(
    ("confirmed_products", "error_code"),
    [
        ({"L999": "P001"}, "ERP_CONFIRMED_LINE_NOT_FOUND"),
        ({"L001": ""}, "ERP_CONFIRMED_PRODUCT_INVALID"),
        ({"": "P001"}, "ERP_CONFIRMED_PRODUCT_INVALID"),
        ({"L001": "P404"}, "ERP_PRODUCT_NOT_FOUND"),
        ({"L001": "P003"}, "ERP_CONFIRMED_PRODUCT_NOT_RECOMMENDED"),
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

    result = _billing_toolset(session).prepare_sales_order(
        "牛肉10斤",
        confirmed_products=confirmed_products,
    )

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

    first = toolset.prepare_sales_order("马铃薯5斤")
    second = toolset.prepare_sales_order("马铃薯8斤，番茄3斤", source="voice")

    assert [
        (line["pfullname"], line["quantity"])
        for line in first["confirmedProducts"]
    ] == [("土豆", 5)]
    assert [
        (line["pfullname"], line["quantity"])
        for line in second["confirmedProducts"]
    ] == [
        ("土豆", 8),
        ("西红柿", 3),
    ]


def test_create_draft_rejects_unsupported_source(tmp_path):
    session = _session(
        tmp_path,
        [{"ptypeid": "P001", "pfullname": "土豆", "unit": "斤"}],
    )

    result = _billing_toolset(session).prepare_sales_order(
        "土豆2斤",
        source="audio",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "ERP_ORDER_SOURCE_INVALID"


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
        "search_products",
        "search_sales_order_options",
        "prepare_sales_order",
        "submit_sales_order",
    }
    assert toolset.get("sync_products").is_read_only is False
    assert toolset.get("search_products").is_read_only is True
    assert toolset.get("search_sales_order_options").is_read_only is True
    assert toolset.get("prepare_sales_order").is_read_only is True
    assert toolset.get("submit_sales_order").is_read_only is False
    for tool in toolset.local_tools():
        schema_text = json.dumps(tool.input_schema, ensure_ascii=False)
        assert "output_path" not in schema_text
        assert "catalog_path" not in schema_text
        assert "file_path" not in schema_text
    schema = toolset.get("prepare_sales_order").input_schema
    confirmed = schema["properties"]["confirmed_products"]
    object_schema = next(
        item
        for item in confirmed["anyOf"]
        if item.get("type") == "object"
    )
    assert object_schema["additionalProperties"] == {"type": "string"}


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
        def fetch_products(self, context, limit=None):
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

    result = _billing_toolset(session, FakeBillingApi()).sync_products(limit=1)

    assert result["ok"] is True
    assert result["productCount"] == 1
    assert "catalogPath" not in result
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

        def fetch_products(self, context, limit=None):
            return self.snapshots.pop(0)

    toolset = _billing_toolset(session, SequenceBillingApi())

    assert toolset.sync_products()["ok"] is True
    alias_draft = toolset.prepare_sales_order("来十斤马铃薯", source="voice")
    assert alias_draft["confirmedProducts"][0]["pfullname"] == "土豆"

    assert toolset.sync_products()["ok"] is True
    assert [product.product_id for product in session.catalog.products] == [
        "P002",
    ]
    assert session.search_products(["土豆"])["results"][0]["status"] == "unmatched"
    assert (
        toolset.prepare_sales_order("苹果2斤")["confirmedProducts"][0]["ptypeid"]
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

        def fetch_products(self, context, limit=None):
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

    assert toolset.sync_products()["ok"] is True
    failed = toolset.sync_products()

    assert failed["ok"] is False
    assert failed["error"]["code"] == "ERP_LIVE_PRODUCT_EMPTY"
    assert [product.product_id for product in session.catalog.products] == [
        "P001",
    ]
    assert list(tmp_path.iterdir()) == []


def test_create_draft_auto_syncs_when_catalog_empty(tmp_path):
    """目录为空时 prepare_sales_order 自动同步一次，后续调用不重复拉取。"""
    session = ErpBillingSession.from_settings(
        _settings(tmp_path),
        allow_missing_catalog=True,
    )

    class CountingBillingApi:
        def __init__(self):
            self.calls = 0

        def fetch_products(self, context, limit=None):
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

    first = toolset.prepare_sales_order("土豆2斤")
    second = toolset.prepare_sales_order("土豆3斤")

    assert first["ok"] is True
    assert first["confirmedProducts"][0]["ptypeid"] == "P001"
    assert second["ok"] is True
    assert api.calls == 1


def test_create_draft_returns_error_when_auto_sync_fails(tmp_path):
    """目录为空且自动同步失败时，prepare_sales_order 返回底层错误而非目录为空提示。"""
    session = ErpBillingSession.from_settings(
        _settings(tmp_path),
        allow_missing_catalog=True,
    )

    result = _billing_toolset(session).prepare_sales_order("土豆2斤")

    assert result["ok"] is False
    assert result["error"]["code"] == "BILLING_API_NOT_CONFIGURED"


def test_erp_agent_uses_shared_agentscope_factory(tmp_path):
    session = ErpBillingSession.from_settings(
        _settings(tmp_path),
        allow_missing_catalog=True,
    )
    settings = LLMSettings(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        model_name="deepseek-chat",
        api_key="test-key",
        stream=True,
        parameters={},
        timeout_seconds=30,
        max_retries=3,
        context_size=None,
    )

    agent = build_agent(
        _billing_toolset(session),
        settings,
        ERP_BILLING_AGENT_SPEC,
    )

    assert isinstance(agent, Agent)
    assert agent.name == "ErpBillingAgent"


class CompleteSalesOrderApi:
    """完整销售单流程使用的确定性业务端口。"""

    def __init__(self):
        self.created_payloads = []

    @staticmethod
    def fetch_products(context, limit=None):
        return BillingProductSnapshot(products=())

    @staticmethod
    def search_customers(context, keyword, limit=10):
        return BillingReferenceSnapshot(
            options=(
                {"id": "CUS-1", "code": "C001", "name": "客户甲", "isDefault": False},
            ),
        )

    @staticmethod
    def search_warehouses(context, keyword, limit=10):
        return BillingReferenceSnapshot(
            options=(
                {"id": "WH-1", "code": "W001", "name": "一号仓", "isDefault": True},
            ),
        )

    @staticmethod
    def search_staff(context, keyword, limit=10):
        return BillingReferenceSnapshot(
            options=(
                {"id": "STAFF-1", "code": "S001", "name": "张三", "isDefault": True},
            ),
        )

    def create_sales_order(self, context, payload):
        self.created_payloads.append(payload)
        return BillingSalesOrderResult(order_id="SO-20260804-1")


def test_prepare_sales_order_distinguishes_required_optional_and_system_fields(tmp_path):
    session = _session(
        tmp_path,
        [{"id": "P001", "name": "土豆", "unit": "斤"}],
    )

    result = _billing_toolset(session).prepare_sales_order(order_text="土豆2斤")

    assert result["ok"] is True
    assert [item["field"] for item in result["missingRequiredFields"]] == [
        "customer",
        "warehouse",
        "handler",
        "order_date",
    ]
    assert result["fieldRequirements"] == {
        "required": ["customer", "warehouse", "handler", "order_date", "order_text"],
        "optional": ["remark"],
        "systemManaged": ["id", "saveType"],
    }
    assert result["readyToSubmit"] is False
    assert result["previewId"] is None


def test_complete_sales_order_preview_confirmation_submit_and_idempotency(tmp_path):
    session = _session(
        tmp_path,
        [{"id": "P001", "code": "SP001", "name": "土豆", "unit": "斤", "salesPrice": 3.5}],
    )
    api = CompleteSalesOrderApi()
    toolset = _billing_toolset(session, api)

    prepared = toolset.prepare_sales_order(
        order_text="土豆2斤",
        customer="C001",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-08-04",
        remark="下午送达",
    )

    assert prepared["ok"] is True
    assert prepared["readyToSubmit"] is True
    assert prepared["missingRequiredFields"] == []
    assert prepared["needsConfirmation"] == []
    assert prepared["preview"]["saveTypeLabel"] == "正式"
    assert prepared["preview"]["customer"]["id"] == "CUS-1"
    assert prepared["preview"]["items"] == [
        {
            "productId": "P001",
            "name": "土豆",
            "quantity": 2,
            "unit": "斤",
            "unitPrice": 3.5,
        },
    ]

    rejected = toolset.submit_sales_order(
        prepared["previewId"],
        "business-request-1",
        confirmed_by_user=False,
    )
    assert rejected["error"]["code"] == "ERP_SALES_ORDER_CONFIRMATION_REQUIRED"
    assert api.created_payloads == []

    submitted = toolset.submit_sales_order(
        prepared["previewId"],
        "business-request-1",
        confirmed_by_user=True,
    )
    assert submitted == {
        "ok": True,
        "submitted": True,
        "orderId": "SO-20260804-1",
        "previewId": prepared["previewId"],
        "saveType": "final",
        "idempotentReplay": False,
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

    replayed = toolset.submit_sales_order(
        prepared["previewId"],
        "business-request-1",
        confirmed_by_user=True,
    )
    assert replayed["idempotentReplay"] is True
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
    prepared = toolset.prepare_sales_order(
        order_text="土豆2斤",
        customer="客户甲",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-08-04",
        save_type=save_type,
    )

    assert prepared["readyToSubmit"] is True
    submitted = toolset.submit_sales_order(
        prepared["previewId"],
        "save-type-" + save_type,
        confirmed_by_user=True,
    )

    assert submitted["ok"] is True
    assert api.created_payloads[0]["saveType"] == expected_code


def test_prepare_sales_order_rejects_invalid_date_long_remark_and_unit_guessing(tmp_path):
    session = _session(
        tmp_path,
        [{"id": "P001", "name": "土豆", "unit": "斤"}],
    )
    toolset = _billing_toolset(session, CompleteSalesOrderApi())

    invalid_date = toolset.prepare_sales_order(order_date="2026/08/04")
    long_remark = toolset.prepare_sales_order(remark="备" * 201)
    unit_mismatch = toolset.prepare_sales_order(
        order_text="土豆2kg",
        customer="客户甲",
        warehouse="一号仓",
        handler="张三",
        order_date="2026-08-04",
    )

    assert invalid_date["error"]["code"] == "ERP_SALES_ORDER_DATE_INVALID"
    assert long_remark["error"]["code"] == "ERP_SALES_ORDER_REMARK_TOO_LONG"
    assert unit_mismatch["readyToSubmit"] is False
    assert unit_mismatch["unitWarnings"][0]["requestedUnit"] == "kg"
    assert unit_mismatch["unitWarnings"][0]["erpUnit"] == "斤"

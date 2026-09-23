"""单位确认、局部修改与上游错误的完整工具边界回归。"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx
import jsonschema
import pytest
from mcp.server import ServerRequestContext
from mcp.server.mcpserver import Context

from yuncyb.adapters import BusinessAuthenticatedJsonClient, ErpAuthenticatedHttpAdapter
from yuncyb.presentation import present_yuncyb_result
from gjp_common.context import InvocationContext
from gjp_common.errors import DomainError
from gjp_common.mcp import create_mcp_server
from tests.yuncyb.test_yuncyb import CompleteSalesOrderApi, _yuncyb_toolset, _session
from tests.yuncyb.test_mcp_tool_export import _StaticIdentityResolver, _StaticToolSetResolver
from tests.yuncyb.test_sales_order_id_resolution import _FakeHttp

PREVIEW = dict(order_text="土豆2kg", customer="C001", warehouse="一号仓",
               handler="张三", order_date="2026-08-04")
CONFIRMATION = dict(line_id="L001", product_id="P001", unit="斤", quantity=4)
ORDER = {
    "id": "123", "orderNo": "XS123", "status": 0,
    "orderDate": "2026-08-04", "handlerId": "11", "customerId": "12", "warehouseId": "13",
    "remark": "保留备注", "discountAmount": 5, "discountAccountId": "14",
    "receivedAmount": 10, "receiptAccountId": "15",
    "items": [{"id": "21", "productId": "P001", "quantity": 2,
               "unit": "箱", "unitId": "31", "conversionRate": 12, "unitPrice": 24,
               "remark": "保留行备注", "costPrice": 9}],
}


def toolset(tmp_path, api=None):
    return _yuncyb_toolset(
        _session(tmp_path, [{"id": "P001", "name": "土豆", "unit": "斤", "salesPrice": 3.5}]),
        api or CompleteSalesOrderApi(),
    )


def _protocol_server(tools):
    context = InvocationContext(
        tenant_id="tenant-test", subject_id="user-test", account_id="yuncyb-test",
        session_id="session-test", scopes=frozenset({"yuncyb:read", "yuncyb:write"}),
    )
    return create_mcp_server(
        "yuncyb", tools, _StaticIdentityResolver(context), _StaticToolSetResolver(tools),
        result_presenter=present_yuncyb_result,
    )


async def _call_yuncyb_protocol(server, name, **arguments):
    """必须经过 MCPServer 公共入口，不能绕过 presenter 或 outputSchema 校验。"""
    request_context = ServerRequestContext(
        session=SimpleNamespace(),
        lifespan_context=None,
        protocol_version="2026-07-28",
        method="tools/call",
        request_id=1,
        request=SimpleNamespace(headers={}),
    )
    result = await server.call_tool(
        name,
        arguments,
        Context(request_context=request_context, mcp_server=server),
    )
    assert isinstance(result.structured_content, dict)
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    return result


def _assert_business_text(result, *private_values):
    text = result.content[0].text
    # 不只隐藏 ID：执行动作、匹配状态、提交控制、诊断信息也不得展示。
    forbidden = (
        "preview_id", "product_id", "line_id", "orderItemId", "productId",
        "confirmed_products", "confirmed_units", "confirmed_by_user",
        "required_actions", "confirm_units", "confirm_submit", "idempotent_replay",
        "raw", "details", "trace", "catalog", "unknown", "page_size",
        '"matched"', '"final"', "提交状态", "保存类型",
        *private_values,
    )
    assert not [value for value in forbidden if value in text], text
    return text


def test_protocol_filtered_preview_confirmation_submit_and_replay(tmp_path):
    """客户端只使用过滤后的执行字段，仍可完成候选选择、单位确认和幂等提交。"""
    api = CompleteSalesOrderApi()
    server = _protocol_server(toolset(tmp_path, api))

    async def scenario():
        search = await _call_yuncyb_protocol(server, "searchProducts", keywords=["土豆"])
        product = search.structured_content["results"][0]["product"]
        references = await _call_yuncyb_protocol(
            server, "searchBusinessReferences", reference_type="customer", keyword=PREVIEW["customer"],
        )
        customer = references.structured_content["options"][0]
        first = await _call_yuncyb_protocol(server, "previewSalesOrder", **PREVIEW)
        pending = first.structured_content
        assert pending["ready_to_submit"] is False
        assert pending["preview_id"] is None
        assert pending["preview"] is None
        assert pending["required_actions"] == ["confirm_units"]
        warning, = pending["unit_warnings"]
        candidate, = pending["confirmed_products"]
        assert candidate["line_id"] == warning["line_id"]
        assert candidate["product_id"] == warning["product_id"] == product["product_id"]
        assert pending["reference_resolutions"]["customer"]["selected"]["id"] == customer["id"]

        # 模拟用户明确选择候选并确认“按斤开 4 斤”；身份字段必须从协议结果取值。
        confirmation = {
            "line_id": warning["line_id"], "product_id": candidate["product_id"],
            "unit": warning["erp_unit"], "quantity": CONFIRMATION["quantity"],
        }
        assert confirmation == CONFIRMATION
        ready = await _call_yuncyb_protocol(
            server, "previewSalesOrder", **{**PREVIEW, "customer": customer["id"]},
            confirmed_products=[{key: candidate[key] for key in ("line_id", "product_id")}],
            confirmed_units=[confirmation],
        )
        prepared = ready.structured_content
        assert prepared["ready_to_submit"] is True
        assert prepared["unit_warnings"] == []
        assert prepared["required_actions"] == ["confirm_submit"]
        assert prepared["preview"]["items"][0]["quantity"] == 4
        assert prepared["preview"]["total_amount"] == 14
        preview_id = prepared["preview_id"]
        assert isinstance(preview_id, str) and preview_id
        assert api.created_payloads == []

        rejected = await _call_yuncyb_protocol(server, "submitSalesOrder", preview_id=preview_id)
        assert rejected.is_error is True
        assert rejected.structured_content["ok"] is False
        assert rejected.structured_content["error"]["code"] == "erp_document_confirmation_required"
        assert api.created_payloads == []
        submitted = await _call_yuncyb_protocol(
            server, "submitSalesOrder", preview_id=preview_id, confirmed_by_user=True,
        )
        assert submitted.is_error is False
        replay = await _call_yuncyb_protocol(
            server, "submitSalesOrder", preview_id=preview_id, confirmed_by_user=True,
        )
        assert submitted.structured_content["submitted"] is True
        assert submitted.structured_content["idempotent_replay"] is False
        assert replay.structured_content == {**submitted.structured_content, "idempotent_replay": True}
        assert len(api.created_payloads) == 1
        assert api.created_payloads[0]["customerId"] == customer["id"]
        assert api.created_payloads[0]["items"] == [
            {"productId": product["product_id"], "quantity": 4, "unit": "斤", "unitPrice": 3.5},
        ]

        visible = ready.content[0].text
        assert "### 单据预览" in visible
        assert "| 名称 | 数量 | 单位 | 单价 | 金额 |" in visible
        assert "| 土豆 | 4.0 | 斤 | 3.5 | 14.0 |" in visible
        assert prepared["preview"]["items"][0]["line_amount"] == 14
        assert "| 合计金额 | 14.0 |" in visible
        assert submitted.structured_content["order_no"] in submitted.content[0].text
        # 先验证写入闭环和业务展示，再检查控制值不能因中文标签包装而泄漏。
        for result in (search, references, first, ready, rejected, submitted, replay):
            _assert_business_text(
                result, preview_id, candidate["line_id"], product["product_id"],
                customer["id"], "WH-1", "STAFF-1", "erp_document_confirmation_required",
            )

    asyncio.run(scenario())


def test_unit_confirmation_closes_preview_and_submit(tmp_path):
    api = CompleteSalesOrderApi()
    tools = toolset(tmp_path, api)

    async def scenario():
        first = await tools.preview_sales_order(**PREVIEW)
        assert not first["ready_to_submit"]
        assert first["preview_id"] is None
        assert first["unit_warnings"][0]["product_id"] == "P001"
        confirmed = await tools.preview_sales_order(**PREVIEW, confirmed_units=[CONFIRMATION])
        assert confirmed["ready_to_submit"]
        assert confirmed["unit_warnings"] == []
        assert confirmed["preview"]["items"][0]["quantity"] == 4
        assert confirmed["preview"]["items"][0]["unit"] == "斤"
        result = await tools.submit_sales_order(confirmed["preview_id"], confirmed_by_user=True)
        replay = await tools.submit_sales_order(confirmed["preview_id"], confirmed_by_user=True)
        assert result["submitted"]
        assert replay["idempotent_replay"]
        assert len(api.created_payloads) == 1
        assert api.created_payloads[0]["items"][0]["quantity"] == 4
        assert api.created_payloads[0]["items"][0]["unit"] == "斤"
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"line_id": "L002"}, {"product_id": "other"}, {"unit": "kg"},
    {"quantity": 0}, {"quantity": float("nan")}, {"quantity": True},
])
def test_invalid_unit_confirmation_never_prepares_order(tmp_path, changes):
    result = asyncio.run(toolset(tmp_path).preview_sales_order(
        **PREVIEW, confirmed_units=[{**CONFIRMATION, **changes}],
    ))
    assert result["error"]["code"] == "erp_unit_confirmation_invalid"


def test_duplicate_unit_confirmation_rejected(tmp_path):
    result = asyncio.run(toolset(tmp_path).preview_sales_order(
        **PREVIEW, confirmed_units=[CONFIRMATION, CONFIRMATION],
    ))
    assert result["error"]["code"] == "erp_unit_confirmation_invalid"


def update_tools(tmp_path, current=None):
    http = _FakeHttp()
    http.get_responses["/sales/orders/123"] = {"code": "A00000", "data": deepcopy(current or ORDER)}
    return toolset(tmp_path, ErpAuthenticatedHttpAdapter(http)), http


@pytest.mark.parametrize("item_id_key", ["id", "orderItemId"])
def test_protocol_filtered_sales_detail_can_update_existing_line(tmp_path, item_id_key):
    """详情保留原始 camelCase 身份字段，客户端可据此修改已生效单据的既有行。"""
    diagnostics = {
        "raw": {"productName": "raw-private"}, "details": {"message": "details-private"},
        "traceId": "trace-private", "catalog": {"name": "catalog-private"},
        "unknownField": "unknown-private",
    }
    current = deepcopy(ORDER)
    current.update(status=2, orderNo="XS20260804001", totalAmount="48.00", **diagnostics)
    current["items"][0].pop("id")
    current["items"][0].update({
        item_id_key: "7894561230001", "productName": "土豆", "amount": "48.00", **diagnostics,
    })
    tools, http = update_tools(tmp_path, current)
    server = _protocol_server(tools)

    async def scenario():
        detail = await _call_yuncyb_protocol(server, "getSalesOrder", order_id="123")
        order = detail.structured_content["order"]
        item, = order["items"]
        assert order["id"] == "123"
        assert item[item_id_key] == "7894561230001"
        assert item["productId"] == "P001"
        assert "product_id" not in item
        assert "order_item_id" not in item
        assert item["amount"] == "48.00"  # structured 不做展示层的数值转换
        assert order["totalAmount"] == "48.00"
        for record in (order, item):
            assert diagnostics.keys().isdisjoint(record)
        assert http.put_calls == []

        updated = await _call_yuncyb_protocol(
            server, "updateSalesOrder", order_id=order["id"], confirmed_by_user=True,
            items=[{
                "order_item_id": item[item_id_key], "product_id": item["productId"],
                "quantity": 3, "unit": item["unit"], "unit_price": item["unitPrice"],
                "unit_id": item["unitId"], "conversion_rate": item["conversionRate"],
            }],
        )
        assert updated.structured_content["modified"] is True
        assert len(http.put_calls) == 1
        path, body = http.put_calls[0]
        assert path == "/sales/orders/123"
        assert body["id"] == 123
        assert body["items"][0]["orderItemId"] == item[item_id_key]
        assert body["items"][0]["productId"] == item["productId"]
        assert body["items"][0]["quantity"] == 3
        assert body["items"][0]["unitId"] == item["unitId"]
        assert body["items"][0]["conversionRate"] == item["conversionRate"]
        assert "unitId" not in detail.content[0].text
        assert http.get_responses["/sales/orders/123"]["data"] == current
        for result in (detail, updated):
            _assert_business_text(result, order["id"], item[item_id_key], item["productId"])
        visible = detail.content[0].text
        assert "### 单据" in visible
        for label in ("数量", "单位", "单价", "备注", "成本单价", "商品名称", "金额"):
            assert label in visible
        assert "| 2 | 箱 | 12 | 24 | 保留行备注 | 9 | 土豆 | 48.0 |" in visible
        assert "| 合计金额 | 48.0 |" in visible

    asyncio.run(scenario())


def test_remark_only_update_preserves_latest_order_and_multiunit(tmp_path):
    tools, http = update_tools(tmp_path)
    result = asyncio.run(tools.update_sales_order(
        "123", remark="新备注", confirmed_by_user=True,
    ))
    assert result["modified"]
    body = http.put_calls[0][1]
    assert body["remark"] == "新备注"
    assert body["handlerId"] == "11"
    assert body["orderDate"] == ORDER["orderDate"]
    assert body["discountAmount"] == 5
    assert body["items"] == [{
        "orderItemId": "21", "productId": "P001", "quantity": 2,
        "unit": "箱", "unitId": "31", "conversionRate": 12, "unitPrice": 24,
        "remark": "保留行备注",
    }]
    assert "receiptAmount" not in body  # 已收金额不能当作本次追加收款
    assert "receiptAccountId" not in body
    assert "saveType" not in body


@pytest.mark.parametrize("changes,expected", [
    ({"discount_amount": 0}, "保留备注"),
    ({"remark": ""}, ""),
])
def test_omitted_remark_is_preserved_but_empty_string_clears(tmp_path, changes, expected):
    tools, http = update_tools(tmp_path)
    result = asyncio.run(tools.update_sales_order("123", **changes, confirmed_by_user=True))
    assert result["modified"]
    assert http.put_calls[0][1]["remark"] == expected


@pytest.mark.parametrize("status,changes,code", [
    (3, {"remark": "修改"}, "erp_sales_order_state_invalid"),
    (2, {"customer_id": "99"}, "erp_sales_order_field_locked"),
    (2, {"warehouse_id": "99"}, "erp_sales_order_field_locked"),
    (2, {"discount_amount": 9}, "erp_sales_order_field_locked"),
    (2, {"items": [{"product_id": "P001", "quantity": 3}]}, "erp_sales_order_item_id_required"),
    (0, {"save_type": "pre_receipt"}, "erp_sales_order_save_type_invalid"),
    (0, {}, "erp_sales_order_update_empty"),
])
def test_invalid_updates_do_not_write(tmp_path, status, changes, code):
    tools, http = update_tools(tmp_path, {**ORDER, "status": status})
    result = asyncio.run(tools.update_sales_order("123", **changes, confirmed_by_user=True))
    assert result["error"]["code"] == code
    assert not http.put_calls


def test_effective_order_remark_update_preserves_line_ids(tmp_path):
    tools, http = update_tools(tmp_path, {**ORDER, "status": 2})
    result = asyncio.run(tools.update_sales_order("123", remark="更新", confirmed_by_user=True))
    assert result["modified"]
    assert http.put_calls[0][1]["items"][0]["orderItemId"] == "21"


def test_tool_schemas_accept_structured_confirmation_and_partial_update(tmp_path):
    tools = toolset(tmp_path)
    jsonschema.validate({**PREVIEW, "confirmed_units": [CONFIRMATION]},
                        tools.get("preview_sales_order").input_schema)
    jsonschema.validate({"order_id": "123", "remark": "新备注", "confirmed_by_user": True},
                        tools.get("update_sales_order").input_schema)


def test_void_business_error_keeps_upstream_code_and_trace(tmp_path):
    class RejectedHttp(_FakeHttp):
        async def put_json(self, context, path, payload=None):
            return {"code": "A12345", "message": "不满足作废条件", "traceId": "trace-123",
                    "data": {"secret": "must-not-return"}}
    http = RejectedHttp()
    http.get_responses["/sales/orders/123"] = {"code": "A00000", "data": ORDER}
    result = asyncio.run(toolset(tmp_path, ErpAuthenticatedHttpAdapter(http)).void_sales_order("123", True))
    assert result["error"] == {
        "code": "erp_live_request_failed", "message": "不满足作废条件",
        "details": {"upstream_code": "A12345", "trace_id": "trace-123"},
    }


@pytest.mark.parametrize("status,code", [
    (400, "erp_live_request_failed"), (401, "business_reauth_required"),
    (403, "business_forbidden"), (500, "business_write_result_unknown"),
])
def test_http_errors_preserve_diagnostics_without_changing_retry_boundary(status, code):
    async def scenario():
        credentials = SimpleNamespace(resolve=lambda ctx: SimpleNamespace(kind="api_key", value="synthetic"))
        client = BusinessAuthenticatedJsonClient("https://example.com", credentials)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={
                "code": "A123", "traceId": "trace-1", "message": "操作失败", "data": {"private": 1},
            }),
        ))
        try:
            with pytest.raises(DomainError) as caught:
                await client.put_json(InvocationContext("t", "u", "a"), "/sales/orders/123/void")
            assert caught.value.code == code
            assert caught.value.details == {
                "upstream_code": "A123", "trace_id": "trace-1", "http_status": status,
            }
        finally:
            await client.close()
    asyncio.run(scenario())


def test_same_name_references_can_select_second_id_without_keyword_lookup(tmp_path):
    from yuncyb.ports import YunCybReferenceSnapshot

    class References(CompleteSalesOrderApi):
        async def search_customers(self, context, keyword, limit=5, page=1):
            assert keyword == "同名客户"  # ID 不应当作名称关键词再搜索
            return YunCybReferenceSnapshot(
                options=({"id": "C-1", "code": "A", "name": "同名客户"},
                         {"id": "C-2", "code": "B", "name": "同名客户"}),
                total=2, page_num=1, page_size=limit,
            )

    tools = toolset(tmp_path, References())

    async def scenario():
        options = await tools.search_business_references("customer", "同名客户")
        assert [x["id"] for x in options["options"]] == ["C-1", "C-2"]
        ambiguous = await tools._resolve_reference("customer", "同名客户")
        assert len(ambiguous["candidates"]) == 2
        preview = await tools.preview_sales_order(
            **{**PREVIEW, "order_text": "土豆2斤", "customer": "C-2"},
        )
        assert preview["ready_to_submit"]
        assert preview["reference_resolutions"]["customer"]["selected"]["id"] == "C-2"
        payload, _ = tools.session.require_prepared_document("sales_order", preview["preview_id"])
        assert payload["customerId"] == "C-2"
        assert await tools._resolve_update_reference("customer", "C-2", "客户") == "C-2"
    asyncio.run(scenario())


def test_reference_identity_cache_is_isolated_and_copies_values(tmp_path):
    tools = toolset(tmp_path)
    option = {"id": "1", "name": "客户"}
    tools.session.remember_references("customer", (option,))
    option["name"] = "篡改"
    assert tools.session.reference_by_id("customer", "1")["name"] == "客户"
    assert tools.session.reference_by_id("handler", "1") is None
    other = toolset(tmp_path)
    assert other.session.reference_by_id("customer", "1") is None


@pytest.mark.parametrize("text,name,quantity,unit", [
    ("销售 1 本书本", "书本", 1, "本"),
    ("我要买一本书", "书", 1, "本"),
    ("书本 5 本", "书本", 5, "本"),
    ("土豆一百二十三斤", "土豆", 123, "斤"),
])
def test_spoken_order_has_explicit_quantity(text, name, quantity, unit):
    from yuncyb.session import parse_order_text
    line, = parse_order_text(text)
    assert (line.requested_name, line.quantity, line.unit) == (name, quantity, unit)


@pytest.mark.parametrize("text", ["土豆", "土豆若干斤", "土豆-2斤", "土豆一二斤", "火龙果猕猴桃各5斤"])
def test_uncertain_order_never_defaults_to_one(tmp_path, text):
    result = asyncio.run(toolset(tmp_path).preview_sales_order(
        **{**PREVIEW, "order_text": text},
    ))
    assert not result["ok"]
    assert result["error"]["code"] in {"erp_order_text_invalid", "erp_order_quantity_invalid"}


@pytest.mark.parametrize("limit", [0, -1, "2", True, 2.5])
def test_sync_limit_invalid_before_upstream(tmp_path, limit):
    tools = toolset(tmp_path, object())
    result = asyncio.run(tools.sync_products(limit))
    assert result["error"]["code"] == "erp_product_limit_invalid"


def test_search_loads_catalog_without_manual_sync(tmp_path):
    from yuncyb.ports import YunCybProductSnapshot
    class Catalog(CompleteSalesOrderApi):
        calls = 0
        async def fetch_products(self, context, limit=None):
            self.calls += 1
            return YunCybProductSnapshot(products=({"id": "P001", "name": "土豆", "unit": "斤"},))
    api = Catalog()
    tools = toolset(tmp_path, api)
    from yuncyb.catalog_state import TenantCatalogState
    tools.session._catalog_state = TenantCatalogState(600, {}, {})
    result = asyncio.run(tools.search_products(["土豆"]))
    assert result["results"][0]["product"]["product_id"] == "P001"
    assert api.calls == 1

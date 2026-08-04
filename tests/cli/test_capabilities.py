import asyncio
import json

from agentscope.agent import Agent
from mcp import types
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
import pytest

from erp_billing.adapters import (
    BusinessAuthenticatedJsonClient,
    ErpAuthenticatedHttpAdapter,
)
from erp_billing.config import ErpBillingSettings
from erp_billing.mcp_service import create_billing_mcp_service
from erp_billing.ports import BillingProductSnapshot
from erp_billing.runtime import create_billing_runtime
from erp_billing.session import ErpBillingSession
from erp_billing.toolset import BillingToolSet
from gjp_cli.agent import ERP_BILLING_AGENT_SPEC, build_agent
from gjp_cli.model_runtime import LLMSettings
from gjp_common.connections import (
    BusinessApiCredential,
    StaticBusinessApiCredentialProvider,
    business_api_url,
    normalize_business_api_base_url,
)
from gjp_common.context import InvocationContext, InvocationContextStore
from gjp_common.errors import DomainError
from gjp_common.mcp import (
    StaticIdentityResolver,
    StaticToolSetResolver,
    create_mcp_server,
)


def _billing_context() -> InvocationContext:
    return InvocationContext(
        tenant_id="tenant-b",
        subject_id="user-2",
        account_id="billing-account-5",
        session_id="order-7",
        scopes=frozenset({"billing:read"}),
    )


def _billing_settings() -> ErpBillingSettings:
    return ErpBillingSettings(
        product_catalog_path=None,
        alias_path=None,
        recommendation_score=0.6,
        use_default_fresh_aliases=False,
        category_path=None,
        use_default_categories=False,
    )


class FakeBillingApi:
    def __init__(self) -> None:
        self.contexts = []

    def fetch_products(self, context, limit=None):
        self.contexts.append(context)
        products = (
            {
                "ptypeid": "P001",
                "pusercode": "SP001",
                "pfullname": "马铃薯",
                "unit": "斤",
                "preprice1": 2.5,
            },
        )
        return BillingProductSnapshot(products=products[:limit] if limit is not None else products)


def _billing_toolset(api=None) -> BillingToolSet:
    session = ErpBillingSession.from_settings(
        _billing_settings(),
        allow_missing_catalog=True,
    )
    return BillingToolSet(
        session,
        api or FakeBillingApi(),
        InvocationContextStore(default=_billing_context()),
    )


def test_billing_runtime_binds_only_standard_tools_without_auth_parameters():
    api = FakeBillingApi()
    session = ErpBillingSession.from_settings(_billing_settings(), allow_missing_catalog=True)
    runtime = create_billing_runtime(session, api, _billing_context())

    assert {tool.name for tool in runtime.toolset.tools()} == {
        "sync_products",
        "search_products",
        "search_sales_order_options",
        "prepare_sales_order",
        "submit_sales_order",
    }
    for tool in runtime.toolset.tools():
        schema = json.dumps(tool.input_schema, ensure_ascii=False).casefold()
        assert "password" not in schema
        assert "access_token" not in schema
        assert "account_id" not in schema
        assert "invocation_context" not in schema


def test_reference_agent_binds_billing_toolset():
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

    agent = build_agent(_billing_toolset(), settings, ERP_BILLING_AGENT_SPEC)

    assert isinstance(agent, Agent)
    assert agent.name == "ErpBillingAgent"


def test_billing_mcp_reuses_agentscope_schema_and_resolves_identity_per_call():
    asyncio.run(_assert_billing_mcp_schema_and_identity())


async def _assert_billing_mcp_schema_and_identity():
    schema_toolset = _billing_toolset()
    api = FakeBillingApi()
    runtime_toolset = _billing_toolset(api)
    identity = _billing_context()
    server = create_mcp_server(
        "billing-tools",
        schema_toolset,
        StaticIdentityResolver(identity),
        StaticToolSetResolver(runtime_toolset),
    )

    listed = await server.request_handlers[types.ListToolsRequest](types.ListToolsRequest())
    listed_tools = listed.root.tools
    assert {tool.name for tool in listed_tools} == {
        "sync_products",
        "search_products",
        "search_sales_order_options",
        "prepare_sales_order",
        "submit_sales_order",
    }
    sync_tool = next(tool for tool in listed_tools if tool.name == "sync_products")
    assert sync_tool.inputSchema == schema_toolset.get("sync_products").input_schema

    token = request_ctx.set(
        RequestContext(
            request_id="request-1",
            meta=None,
            session=None,
            lifespan_context=None,
        ),
    )
    try:
        called = await server.request_handlers[types.CallToolRequest](
            types.CallToolRequest(
                params=types.CallToolRequestParams(
                    name="sync_products",
                    arguments={"limit": 1},
                ),
            ),
        )
    finally:
        request_ctx.reset(token)

    assert called.root.isError is False
    assert called.root.structuredContent["productCount"] == 1
    assert api.contexts == [identity]


def test_billing_mcp_service_rejects_non_billing_toolset():
    context = _billing_context()
    with pytest.raises(TypeError, match="开单服务只能发布"):
        create_billing_mcp_service(
            object(),
            StaticIdentityResolver(context),
            StaticToolSetResolver(object()),
        )


def test_fixed_business_url_validates_https_and_credential_redacts_secret():
    credential = BusinessApiCredential(kind="bearer", value="business-secret")

    assert normalize_business_api_base_url("https://tenant.erp.example.test/app/") == (
        "https://tenant.erp.example.test/app"
    )
    assert business_api_url("https://tenant.erp.example.test/app", "/product/page") == (
        "https://tenant.erp.example.test/app/product/page"
    )
    assert "business-secret" not in repr(credential)
    with pytest.raises(DomainError, match="HTTPS"):
        normalize_business_api_base_url("http://127.0.0.1/internal")


def test_authenticated_business_client_injects_server_side_bearer(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"code":"A00000","data":{"list":[],"total":0}}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["method"] = request.method
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = StaticBusinessApiCredentialProvider(
        BusinessApiCredential(kind="bearer", value="business-secret"),
    )
    client = BusinessAuthenticatedJsonClient(
        "https://tenant.erp.example.test/aicyberp-api",
        provider,
        timeout_seconds=12,
    )

    response = client.get_json(
        _billing_context(),
        "/product/page",
        {"pageNum": 1, "pageSize": 20},
    )

    assert response["code"] == "A00000"
    assert captured == {
        "url": "https://tenant.erp.example.test/aicyberp-api/product/page?pageNum=1&pageSize=20",
        "authorization": "Bearer business-secret",
        "method": "GET",
        "timeout": 12,
    }


def test_authenticated_business_client_posts_sales_order_with_server_side_bearer(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"code":"A00000","data":"SO-1"}'

    def fake_urlopen(request, timeout):
        captured.update(
            url=request.full_url,
            authorization=request.headers["Authorization"],
            content_type=request.headers["Content-type"],
            method=request.method,
            payload=json.loads(request.data.decode("utf-8")),
            timeout=timeout,
        )
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = StaticBusinessApiCredentialProvider(
        BusinessApiCredential(kind="bearer", value="business-secret"),
    )
    client = BusinessAuthenticatedJsonClient(
        "https://tenant.erp.example.test/aicyberp-api",
        provider,
        timeout_seconds=12,
    )

    response = client.post_json(
        _billing_context(),
        "/sales/orders",
        {"id": 0, "saveType": 2},
    )

    assert response == {"code": "A00000", "data": "SO-1"}
    assert captured == {
        "url": "https://tenant.erp.example.test/aicyberp-api/sales/orders",
        "authorization": "Bearer business-secret",
        "content_type": "application/json",
        "method": "POST",
        "payload": {"id": 0, "saveType": 2},
        "timeout": 12,
    }


def test_erp_http_adapter_delegates_authentication_to_product():
    calls = []
    context = _billing_context()

    class AuthenticatedHttp:
        def get_json(self, current, path, params=None):
            calls.append((current, path, params))
            page_num = params["pageNum"]
            rows = {
                1: [
                    {
                        "id": "P001",
                        "code": "SP001",
                        "name": "马铃薯",
                        "unit": "斤",
                        "salesPrice": 2.5,
                        "stockQuantity": 8,
                        "status": 1,
                    },
                    {
                        "id": "P002",
                        "code": "SP002",
                        "name": "番茄",
                        "unit": "斤",
                        "status": 1,
                    },
                ],
                2: [
                    {
                        "id": "P003",
                        "code": "SP003",
                        "name": "牛肉",
                        "unit": "斤",
                        "status": 1,
                    },
                ],
            }[page_num]
            return {
                "code": "A00000",
                "data": {"total": 3, "pageNum": page_num, "pageSize": 2, "list": rows},
            }

    snapshot = ErpAuthenticatedHttpAdapter(AuthenticatedHttp(), page_size=2).fetch_products(context)

    potato = next(item for item in snapshot.products if item["productId"] == "P001")
    assert potato["name"] == "马铃薯"
    assert potato["price"] == 2.5
    assert potato["stock"] == 8
    assert len(snapshot.products) == 3
    assert calls[0][0] == context
    assert calls[0][1] == "/product/page"
    assert calls == [
        (context, "/product/page", {"pageNum": 1, "pageSize": 2, "status": 1}),
        (context, "/product/page", {"pageNum": 2, "pageSize": 2, "status": 1}),
    ]
    assert "password" not in json.dumps(calls[0][2]).casefold()


def test_erp_http_adapter_uses_real_sales_order_reference_and_create_endpoints():
    calls = []
    context = _billing_context()

    class AuthenticatedHttp:
        def get_json(self, current, path, params=None):
            calls.append(("GET", current, path, params))
            names = {
                "/customer/page": ("CUS-1", "C001", "客户甲"),
                "/warehouse/page": ("WH-1", "W001", "一号仓"),
                "/staff/page": ("STAFF-1", "S001", "张三"),
            }
            option_id, code, name = names[path]
            return {
                "code": "A00000",
                "data": {
                    "total": 1,
                    "list": [
                        {
                            "id": option_id,
                            "code": code,
                            "name": name,
                            "isDefault": True,
                            "status": 1,
                        },
                    ],
                },
            }

        def post_json(self, current, path, payload):
            calls.append(("POST", current, path, payload))
            return {"code": "A00000", "data": "SO-1"}

    adapter = ErpAuthenticatedHttpAdapter(AuthenticatedHttp())

    customer = adapter.search_customers(context, "客户甲")
    warehouse = adapter.search_warehouses(context, "一号仓")
    handler = adapter.search_staff(context, "张三")
    created = adapter.create_sales_order(context, {"id": 0, "saveType": 2})

    assert customer.options[0]["id"] == "CUS-1"
    assert warehouse.options[0]["id"] == "WH-1"
    assert handler.options[0]["id"] == "STAFF-1"
    assert created.order_id == "SO-1"
    assert calls == [
        (
            "GET",
            context,
            "/customer/page",
            {"pageNum": 1, "pageSize": 10, "status": 1, "keyword": "客户甲"},
        ),
        (
            "GET",
            context,
            "/warehouse/page",
            {"pageNum": 1, "pageSize": 10, "status": 1, "keyword": "一号仓"},
        ),
        (
            "GET",
            context,
            "/staff/page",
            {"pageNum": 1, "pageSize": 10, "status": 1, "keyword": "张三"},
        ),
        ("POST", context, "/sales/orders", {"id": 0, "saveType": 2}),
    ]


def test_billing_toolset_syncs_and_creates_confirmed_product():
    asyncio.run(_assert_billing_toolset_syncs_and_creates_draft())


async def _assert_billing_toolset_syncs_and_creates_draft():
    api = FakeBillingApi()
    toolset = _billing_toolset(api)

    synced = await toolset.get("sync_products")(limit=None)
    assert json.loads(synced.content[0].text)["ok"] is True

    result = await toolset.get("prepare_sales_order")(order_text="马铃薯2斤")
    payload = json.loads(result.content[0].text)

    assert {
        "ok": payload["ok"],
        "confirmedProducts": payload["confirmedProducts"],
        "recommendedProducts": payload["recommendedProducts"],
        "unmatchedProducts": payload["unmatchedProducts"],
    } == {
        "ok": True,
        "confirmedProducts": [
            {
                "lineId": "L001",
                "ptypeid": "P001",
                "pfullname": "马铃薯",
                "unit": "斤",
                "quantity": 2,
            },
        ],
        "recommendedProducts": [],
        "unmatchedProducts": [],
    }
    assert api.contexts == [_billing_context()]

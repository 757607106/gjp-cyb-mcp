"""验证服务直接接受 ERP JWT 作为 MCP Bearer，不再需要 /test-auth/token 换票。"""

import base64
import json

from starlette.testclient import TestClient

from gjp_cli.billing_validation import create_billing_validation_app
from gjp_cli.validation import ValidationCredentialStore


FIXED_ERP_URL = "https://test-ai.yuncyb.com/aicyberp-api"


def _make_jwt(tenant_id=1, login_id=20321082350030080703):
    """生成测试用 ERP JWT（不验签，仅用于测试身份解析）。"""
    header = base64.urlsafe_b64encode(
        json.dumps({"typ": "JWT", "alg": "HS256"}).encode(),
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"tenantId": tenant_id, "loginId": login_id}).encode(),
    ).rstrip(b"=").decode()
    return "%s.%s.test-signature" % (header, payload)


def _mcp_request(client, method, params=None, bearer=None, request_id=1):
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    if bearer:
        headers["authorization"] = "Bearer " + bearer
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        },
        headers=headers,
    )


class FakeErpHttpClient:
    def __init__(self):
        self.calls = []

    def get_json(self, context, url, params=None):
        self.calls.append((context, url, params))
        return {
            "code": "A00000",
            "data": {
                "total": 1,
                "pageNum": 1,
                "pageSize": params["pageSize"],
                "list": [
                    {
                        "id": "P001",
                        "code": "SP001",
                        "name": "马铃薯",
                        "unit": "斤",
                        "status": 1,
                    },
                ],
            },
        }


def test_billing_validation_accepts_erp_jwt_directly(tmp_path):
    """ERP JWT 直接作为 MCP Bearer，无需 /test-auth/token 换票。"""
    store = ValidationCredentialStore(ttl_seconds=600)
    http = FakeErpHttpClient()
    app = create_billing_validation_app(
        base_url=FIXED_ERP_URL,
        http_client=http,
        store=store,
    )

    jwt = _make_jwt(tenant_id=1, login_id=20321082350030080703)

    with TestClient(app) as client:
        # /test-auth/token 端点已移除
        assert client.post("/test-auth/token", json={}).status_code == 404

        listed = _mcp_request(client, "tools/list", bearer=jwt, request_id=1)
        assert {
            tool["name"]
            for tool in listed.json()["result"]["tools"]
        } == {
            "sync_products",
            "list_products",
            "search_products",
            "search_sales_order_options",
            "prepare_sales_order",
            "submit_sales_order",
            "get_sales_order_detail",
            "list_sales_orders",
            "void_sales_order",
            "modify_sales_order",
        }
        schema = json.dumps(listed.json(), ensure_ascii=False).casefold()
        assert "password" not in schema
        assert "access_token" not in schema
        assert "base_url" not in schema

        called = _mcp_request(
            client,
            "tools/call",
            {"name": "sync_products", "arguments": {"limit": 1}},
            bearer=jwt,
            request_id=2,
        )

    payload = called.json()["result"]["structuredContent"]
    assert payload["ok"] is True
    assert payload["productCount"] == 1
    assert http.calls[0][0].tenant_id == "1"
    assert http.calls[0][0].subject_id == "20321082350030080703"
    assert http.calls[0][1] == "/product/page"
    assert http.calls[0][2] == {"pageNum": 1, "pageSize": 1, "status": 1}
    assert list(tmp_path.iterdir()) == []


class TenantAwareErpHttpClient:
    """按租户返回不同商品，用于验证固定 URL 下凭据会话仍互不污染。"""

    def get_json(self, context, url, params=None):
        name = "土豆%s" % context.tenant_id[-1].upper()
        return {
            "code": "A00000",
            "data": {
                "total": 1,
                "pageNum": 1,
                "pageSize": params["pageSize"],
                "list": [
                    {
                        "id": "P-" + context.tenant_id,
                        "code": "SP-" + context.tenant_id,
                        "name": name,
                        "unit": "斤",
                        "status": 1,
                    },
                ],
            },
        }


def test_billing_validation_isolates_catalogs_between_tenants():
    app = create_billing_validation_app(
        base_url=FIXED_ERP_URL,
        http_client=TenantAwareErpHttpClient(),
    )

    jwt_a = _make_jwt(tenant_id=11, login_id=1001)
    jwt_b = _make_jwt(tenant_id=22, login_id=2002)

    with TestClient(app) as client:
        for jwt, request_id in ((jwt_a, 1), (jwt_b, 2)):
            synced = _mcp_request(
                client,
                "tools/call",
                {"name": "sync_products", "arguments": {"limit": 1}},
                bearer=jwt,
                request_id=request_id,
            )
            assert synced.json()["result"]["structuredContent"]["ok"] is True

        results = {}
        for jwt, request_id in ((jwt_a, 3), (jwt_b, 4)):
            searched = _mcp_request(
                client,
                "tools/call",
                {"name": "search_products", "arguments": {"keywords": ["土豆"]}},
                bearer=jwt,
                request_id=request_id,
            )
            results[jwt] = json.dumps(
                searched.json()["result"]["structuredContent"],
                ensure_ascii=False,
            )

    assert "土豆1" in results[jwt_a]
    assert "土豆2" not in results[jwt_a]
    assert "土豆2" in results[jwt_b]
    assert "土豆1" not in results[jwt_b]


def test_expiring_toolset_cache_evicts_idle_sessions(monkeypatch):
    from gjp_common.context import InvocationContext
    from gjp_cli import validation as validation_module
    from gjp_cli.validation import ExpiringToolSetCache

    clock = {"now": 0.0}
    monkeypatch.setattr(validation_module.time, "monotonic", lambda: clock["now"])
    cache = ExpiringToolSetCache(ttl_seconds=10)
    context = InvocationContext(
        tenant_id="tenant-a",
        subject_id="user-a",
        account_id="account-a",
        session_id="session-a",
    )

    first = cache.get_or_create(context, object)
    assert cache.get_or_create(context, object) is first

    clock["now"] = 9.0
    assert cache.get_or_create(context, object) is first
    clock["now"] = 18.0
    assert cache.get_or_create(context, object) is first

    clock["now"] = 29.0
    rebuilt = cache.get_or_create(context, object)
    assert rebuilt is not first

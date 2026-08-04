import json

from starlette.testclient import TestClient

from gjp_cli.billing_validation import create_billing_validation_app
from gjp_cli.validation import ValidationCredentialStore


FIXED_ERP_URL = "https://test-ai.yuncyb.com/aicyberp-api"


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


def test_billing_validation_is_token_only_and_passes_bearer_to_mcp_runtime(tmp_path):
    store = ValidationCredentialStore(ttl_seconds=600)
    http = FakeErpHttpClient()
    app = create_billing_validation_app(
        base_url=FIXED_ERP_URL,
        http_client=http,
        store=store,
    )

    with TestClient(app) as client:
        assert client.get("/test-auth/captcha").status_code == 404
        assert client.post("/test-auth/login", json={}).status_code == 404

        issued = client.post(
            "/test-auth/token",
            json={
                "upstreamToken": "Bearer browser-upstream-secret",
                "tenant_id": "tenant-token",
                "subject_id": "user-token",
                "session_id": "session-token",
            },
        )
        assert issued.status_code == 200
        issued_payload = issued.json()
        bearer = issued_payload["accessToken"]
        assert bearer != "browser-upstream-secret"
        assert issued_payload["scopes"] == ["billing:read", "billing:write"]
        assert "browser-upstream-secret" not in json.dumps(issued_payload, ensure_ascii=False)

        credential = store.require_bearer(bearer)
        assert credential.upstream_bearer == "browser-upstream-secret"
        assert credential.context.tenant_id == "tenant-token"
        assert not hasattr(credential, "app_base_url")

        listed = _mcp_request(client, "tools/list", bearer=bearer, request_id=2)
        assert {
            tool["name"]
            for tool in listed.json()["result"]["tools"]
        } == {
            "sync_products",
            "search_products",
            "search_sales_order_options",
            "prepare_sales_order",
            "submit_sales_order",
        }
        schema = json.dumps(listed.json(), ensure_ascii=False).casefold()
        assert "password" not in schema
        assert "captcha" not in schema
        assert "access_token" not in schema
        assert "base_url" not in schema

        called = _mcp_request(
            client,
            "tools/call",
            {"name": "sync_products", "arguments": {"limit": 1}},
            bearer=bearer,
            request_id=3,
        )

    payload = called.json()["result"]["structuredContent"]
    assert payload["ok"] is True
    assert payload["productCount"] == 1
    assert http.calls[0][0].session_id == "session-token"
    assert http.calls[0][1] == "/product/page"
    assert http.calls[0][2] == {"pageNum": 1, "pageSize": 1, "status": 1}
    assert list(tmp_path.iterdir()) == []


def test_billing_validation_rejects_dynamic_url_in_token_request():
    app = create_billing_validation_app(
        base_url=FIXED_ERP_URL,
        http_client=FakeErpHttpClient(),
    )

    with TestClient(app) as client:
        response = client.post(
            "/test-auth/token",
            json={
                "upstreamToken": "browser-upstream-secret",
                "appBaseUrl": "https://another.example.test/api",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "ERP_LIVE_CONFIG_INVALID"


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

    with TestClient(app) as client:
        bearers = {}
        for tenant in ("tenant-a", "tenant-b"):
            issued = client.post(
                "/test-auth/token",
                json={
                    "upstreamToken": "token-" + tenant,
                    "tenant_id": tenant,
                    "session_id": "session-" + tenant,
                },
            )
            assert issued.status_code == 200
            bearers[tenant] = issued.json()["accessToken"]

        for tenant, request_id in (("tenant-a", 2), ("tenant-b", 3)):
            synced = _mcp_request(
                client,
                "tools/call",
                {"name": "sync_products", "arguments": {"limit": 1}},
                bearer=bearers[tenant],
                request_id=request_id,
            )
            assert synced.json()["result"]["structuredContent"]["ok"] is True

        results = {}
        for tenant, request_id in (("tenant-a", 4), ("tenant-b", 5)):
            searched = _mcp_request(
                client,
                "tools/call",
                {"name": "search_products", "arguments": {"keywords": ["土豆"]}},
                bearer=bearers[tenant],
                request_id=request_id,
            )
            results[tenant] = json.dumps(
                searched.json()["result"]["structuredContent"],
                ensure_ascii=False,
            )

    assert "土豆A" in results["tenant-a"]
    assert "土豆B" not in results["tenant-a"]
    assert "土豆B" in results["tenant-b"]
    assert "土豆A" not in results["tenant-b"]


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

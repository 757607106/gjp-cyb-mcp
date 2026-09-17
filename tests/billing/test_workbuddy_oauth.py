"""WorkBuddy OAuth 发现、PKCE、绑定与凭据隔离测试。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from erp_billing.workbuddy_oauth import (
    EncryptedSqliteOAuthStore,
    ErpBinding,
    WorkBuddyCredentialProvider,
    WorkBuddyIdentityResolver,
    WorkBuddyOAuthProtectionMiddleware,
    WorkBuddyOAuthProvider,
    WorkBuddyOAuthSettings,
    create_workbuddy_oauth_routes,
)
from erp_billing.workbuddy_app import create_workbuddy_billing_app


def _settings(database_path: str = ":memory:") -> WorkBuddyOAuthSettings:
    return WorkBuddyOAuthSettings(
        public_base_url="https://mcp.example.test",
        connector_source="gjp-erp-billing",
        database_path=database_path,
        encryption_key=Fernet.generate_key().decode("ascii"),
    )


async def _validate_key(api_key: str) -> ErpBinding:
    if api_key != "test-erp-key":
        raise ValueError("invalid test key")
    return ErpBinding(
        tenant_id="tenant-1",
        subject_id="user-1",
        account_id="account-1",
        credential_kind="api_key",
        credential_value=api_key,
    )


def _provider(settings: WorkBuddyOAuthSettings | None = None):
    effective = settings or _settings()
    store = EncryptedSqliteOAuthStore(effective.database_path, effective.encryption_key)
    provider = WorkBuddyOAuthProvider(effective, store, _validate_key)
    return provider, store


def _pkce(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _register(client: TestClient, settings: WorkBuddyOAuthSettings) -> str:
    response = client.post(
        "/oauth/register",
        json={
            "redirect_uris": [settings.private_redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": "billing:read billing:write",
            "client_name": "WorkBuddy Test",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["redirect_uris"] == [settings.private_redirect_uri]
    assert "client_secret" not in payload
    return payload["client_id"]


def _authorize_and_bind(
    client: TestClient,
    settings: WorkBuddyOAuthSettings,
    client_id: str,
    verifier: str,
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": settings.private_redirect_uri,
            "scope": "billing:read billing:write",
            "state": "state-123",
            "code_challenge": _pkce(verifier),
            "code_challenge_method": "S256",
            "resource": settings.resource_url,
        }
    )
    authorize = client.get("/oauth/authorize?" + query, follow_redirects=False)
    assert authorize.status_code == 302
    binding_url = urlparse(authorize.headers["location"])
    request_id = parse_qs(binding_url.query)["request_id"][0]

    page = client.get(f"/oauth/bind?request_id={request_id}")
    assert page.status_code == 200
    assert "ERP AI Token" in page.text
    assert "test-erp-key" not in page.text

    bound = client.post(
        "/oauth/bind",
        data={"request_id": request_id, "api_key": "test-erp-key"},
        follow_redirects=False,
    )
    assert bound.status_code == 302
    callback = urlparse(bound.headers["location"])
    callback_query = parse_qs(callback.query)
    assert callback.scheme == "workbuddy"
    assert callback_query["state"] == ["state-123"]
    return callback_query["code"][0]


def _exchange(
    client: TestClient,
    settings: WorkBuddyOAuthSettings,
    client_id: str,
    code: str,
    verifier: str,
):
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": settings.private_redirect_uri,
            "code_verifier": verifier,
            "resource": settings.resource_url,
        },
    )


def test_workbuddy_oauth_discovery_and_full_pkce_flow():
    settings = _settings()
    provider, store = _provider(settings)
    client = TestClient(Starlette(routes=create_workbuddy_oauth_routes(provider)))
    try:
        metadata = client.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        assert metadata.json()["token_endpoint_auth_methods_supported"] == ["none"]
        assert metadata.json()["code_challenge_methods_supported"] == ["S256"]

        protected = client.get("/.well-known/oauth-protected-resource/mcp")
        assert protected.status_code == 200
        assert protected.json()["resource"] == settings.resource_url

        client_id = _register(client, settings)
        verifier = "v" * 64
        code = _authorize_and_bind(client, settings, client_id, verifier)
        token_response = _exchange(client, settings, client_id, code, verifier)
        assert token_response.status_code == 200, token_response.text
        tokens = token_response.json()
        assert tokens["access_token"].startswith("wb_at_")
        assert tokens["refresh_token"].startswith("wb_rt_")
        assert tokens["scope"] == "billing:read billing:write"

        access = asyncio.run(provider.load_access_token(tokens["access_token"]))
        assert access is not None
        assert access.subject == "user-1"
        assert access.resource == settings.resource_url

        replay = _exchange(client, settings, client_id, code, verifier)
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_grant"

        refreshed = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
                "scope": "billing:read billing:write",
                "resource": settings.resource_url,
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["refresh_token"] != tokens["refresh_token"]
        assert asyncio.run(provider.load_access_token(tokens["access_token"])) is None

        reused_refresh = client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
            },
        )
        assert reused_refresh.status_code == 400
        assert reused_refresh.json()["error"] == "invalid_grant"
    finally:
        store.close()


def test_workbuddy_rejects_non_public_client_and_untrusted_redirect():
    settings = _settings()
    provider, store = _provider(settings)
    client = TestClient(Starlette(routes=create_workbuddy_oauth_routes(provider)))
    try:
        secret_client = client.post(
            "/oauth/register",
            json={
                "redirect_uris": [settings.private_redirect_uri],
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert secret_client.status_code == 400
        assert secret_client.json()["error"] == "invalid_client_metadata"

        bad_redirect = client.post(
            "/oauth/register",
            json={
                "redirect_uris": ["https://attacker.example/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert bad_redirect.status_code == 400
        assert bad_redirect.json()["error"] == "invalid_redirect_uri"
    finally:
        store.close()


def test_pkce_failure_does_not_consume_authorization_code():
    settings = _settings()
    provider, store = _provider(settings)
    client = TestClient(Starlette(routes=create_workbuddy_oauth_routes(provider)))
    try:
        client_id = _register(client, settings)
        verifier = "c" * 64
        code = _authorize_and_bind(client, settings, client_id, verifier)

        rejected = _exchange(client, settings, client_id, code, "wrong-verifier")
        assert rejected.status_code == 400
        assert rejected.json()["error"] == "invalid_grant"

        accepted = _exchange(client, settings, client_id, code, verifier)
        assert accepted.status_code == 200
    finally:
        store.close()


def test_http_protection_and_identity_keep_mcp_and_erp_tokens_separate():
    settings = _settings()
    provider, store = _provider(settings)
    store.save_binding(
        ErpBinding(
            tenant_id="tenant-1",
            subject_id="user-1",
            account_id="account-1",
            credential_kind="api_key",
            credential_value="test-erp-key",
        )
    )
    tokens = provider._issue_token_pair(  # noqa: SLF001 - 精确构造中间件测试身份。
        client_id="client-1",
        scopes=["billing:read", "billing:write"],
        subject="user-1",
        resource=settings.resource_url,
    )

    async def endpoint(request):
        access = request.scope.get("workbuddy_access_token")
        return JSONResponse({"subject": access.subject if access else None})

    protected = WorkBuddyOAuthProtectionMiddleware(
        Starlette(routes=[Route("/mcp", endpoint, methods=["POST"])]),
        provider,
    )
    client = TestClient(protected)
    try:
        unauthorized = client.post("/mcp")
        assert unauthorized.status_code == 401
        assert "resource_metadata=" in unauthorized.headers["www-authenticate"]

        authorized = client.post(
            "/mcp",
            headers={"Authorization": "Bearer " + tokens.access_token},
        )
        assert authorized.status_code == 200
        assert authorized.json() == {"subject": "user-1"}

        access = asyncio.run(provider.load_access_token(tokens.access_token))
        request = SimpleNamespace(
            scope={"workbuddy_access_token": access},
            headers={"x-conversation-id": "conversation-a"},
        )
        context = asyncio.run(WorkBuddyIdentityResolver(provider).resolve(SimpleNamespace(request=request)))
        assert context.tenant_id == "tenant-1"
        assert context.account_id == "account-1"
        assert context.session_id.startswith("workbuddy-user-1-")

        credential = WorkBuddyCredentialProvider(provider).resolve(context)
        assert credential.kind == "api_key"
        assert credential.value == "test-erp-key"
        assert tokens.access_token != credential.value
    finally:
        store.close()


def test_sqlite_store_does_not_write_plaintext_credentials(tmp_path):
    database_path = tmp_path / "workbuddy.db"
    settings = _settings(str(database_path))
    store = EncryptedSqliteOAuthStore(settings.database_path, settings.encryption_key)
    try:
        store.save_binding(
            ErpBinding(
                tenant_id="tenant-secret",
                subject_id="subject-secret",
                account_id="account-secret",
                credential_kind="api_key",
                credential_value="never-write-this-key",
            )
        )
    finally:
        store.close()

    raw = database_path.read_bytes()
    assert b"never-write-this-key" not in raw
    assert b"subject-secret" not in raw


def test_workbuddy_app_keeps_health_public_and_mcp_protected():
    settings = _settings()
    store = EncryptedSqliteOAuthStore(settings.database_path, settings.encryption_key)
    app = create_workbuddy_billing_app(
        oauth_settings=settings,
        oauth_store=store,
        binding_validator=_validate_key,
    )
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"ok": True}
        assert client.get("/.well-known/oauth-authorization-server").status_code == 200
        protected = client.post("/mcp")
        assert protected.status_code == 401
        assert settings.protected_resource_metadata_url in protected.headers["www-authenticate"]


def test_workbuddy_connector_assets_keep_auth_modes_separate():
    root = Path(__file__).resolve().parents[2] / "integrations" / "workbuddy"
    oauth_meta = json.loads((root / "gjp-erp-billing" / "connector-meta.json").read_text())
    oauth_mcp = json.loads((root / "gjp-erp-billing" / "mcp.json").read_text())
    token_meta = json.loads((root / "gjp-erp-billing-token" / "connector-meta.json").read_text())
    token_mcp = json.loads((root / "gjp-erp-billing-token" / "mcp.json").read_text())

    assert oauth_meta["source"] == "gjp-erp-billing"
    assert "auth_mode" not in oauth_meta
    assert "headers" not in oauth_mcp["mcpServers"]["erp-billing"]
    assert token_meta["source"] == "gjp-erp-billing-token"
    assert token_meta["auth_mode"] == "token"
    assert token_mcp["mcpServers"]["erp-billing"]["headers"] == {"X-API-Key": "${ERP_API_KEY}"}

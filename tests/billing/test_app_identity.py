"""验证开单服务按环境选择鉴权强度：local 宽松、production 强制 HS256 验签。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from erp_billing.app import (
    ApiKeyIdentityResolver,
    DirectJwtIdentityResolver,
    SessionCredentialStore,
    VerifiedJwtIdentityResolver,
    _CompositeIdentityResolver,
    _create_identity_resolver,
)
from gjp_common.connections import BusinessApiCredential
from gjp_common.context import InvocationContext
from gjp_common.errors import DomainError

SECRET = "unit-test-hs256-secret"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_token(payload: dict, secret: str = SECRET, alg: str = "HS256") -> str:
    header_b64 = _b64url(json.dumps({"alg": alg, "typ": "JWT"}).encode("utf-8"))
    payload_b64 = _b64url(json.dumps(payload).encode("utf-8"))
    signing_input = (header_b64 + "." + payload_b64).encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return header_b64 + "." + payload_b64 + "." + _b64url(signature)


def _mcp_context(token: str) -> SimpleNamespace:
    return SimpleNamespace(
        request=SimpleNamespace(headers={"authorization": "Bearer " + token}),
    )


def _valid_payload() -> dict:
    return {
        "tenantId": "tenant-1",
        "loginId": "user-1",
        "exp": int(time.time()) + 3600,
    }


def test_verified_resolver_accepts_signed_token() -> None:
    store = SessionCredentialStore()
    resolver = VerifiedJwtIdentityResolver(store, SECRET)

    context = resolver.resolve(_mcp_context(_make_token(_valid_payload())))

    assert context.tenant_id == "tenant-1"
    assert context.subject_id == "user-1"
    assert store.resolve(context).value == _make_token(_valid_payload())


def test_verified_resolver_rejects_tampered_payload() -> None:
    resolver = VerifiedJwtIdentityResolver(SessionCredentialStore(), SECRET)
    header_b64, _, signature_b64 = _make_token(_valid_payload()).split(".")
    forged_payload = _b64url(json.dumps({"tenantId": "evil", "loginId": "evil"}).encode("utf-8"))
    forged_token = header_b64 + "." + forged_payload + "." + signature_b64

    with pytest.raises(DomainError) as excinfo:
        resolver.resolve(_mcp_context(forged_token))

    assert excinfo.value.code == "mcp_unauthorized"


def test_verified_resolver_rejects_wrong_secret() -> None:
    resolver = VerifiedJwtIdentityResolver(SessionCredentialStore(), SECRET)
    token = _make_token(_valid_payload(), secret="another-secret")

    with pytest.raises(DomainError) as excinfo:
        resolver.resolve(_mcp_context(token))

    assert excinfo.value.code == "mcp_unauthorized"


def test_verified_resolver_rejects_expired_token() -> None:
    resolver = VerifiedJwtIdentityResolver(SessionCredentialStore(), SECRET)
    expired = dict(_valid_payload(), exp=int(time.time()) - 60)

    with pytest.raises(DomainError) as excinfo:
        resolver.resolve(_mcp_context(_make_token(expired)))

    assert excinfo.value.code == "mcp_unauthorized"


def test_verified_resolver_rejects_non_hs256_algorithm() -> None:
    resolver = VerifiedJwtIdentityResolver(SessionCredentialStore(), SECRET)
    token = _make_token(_valid_payload(), alg="none")

    with pytest.raises(DomainError) as excinfo:
        resolver.resolve(_mcp_context(token))

    assert excinfo.value.code == "mcp_unauthorized"


def test_verified_resolver_requires_secret() -> None:
    with pytest.raises(DomainError) as excinfo:
        VerifiedJwtIdentityResolver(SessionCredentialStore(), "   ")

    assert excinfo.value.code == "mcp_unauthorized"


def test_local_resolver_accepts_unsigned_token() -> None:
    header_b64 = _b64url(json.dumps({"alg": "none"}).encode("utf-8"))
    payload_b64 = _b64url(json.dumps({"tenantId": "t", "loginId": "u"}).encode("utf-8"))
    resolver = DirectJwtIdentityResolver(SessionCredentialStore())

    context = resolver.resolve(_mcp_context(header_b64 + "." + payload_b64 + ".sig"))

    assert context.tenant_id == "t"


def test_local_resolver_strips_duplicate_bearer_prefix() -> None:
    """客户端误传 Bearer Bearer <token> 时应剥离多余前缀，下游拿到纯 token。"""
    header_b64 = _b64url(json.dumps({"alg": "none"}).encode("utf-8"))
    payload_b64 = _b64url(json.dumps({"tenantId": "t", "loginId": "u"}).encode("utf-8"))
    jwt = header_b64 + "." + payload_b64 + ".sig"
    mcp_context = SimpleNamespace(
        request=SimpleNamespace(headers={"authorization": "Bearer Bearer " + jwt}),
    )
    store = SessionCredentialStore()
    resolver = DirectJwtIdentityResolver(store)

    context = resolver.resolve(mcp_context)

    assert context.tenant_id == "t"
    assert store.resolve(context).value == jwt


def test_verified_resolver_strips_duplicate_bearer_prefix() -> None:
    """验签器同样能剥离客户端误传的多余 Bearer 前缀。"""
    store = SessionCredentialStore()
    resolver = VerifiedJwtIdentityResolver(store, SECRET)
    token = _make_token(_valid_payload())
    mcp_context = SimpleNamespace(
        request=SimpleNamespace(headers={"authorization": "Bearer Bearer " + token}),
    )

    context = resolver.resolve(mcp_context)

    assert context.tenant_id == "tenant-1"
    assert store.resolve(context).value == token


def test_composition_root_routes_bearer_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """组合根按环境构造 Bearer 解析器：生产验签，本地不验签。"""
    monkeypatch.setenv("ERP_BILLING_JWT_SECRET", SECRET)
    token = _make_token(_valid_payload())

    monkeypatch.setenv("GJP_ENV", "production")
    store_prod = SessionCredentialStore()
    resolver_prod = _create_identity_resolver(store_prod)
    context = resolver_prod.resolve(_mcp_context(token))
    assert context.tenant_id == "tenant-1"
    assert store_prod.resolve(context).value == token

    monkeypatch.delenv("GJP_ENV", raising=False)
    store_local = SessionCredentialStore()
    resolver_local = _create_identity_resolver(store_local)
    context_local = resolver_local.resolve(_mcp_context(token))
    assert context_local.tenant_id == "tenant-1"


def test_bearer_store_rejects_expired_token() -> None:
    """过期 Token 在 resolve 时返回 business_reauth_required，复用现有错误码。"""
    store = SessionCredentialStore()
    context = InvocationContext(
        tenant_id="t",
        subject_id="u",
        account_id="t",
        session_id="billing-u",
    )
    expired_payload = {"tenantId": "t", "loginId": "u", "exp": int(time.time()) - 60}
    expired_token = _make_token(expired_payload)
    store.register(context, BusinessApiCredential(kind="bearer", value=expired_token))

    with pytest.raises(DomainError) as excinfo:
        store.resolve(context)

    assert excinfo.value.code == "business_reauth_required"


def test_bearer_store_accepts_unexpired_token() -> None:
    """未过期的 Token 正常返回。"""
    store = SessionCredentialStore()
    context = InvocationContext(
        tenant_id="t",
        subject_id="u",
        account_id="t",
        session_id="billing-u",
    )
    token = _make_token(_valid_payload())
    store.register(context, BusinessApiCredential(kind="bearer", value=token))

    assert store.resolve(context).value == token


def _mcp_api_key_context(api_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        request=SimpleNamespace(headers={"x-api-key": api_key}),
    )


def test_api_key_resolver_accepts_any_key() -> None:
    """key 等同于 token：任意 key 都被接受，用 key 构造会话隔离身份。"""
    store = SessionCredentialStore()
    resolver = ApiKeyIdentityResolver(store)

    context = resolver.resolve(_mcp_api_key_context("ak_any_key"))

    assert context.tenant_id == "ak_any_key"
    assert context.subject_id == "ak_any_key"
    credential = store.resolve(context)
    assert credential.kind == "api_key"
    assert credential.value == "ak_any_key"


def test_api_key_resolver_rejects_missing_header() -> None:
    resolver = ApiKeyIdentityResolver(SessionCredentialStore())
    ctx = SimpleNamespace(request=SimpleNamespace(headers={}))
    with pytest.raises(DomainError) as excinfo:
        resolver.resolve(ctx)
    assert excinfo.value.code == "mcp_unauthorized"


def test_composite_resolver_prefers_bearer_when_present() -> None:
    store = SessionCredentialStore()
    api_key_resolver = ApiKeyIdentityResolver(store)

    def bearer_factory() -> Any:
        return DirectJwtIdentityResolver(store)

    resolver = _CompositeIdentityResolver(bearer_factory, api_key_resolver)
    header_b64 = _b64url(json.dumps({"alg": "none"}).encode("utf-8"))
    payload_b64 = _b64url(json.dumps({"tenantId": "jwt-t", "loginId": "jwt-u"}).encode("utf-8"))
    jwt_token = header_b64 + "." + payload_b64 + ".sig"

    context = resolver.resolve(
        SimpleNamespace(
            request=SimpleNamespace(
                headers={"authorization": "Bearer " + jwt_token, "x-api-key": "ak_x"},
            ),
        ),
    )
    assert context.tenant_id == "jwt-t"


def test_composite_resolver_falls_back_to_api_key() -> None:
    store = SessionCredentialStore()
    api_key_resolver = ApiKeyIdentityResolver(store)

    def bearer_factory() -> Any:
        return DirectJwtIdentityResolver(store)

    resolver = _CompositeIdentityResolver(bearer_factory, api_key_resolver)
    context = resolver.resolve(_mcp_api_key_context("ak_x"))
    assert context.tenant_id == "ak_x"
    assert store.resolve(context).kind == "api_key"


def test_production_without_secret_allows_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产无 JWT secret 时仍可启动：X-API-Key 请求正常，Bearer 请求才报错。"""
    monkeypatch.setenv("GJP_ENV", "production")
    monkeypatch.delenv("ERP_BILLING_JWT_SECRET", raising=False)
    monkeypatch.delenv("ERP_BILLING_API_KEYS", raising=False)

    resolver = _create_identity_resolver(SessionCredentialStore())
    # X-API-Key 请求正常
    context = resolver.resolve(_mcp_api_key_context("ak_any"))
    assert context.tenant_id == "ak_any"
    # Bearer 请求才报错（惰性构造时 secret 缺失）
    token = _make_token(_valid_payload())
    with pytest.raises(DomainError) as excinfo:
        resolver.resolve(_mcp_context(token))
    assert excinfo.value.code == "mcp_unauthorized"

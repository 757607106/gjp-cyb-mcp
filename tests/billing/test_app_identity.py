"""验证开单服务按环境选择鉴权强度：local 宽松、production 强制 HS256 验签。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest

from erp_billing.app import (
    DirectJwtIdentityResolver,
    SessionBearerStore,
    VerifiedJwtIdentityResolver,
    _create_identity_resolver,
)
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
    store = SessionBearerStore()
    resolver = VerifiedJwtIdentityResolver(store, SECRET)

    context = resolver.resolve(_mcp_context(_make_token(_valid_payload())))

    assert context.tenant_id == "tenant-1"
    assert context.subject_id == "user-1"
    assert store.resolve(context).value == _make_token(_valid_payload())


def test_verified_resolver_rejects_tampered_payload() -> None:
    resolver = VerifiedJwtIdentityResolver(SessionBearerStore(), SECRET)
    header_b64, _, signature_b64 = _make_token(_valid_payload()).split(".")
    forged_payload = _b64url(json.dumps({"tenantId": "evil", "loginId": "evil"}).encode("utf-8"))
    forged_token = header_b64 + "." + forged_payload + "." + signature_b64

    with pytest.raises(DomainError) as excinfo:
        resolver.resolve(_mcp_context(forged_token))

    assert excinfo.value.code == "mcp_unauthorized"


def test_verified_resolver_rejects_wrong_secret() -> None:
    resolver = VerifiedJwtIdentityResolver(SessionBearerStore(), SECRET)
    token = _make_token(_valid_payload(), secret="another-secret")

    with pytest.raises(DomainError) as excinfo:
        resolver.resolve(_mcp_context(token))

    assert excinfo.value.code == "mcp_unauthorized"


def test_verified_resolver_rejects_expired_token() -> None:
    resolver = VerifiedJwtIdentityResolver(SessionBearerStore(), SECRET)
    expired = dict(_valid_payload(), exp=int(time.time()) - 60)

    with pytest.raises(DomainError) as excinfo:
        resolver.resolve(_mcp_context(_make_token(expired)))

    assert excinfo.value.code == "mcp_unauthorized"


def test_verified_resolver_rejects_non_hs256_algorithm() -> None:
    resolver = VerifiedJwtIdentityResolver(SessionBearerStore(), SECRET)
    token = _make_token(_valid_payload(), alg="none")

    with pytest.raises(DomainError) as excinfo:
        resolver.resolve(_mcp_context(token))

    assert excinfo.value.code == "mcp_unauthorized"


def test_verified_resolver_requires_secret() -> None:
    with pytest.raises(DomainError) as excinfo:
        VerifiedJwtIdentityResolver(SessionBearerStore(), "   ")

    assert excinfo.value.code == "mcp_unauthorized"


def test_local_resolver_accepts_unsigned_token() -> None:
    header_b64 = _b64url(json.dumps({"alg": "none"}).encode("utf-8"))
    payload_b64 = _b64url(json.dumps({"tenantId": "t", "loginId": "u"}).encode("utf-8"))
    resolver = DirectJwtIdentityResolver(SessionBearerStore())

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
    store = SessionBearerStore()
    resolver = DirectJwtIdentityResolver(store)

    context = resolver.resolve(mcp_context)

    assert context.tenant_id == "t"
    assert store.resolve(context).value == jwt


def test_verified_resolver_strips_duplicate_bearer_prefix() -> None:
    """验签器同样能剥离客户端误传的多余 Bearer 前缀。"""
    store = SessionBearerStore()
    resolver = VerifiedJwtIdentityResolver(store, SECRET)
    token = _make_token(_valid_payload())
    mcp_context = SimpleNamespace(
        request=SimpleNamespace(headers={"authorization": "Bearer Bearer " + token}),
    )

    context = resolver.resolve(mcp_context)

    assert context.tenant_id == "tenant-1"
    assert store.resolve(context).value == token


def test_composition_root_selects_resolver_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionBearerStore()
    monkeypatch.setenv("ERP_BILLING_JWT_SECRET", SECRET)

    monkeypatch.setenv("GJP_ENV", "production")
    assert isinstance(_create_identity_resolver(store), VerifiedJwtIdentityResolver)

    monkeypatch.delenv("GJP_ENV", raising=False)
    assert isinstance(_create_identity_resolver(store), DirectJwtIdentityResolver)


def test_production_without_secret_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GJP_ENV", "production")
    monkeypatch.delenv("ERP_BILLING_JWT_SECRET", raising=False)

    with pytest.raises(DomainError) as excinfo:
        _create_identity_resolver(SessionBearerStore())

    assert excinfo.value.code == "mcp_unauthorized"


def test_bearer_store_rejects_expired_token() -> None:
    """过期 Token 在 resolve 时返回 business_reauth_required，复用现有错误码。"""
    store = SessionBearerStore()
    context = InvocationContext(
        tenant_id="t",
        subject_id="u",
        account_id="t",
        session_id="billing-u",
    )
    expired_payload = {"tenantId": "t", "loginId": "u", "exp": int(time.time()) - 60}
    expired_token = _make_token(expired_payload)
    store.register(context, expired_token)

    with pytest.raises(DomainError) as excinfo:
        store.resolve(context)

    assert excinfo.value.code == "business_reauth_required"


def test_bearer_store_accepts_unexpired_token() -> None:
    """未过期的 Token 正常返回。"""
    store = SessionBearerStore()
    context = InvocationContext(
        tenant_id="t",
        subject_id="u",
        account_id="t",
        session_id="billing-u",
    )
    token = _make_token(_valid_payload())
    store.register(context, token)

    assert store.resolve(context).value == token

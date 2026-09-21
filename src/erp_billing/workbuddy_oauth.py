"""WorkBuddy MCP OAuth 2.1 适配层。

本模块只负责 WorkBuddy 与 MCP 之间的认证、ERP AI Token 绑定和凭据解析，
不改变开单 ToolSet、商品匹配或业务 API 映射。OAuth Token 与 ERP 凭据严格分离。
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from gjp_common.connections import (
    BusinessApiCredential,
    CredentialKind,
    business_api_url,
    normalize_business_api_base_url,
)
from gjp_common.context import InvocationContext
from gjp_common.errors import DomainError
from gjp_common.paths import resolve_output_path

logger = logging.getLogger(__name__)

WORKBUDDY_READ_SCOPE = "billing:read"
WORKBUDDY_WRITE_SCOPE = "billing:write"
WORKBUDDY_SCOPES = (WORKBUDDY_READ_SCOPE, WORKBUDDY_WRITE_SCOPE)


def _positive_int(value: str, name: str, default: int) -> int:
    raw = value.strip() or str(default)
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise DomainError("workbuddy_config_invalid", f"{name} 必须是整数") from exc
    if parsed <= 0:
        raise DomainError("workbuddy_config_invalid", f"{name} 必须大于 0")
    return parsed


def _public_base_url(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
    if not ((parsed.scheme == "https" and parsed.hostname) or local_http):
        raise DomainError(
            "workbuddy_config_invalid",
            "WORKBUDDY_PUBLIC_BASE_URL 必须是 HTTPS 地址；本地测试可使用 localhost HTTP",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DomainError("workbuddy_config_invalid", "WORKBUDDY_PUBLIC_BASE_URL 不能包含凭据、查询参数或片段")
    return normalized


@dataclass(frozen=True)
class WorkBuddyOAuthSettings:
    """WorkBuddy OAuth 服务配置。"""

    public_base_url: str
    connector_source: str
    database_path: str
    encryption_key: str
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 30 * 24 * 3600
    authorization_code_ttl_seconds: int = 600

    def __post_init__(self) -> None:
        object.__setattr__(self, "public_base_url", _public_base_url(self.public_base_url))
        source = self.connector_source.strip()
        if not source or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in source):
            raise DomainError(
                "workbuddy_config_invalid",
                "WORKBUDDY_CONNECTOR_SOURCE 只能包含小写字母、数字和连字符",
            )
        object.__setattr__(self, "connector_source", source)
        try:
            Fernet(self.encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise DomainError(
                "workbuddy_config_invalid",
                "WORKBUDDY_OAUTH_ENCRYPTION_KEY 必须是 Fernet 密钥",
            ) from exc
        for name, value in (
            ("access_token_ttl_seconds", self.access_token_ttl_seconds),
            ("refresh_token_ttl_seconds", self.refresh_token_ttl_seconds),
            ("authorization_code_ttl_seconds", self.authorization_code_ttl_seconds),
        ):
            if value <= 0:
                raise DomainError("workbuddy_config_invalid", f"{name} 必须大于 0")

    @property
    def issuer_url(self) -> str:
        return self.public_base_url

    @property
    def resource_url(self) -> str:
        return self.public_base_url + "/mcp"

    @property
    def protected_resource_metadata_url(self) -> str:
        return self.public_base_url + "/.well-known/oauth-protected-resource/mcp"

    @property
    def private_redirect_uri(self) -> str:
        encoded_source = quote("connector:" + self.connector_source, safe="")
        return f"workbuddy://workbuddy/mcp/{encoded_source}/oauth/callback"

    @classmethod
    def from_env(cls) -> "WorkBuddyOAuthSettings":
        """从环境读取配置；local 缺少存储配置时使用进程内测试存储。"""
        from gjp_common.config import get_env_value, is_production

        public_base_url = get_env_value(
            "WORKBUDDY_PUBLIC_BASE_URL",
            "https://workbuddy-mcp.yuncyb.com",
        )
        database_path = get_env_value("WORKBUDDY_OAUTH_DB_PATH").strip()
        encryption_key = get_env_value("WORKBUDDY_OAUTH_ENCRYPTION_KEY").strip()
        if is_production() and not database_path:
            raise DomainError(
                "workbuddy_config_invalid",
                "生产环境必须配置 WORKBUDDY_OAUTH_DB_PATH",
            )
        if is_production() and not encryption_key:
            raise DomainError(
                "workbuddy_config_invalid",
                "生产环境必须配置 WORKBUDDY_OAUTH_ENCRYPTION_KEY",
            )
        if not database_path:
            database_path = ":memory:"
        elif database_path != ":memory:":
            database_path = str(resolve_output_path(database_path))
        if not encryption_key:
            encryption_key = Fernet.generate_key().decode("ascii")
        return cls(
            public_base_url=public_base_url,
            connector_source=get_env_value("WORKBUDDY_CONNECTOR_SOURCE", "gjp-erp-billing"),
            database_path=database_path,
            encryption_key=encryption_key,
            access_token_ttl_seconds=_positive_int(
                get_env_value("WORKBUDDY_ACCESS_TOKEN_TTL_SECONDS"),
                "WORKBUDDY_ACCESS_TOKEN_TTL_SECONDS",
                3600,
            ),
            refresh_token_ttl_seconds=_positive_int(
                get_env_value("WORKBUDDY_REFRESH_TOKEN_TTL_SECONDS"),
                "WORKBUDDY_REFRESH_TOKEN_TTL_SECONDS",
                30 * 24 * 3600,
            ),
            authorization_code_ttl_seconds=_positive_int(
                get_env_value("WORKBUDDY_AUTHORIZATION_CODE_TTL_SECONDS"),
                "WORKBUDDY_AUTHORIZATION_CODE_TTL_SECONDS",
                600,
            ),
        )


@dataclass(frozen=True)
class ErpBinding:
    """一个 WorkBuddy 授权主体绑定的 ERP 身份和服务端凭据。"""

    tenant_id: str
    subject_id: str
    account_id: str
    credential_kind: CredentialKind
    credential_value: str

    def credential(self) -> BusinessApiCredential:
        return BusinessApiCredential(kind=self.credential_kind, value=self.credential_value)


@dataclass(frozen=True)
class PendingAuthorization:
    """等待用户绑定 ERP 的 OAuth 授权请求。"""

    request_id: str
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    scopes: tuple[str, ...]
    state: str | None
    resource: str
    expires_at: float


class WorkBuddyRefreshToken(RefreshToken):
    """带目标资源的刷新令牌。"""

    resource: str


class EncryptedSqliteOAuthStore:
    """单实例生产可用的加密 SQLite OAuth 状态存储。

    多副本部署时应在组合根注入实现相同接口的共享存储；本类不把明文 Token、
    ERP 凭据或 OAuth code 写入数据库键和值。
    """

    def __init__(self, database_path: str, encryption_key: str) -> None:
        if database_path != ":memory:":
            path = Path(database_path)
            path.parent.mkdir(parents=True, exist_ok=True)
        self._cipher = Fernet(encryption_key.encode("ascii"))
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workbuddy_oauth_state (
                kind TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                payload BLOB NOT NULL,
                expires_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (kind, key_hash)
            )
            """
        )
        self._connection.commit()

    @staticmethod
    def _key_hash(kind: str, key: str) -> str:
        return hashlib.sha256(f"{kind}:{key}".encode("utf-8")).hexdigest()

    def _seal(self, value: dict[str, Any]) -> bytes:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._cipher.encrypt(raw)

    def _open(self, payload: bytes) -> dict[str, Any]:
        try:
            value = json.loads(self._cipher.decrypt(payload).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DomainError("workbuddy_oauth_state_invalid", "OAuth 状态无法解密") from exc
        if not isinstance(value, dict):
            raise DomainError("workbuddy_oauth_state_invalid", "OAuth 状态格式无效")
        return value

    def _put(self, kind: str, key: str, value: dict[str, Any], expires_at: float = 0) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO workbuddy_oauth_state(kind, key_hash, payload, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(kind, key_hash) DO UPDATE SET
                    payload=excluded.payload,
                    expires_at=excluded.expires_at
                """,
                (kind, self._key_hash(kind, key), self._seal(value), expires_at),
            )
            self._connection.commit()

    def _get(self, kind: str, key: str, *, consume: bool = False) -> dict[str, Any] | None:
        key_hash = self._key_hash(kind, key)
        with self._lock:
            row = self._connection.execute(
                "SELECT payload, expires_at FROM workbuddy_oauth_state WHERE kind=? AND key_hash=?",
                (kind, key_hash),
            ).fetchone()
            if row is None:
                return None
            payload, expires_at = row
            if expires_at and float(expires_at) < time.time():
                self._connection.execute(
                    "DELETE FROM workbuddy_oauth_state WHERE kind=? AND key_hash=?",
                    (kind, key_hash),
                )
                self._connection.commit()
                return None
            if consume:
                self._connection.execute(
                    "DELETE FROM workbuddy_oauth_state WHERE kind=? AND key_hash=?",
                    (kind, key_hash),
                )
                self._connection.commit()
            return self._open(payload)

    def _delete(self, kind: str, key: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM workbuddy_oauth_state WHERE kind=? AND key_hash=?",
                (kind, self._key_hash(kind, key)),
            )
            self._connection.commit()

    def save_client(self, client: OAuthClientInformationFull) -> None:
        assert client.client_id is not None
        self._put("client", client.client_id, client.model_dump(mode="json"))

    def client(self, client_id: str) -> OAuthClientInformationFull | None:
        value = self._get("client", client_id)
        return OAuthClientInformationFull.model_validate(value) if value else None

    def save_pending(self, pending: PendingAuthorization) -> None:
        value = asdict(pending)
        value["scopes"] = list(pending.scopes)
        self._put("pending", pending.request_id, value, pending.expires_at)

    def pending(self, request_id: str) -> PendingAuthorization | None:
        value = self._get("pending", request_id)
        if not value:
            return None
        value["scopes"] = tuple(value["scopes"])
        return PendingAuthorization(**value)

    def consume_pending(self, request_id: str) -> PendingAuthorization | None:
        value = self._get("pending", request_id, consume=True)
        if not value:
            return None
        value["scopes"] = tuple(value["scopes"])
        return PendingAuthorization(**value)

    def save_code(self, code: AuthorizationCode) -> None:
        self._put("code", code.code, code.model_dump(mode="json"), code.expires_at)

    def code(self, value: str) -> AuthorizationCode | None:
        payload = self._get("code", value)
        return AuthorizationCode.model_validate(payload) if payload else None

    def consume_code(self, value: str) -> AuthorizationCode | None:
        payload = self._get("code", value, consume=True)
        return AuthorizationCode.model_validate(payload) if payload else None

    def save_token_pair(self, access: AccessToken, refresh: WorkBuddyRefreshToken) -> None:
        self._put(
            "access",
            access.token,
            {"model": access.model_dump(mode="json"), "paired_token": refresh.token},
            float(access.expires_at or 0),
        )
        self._put(
            "refresh",
            refresh.token,
            {"model": refresh.model_dump(mode="json"), "paired_token": access.token},
            float(refresh.expires_at or 0),
        )

    def access_token(self, value: str) -> AccessToken | None:
        payload = self._get("access", value)
        return AccessToken.model_validate(payload["model"]) if payload else None

    def refresh_token(self, value: str) -> WorkBuddyRefreshToken | None:
        payload = self._get("refresh", value)
        return WorkBuddyRefreshToken.model_validate(payload["model"]) if payload else None

    def consume_refresh_token(self, value: str) -> WorkBuddyRefreshToken | None:
        payload = self._get("refresh", value, consume=True)
        if not payload:
            return None
        self._delete("access", str(payload.get("paired_token") or ""))
        return WorkBuddyRefreshToken.model_validate(payload["model"])

    def revoke_pair(self, kind: str, value: str) -> None:
        payload = self._get(kind, value, consume=True)
        if not payload:
            return
        paired_kind = "refresh" if kind == "access" else "access"
        paired_token = str(payload.get("paired_token") or "")
        if paired_token:
            self._delete(paired_kind, paired_token)

    def save_binding(self, binding: ErpBinding) -> None:
        self._put("binding", binding.subject_id, asdict(binding))

    def binding(self, subject_id: str) -> ErpBinding | None:
        payload = self._get("binding", subject_id)
        return ErpBinding(**payload) if payload else None

    def close(self) -> None:
        with self._lock:
            self._connection.close()


ErpBindingValidator = Callable[[str], Awaitable[ErpBinding]]


class ErpApiKeyBindingValidator:
    """通过只读商品分页请求校验 ERP AI Token。"""

    def __init__(self, base_url: str, timeout_seconds: float = 30) -> None:
        self._base_url = normalize_business_api_base_url(base_url)
        self._timeout_seconds = timeout_seconds

    async def __call__(self, api_key: str) -> ErpBinding:
        normalized = api_key.strip()
        if not normalized:
            raise DomainError("workbuddy_binding_required", "请输入 ERP AI Token")
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds),
                verify=True,
            ) as client:
                response = await client.get(
                    business_api_url(self._base_url, "/product/page"),
                    params={"pageNum": 1, "pageSize": 1, "status": 1},
                    headers={"Accept": "application/json", "X-API-Key": normalized},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DomainError(
                "workbuddy_binding_invalid",
                "ERP AI Token 无效，或 ERP 服务暂时不可用",
            ) from exc
        if not isinstance(payload, dict) or str(payload.get("code") or "") != "A00000":
            raise DomainError("workbuddy_binding_invalid", "ERP AI Token 校验失败")
        identity = "apikey-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return ErpBinding(
            tenant_id=identity,
            subject_id=identity,
            account_id=identity,
            credential_kind="api_key",
            credential_value=normalized,
        )


class WorkBuddyOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, WorkBuddyRefreshToken, AccessToken]):
    """支持 WorkBuddy 公共客户端、PKCE 和刷新令牌轮换的 OAuth Provider。"""

    def __init__(
        self,
        settings: WorkBuddyOAuthSettings,
        store: EncryptedSqliteOAuthStore,
        binding_validator: ErpBindingValidator,
    ) -> None:
        self.settings = settings
        self.store = store
        self._binding_validator = binding_validator

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise RegistrationError("invalid_client_metadata", "缺少 client_id")
        if client_info.token_endpoint_auth_method != "none" or client_info.client_secret:
            raise RegistrationError(
                "invalid_client_metadata",
                "WorkBuddy MCP 必须以不持有 client_secret 的公共客户端注册",
            )
        redirect_uris = client_info.redirect_uris or []
        if not redirect_uris or any(not self._redirect_uri_allowed(str(uri)) for uri in redirect_uris):
            raise RegistrationError("invalid_redirect_uri", "redirect_uri 不在 WorkBuddy 允许范围内")
        self.store.save_client(client_info)

    def _redirect_uri_allowed(self, value: str) -> bool:
        parsed = urlparse(value)
        if parsed.scheme == "workbuddy":
            expected_path = f"/mcp/connector:{self.settings.connector_source}/oauth/callback"
            return parsed.netloc == "workbuddy" and unquote(parsed.path) == expected_path
        return (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port is not None
            and parsed.path == "/oauth/callback"
            and not parsed.query
            and not parsed.fragment
        )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not client.client_id:
            raise AuthorizeError("invalid_request", "客户端缺少 client_id")
        resource = params.resource or self.settings.resource_url
        if resource.rstrip("/") != self.settings.resource_url:
            raise AuthorizeError("invalid_request", "resource 不是当前 MCP 服务")
        scopes = tuple(params.scopes or WORKBUDDY_SCOPES)
        if not set(scopes).issubset(WORKBUDDY_SCOPES):
            raise AuthorizeError("invalid_scope", "请求包含不支持的 scope")
        request_id = "wb_req_" + secrets.token_urlsafe(32)
        self.store.save_pending(
            PendingAuthorization(
                request_id=request_id,
                client_id=client.client_id,
                redirect_uri=str(params.redirect_uri),
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                code_challenge=params.code_challenge,
                scopes=scopes,
                state=params.state,
                resource=self.settings.resource_url,
                expires_at=time.time() + self.settings.authorization_code_ttl_seconds,
            )
        )
        return self.settings.public_base_url + "/oauth/bind?" + urlencode({"request_id": request_id})

    async def complete_binding(self, request_id: str, api_key: str) -> str:
        pending = self.store.pending(request_id)
        if pending is None:
            raise DomainError("workbuddy_authorization_expired", "授权请求已失效，请返回 WorkBuddy 重新连接")
        binding = await self._binding_validator(api_key)
        pending = self.store.consume_pending(request_id)
        if pending is None:
            raise DomainError("workbuddy_authorization_expired", "授权请求已失效，请返回 WorkBuddy 重新连接")
        self.store.save_binding(binding)
        code_value = "wb_code_" + secrets.token_urlsafe(32)
        self.store.save_code(
            AuthorizationCode(
                code=code_value,
                scopes=list(pending.scopes),
                expires_at=time.time() + self.settings.authorization_code_ttl_seconds,
                client_id=pending.client_id,
                code_challenge=pending.code_challenge,
                redirect_uri=pending.redirect_uri,
                redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
                resource=pending.resource,
                subject=binding.subject_id,
            )
        )
        return construct_redirect_uri(
            pending.redirect_uri,
            code=code_value,
            state=pending.state,
        )

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        return self.store.code(authorization_code)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        consumed = self.store.consume_code(authorization_code.code)
        if consumed is None or consumed.client_id != client.client_id:
            raise TokenError("invalid_grant", "authorization code 已使用或不存在")
        if consumed.subject is None or self.store.binding(consumed.subject) is None:
            raise TokenError("invalid_grant", "ERP 绑定不存在")
        return self._issue_token_pair(
            client_id=consumed.client_id,
            scopes=consumed.scopes,
            subject=consumed.subject,
            resource=consumed.resource or self.settings.resource_url,
        )

    def _issue_token_pair(
        self,
        *,
        client_id: str,
        scopes: Sequence[str],
        subject: str,
        resource: str,
    ) -> OAuthToken:
        now = int(time.time())
        access_value = "wb_at_" + secrets.token_urlsafe(32)
        refresh_value = "wb_rt_" + secrets.token_urlsafe(48)
        access = AccessToken(
            token=access_value,
            client_id=client_id,
            scopes=list(scopes),
            expires_at=now + self.settings.access_token_ttl_seconds,
            resource=resource,
            subject=subject,
            claims={"iss": self.settings.issuer_url},
        )
        refresh = WorkBuddyRefreshToken(
            token=refresh_value,
            client_id=client_id,
            scopes=list(scopes),
            expires_at=now + self.settings.refresh_token_ttl_seconds,
            subject=subject,
            resource=resource,
        )
        self.store.save_token_pair(access, refresh)
        return OAuthToken(
            access_token=access_value,
            token_type="Bearer",
            expires_in=self.settings.access_token_ttl_seconds,
            refresh_token=refresh_value,
            scope=" ".join(scopes),
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> WorkBuddyRefreshToken | None:
        return self.store.refresh_token(refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: WorkBuddyRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        consumed = self.store.consume_refresh_token(refresh_token.token)
        if consumed is None or consumed.client_id != client.client_id or not consumed.subject:
            raise TokenError("invalid_grant", "refresh token 已使用或不存在")
        if not set(scopes).issubset(consumed.scopes):
            raise TokenError("invalid_scope", "刷新时不能扩大授权范围")
        return self._issue_token_pair(
            client_id=consumed.client_id,
            scopes=scopes,
            subject=consumed.subject,
            resource=consumed.resource,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self.store.access_token(token)
        if access is None:
            return None
        if access.expires_at and access.expires_at < int(time.time()):
            return None
        if access.resource != self.settings.resource_url:
            return None
        if (access.claims or {}).get("iss") != self.settings.issuer_url:
            return None
        if not access.subject or self.store.binding(access.subject) is None:
            return None
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "access" if isinstance(token, AccessToken) else "refresh"
        self.store.revoke_pair(kind, token.token)

    def binding(self, subject_id: str) -> ErpBinding | None:
        return self.store.binding(subject_id)


class WorkBuddyIdentityResolver:
    """把已由 HTTP 中间件验证的 WorkBuddy Token 映射为业务上下文。"""

    def __init__(self, provider: WorkBuddyOAuthProvider) -> None:
        self._provider = provider

    async def resolve(self, mcp_request_context: Any) -> InvocationContext:
        request = getattr(mcp_request_context, "request", None)
        scope = getattr(request, "scope", {}) if request is not None else {}
        access = scope.get("workbuddy_access_token")
        if not isinstance(access, AccessToken) or not access.subject:
            raise DomainError("mcp_unauthorized", "WorkBuddy OAuth 身份未通过验证")
        binding = self._provider.binding(access.subject)
        if binding is None:
            raise DomainError("business_reauth_required", "ERP 绑定已失效，请重新连接")
        headers = getattr(request, "headers", None)
        conversation = ""
        if headers is not None:
            conversation = headers.get("x-conversation-id", "") or headers.get("mcp-session-id", "")
        session_id = "workbuddy-" + binding.subject_id
        if conversation.strip():
            suffix = hashlib.sha256(conversation.strip().encode("utf-8")).hexdigest()[:16]
            session_id += "-" + suffix
        return InvocationContext(
            tenant_id=binding.tenant_id,
            subject_id=binding.subject_id,
            account_id=binding.account_id,
            session_id=session_id,
            scopes=frozenset(access.scopes),
        )


class WorkBuddyCredentialProvider:
    """按已认证主体解析 ERP 凭据，不把 MCP Token 透传给 ERP。"""

    def __init__(self, provider: WorkBuddyOAuthProvider) -> None:
        self._provider = provider

    def resolve(self, context: InvocationContext) -> BusinessApiCredential:
        binding = self._provider.binding(context.subject_id)
        if binding is None:
            raise DomainError("business_reauth_required", "ERP 绑定已失效，请重新连接")
        return binding.credential()


class WorkBuddyOAuthProtectionMiddleware:
    """只保护 MCP 传输路径，OAuth 元数据、授权页和探活保持公开。"""

    def __init__(
        self,
        app: ASGIApp,
        provider: WorkBuddyOAuthProvider,
        protected_paths: Sequence[str] = ("/mcp",),
    ) -> None:
        self._app = app
        self._provider = provider
        self._protected_paths = tuple(protected_paths)

    def _is_protected(self, path: str) -> bool:
        return any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in self._protected_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("method") == "OPTIONS":
            await self._app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if not self._is_protected(path):
            await self._app(scope, receive, send)
            return
        headers = {key.decode("latin-1").casefold(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        authorization = headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        access = None
        if scheme.casefold() == "bearer" and token.strip():
            access = await self._provider.load_access_token(token.strip())
        if access is None:
            await self._send_error(send, 401, "invalid_token", "Authentication required")
            return
        if WORKBUDDY_READ_SCOPE not in access.scopes:
            await self._send_error(send, 403, "insufficient_scope", f"Required scope: {WORKBUDDY_READ_SCOPE}")
            return
        scope["workbuddy_access_token"] = access
        await self._app(scope, receive, send)

    async def _send_error(
        self,
        send: Send,
        status_code: int,
        error: str,
        description: str,
    ) -> None:
        challenge = 'Bearer error="%s", error_description="%s", scope="%s", resource_metadata="%s"' % (
            error,
            description,
            WORKBUDDY_READ_SCOPE,
            self._provider.settings.protected_resource_metadata_url,
        )
        body = json.dumps({"error": error, "error_description": description}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", challenge.encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _binding_page(request_id: str, error: str = "") -> HTMLResponse:
    """生成轻量 ERP Token 绑定页，不加载第三方资源。"""
    safe_request_id = html.escape(request_id, quote=True)
    error_html = f'<div class="error" role="alert">{html.escape(error)}</div>' if error else ""
    content = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>连接管家婆 ERP</title>
  <style>
    :root {{ --ink:#17242d; --paper:#f4f0e7; --line:#c7bda9; --accent:#d75b34; --ok:#1d6b55; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-height:100vh; display:grid; place-items:center; color:var(--ink);
      background:linear-gradient(90deg,rgba(23,36,45,.045) 1px,transparent 1px),
      linear-gradient(rgba(23,36,45,.045) 1px,transparent 1px),var(--paper);
      background-size:28px 28px; font-family:"Noto Serif SC","Songti SC",Georgia,serif; }}
    main {{ width:min(92vw,560px); border:1px solid var(--ink); background:rgba(250,248,242,.96);
      box-shadow:12px 12px 0 rgba(23,36,45,.13); }}
    header {{ padding:30px 34px 24px; border-bottom:1px solid var(--line); position:relative; }}
    header::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:7px; background:var(--accent); }}
    .eyebrow {{ font:700 12px/1.2 ui-monospace,SFMono-Regular,monospace; letter-spacing:.16em;
      text-transform:uppercase; color:var(--ok); }}
    h1 {{ margin:12px 0 8px; font-size:clamp(28px,6vw,42px); line-height:1.08; font-weight:700; }}
    p {{ margin:0; line-height:1.75; color:#53616a; }}
    form {{ padding:30px 34px 34px; }}
    label {{ display:block; margin-bottom:10px; font-weight:700; }}
    input {{ width:100%; border:1px solid var(--ink); background:#fff; color:var(--ink); padding:14px 15px;
      font:16px/1.3 ui-monospace,SFMono-Regular,monospace; outline:none; transition:box-shadow .15s; }}
    input:focus {{ box-shadow:0 0 0 3px rgba(29,107,85,.22); }}
    .note {{ margin:12px 0 22px; font-size:13px; line-height:1.65; }}
    button {{ width:100%; border:1px solid var(--ink); background:var(--ink); color:#fff; padding:14px 18px;
      font:700 15px/1.2 ui-monospace,SFMono-Regular,monospace; cursor:pointer; letter-spacing:.05em; }}
    button:hover {{ background:var(--ok); }}
    .error {{ margin-bottom:18px; padding:12px 14px; border-left:5px solid var(--accent); background:#fff0e9;
      color:#8e3017; line-height:1.5; }}
    footer {{ padding:15px 34px; border-top:1px solid var(--line); font-size:12px; color:#68747b; }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="eyebrow">WorkBuddy × GJP ERP</div>
      <h1>连接开单能力</h1>
      <p>授权后，WorkBuddy 可按你的指令查询商品和销售单；创建、修改与作废仍需你明确确认。</p>
    </header>
    <form method="post" action="/oauth/bind" autocomplete="off">
      {error_html}
      <input type="hidden" name="request_id" value="{safe_request_id}">
      <label for="api_key">ERP AI Token</label>
      <input id="api_key" name="api_key" type="password" required autofocus
        autocomplete="off" spellcheck="false" placeholder="粘贴 ERP 中生成的 AI Token">
      <p class="note">Token 仅发送到管家婆 MCP 服务并加密保存，不会进入对话、工具参数或 WorkBuddy 模型上下文。</p>
      <button type="submit">验证并连接</button>
    </form>
    <footer>最小权限 · 短期 MCP Token · 可撤销授权</footer>
  </main>
</body>
</html>"""
    return HTMLResponse(
        content,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


def create_workbuddy_oauth_routes(provider: WorkBuddyOAuthProvider) -> list[BaseRoute]:
    """创建 WorkBuddy 发现、动态注册、PKCE、刷新和 ERP 绑定路由。"""
    settings = provider.settings
    client_options = ClientRegistrationOptions(
        enabled=True,
        valid_scopes=list(WORKBUDDY_SCOPES),
        default_scopes=list(WORKBUDDY_SCOPES),
    )
    authenticator = ClientAuthenticator(provider)

    async def authorization_metadata(_request: Request) -> Response:
        return JSONResponse(
            {
                "issuer": settings.issuer_url,
                "authorization_endpoint": settings.public_base_url + "/oauth/authorize",
                "token_endpoint": settings.public_base_url + "/oauth/token",
                "registration_endpoint": settings.public_base_url + "/oauth/register",
                "revocation_endpoint": settings.public_base_url + "/oauth/revoke",
                "scopes_supported": list(WORKBUDDY_SCOPES),
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
            },
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def protected_resource_metadata(_request: Request) -> Response:
        return JSONResponse(
            {
                "resource": settings.resource_url,
                "authorization_servers": [settings.issuer_url],
                "scopes_supported": list(WORKBUDDY_SCOPES),
                "bearer_methods_supported": ["header"],
                "resource_name": "GJP ERP Billing MCP",
            },
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def bind_page(request: Request) -> Response:
        request_id = request.query_params.get("request_id", "").strip()
        if not request_id or provider.store.pending(request_id) is None:
            return _binding_page(request_id, "授权请求已失效，请返回 WorkBuddy 重新连接")
        return _binding_page(request_id)

    async def bind_submit(request: Request) -> Response:
        form = await request.form()
        request_id = str(form.get("request_id") or "").strip()
        api_key = str(form.get("api_key") or "").strip()
        try:
            redirect_uri = await provider.complete_binding(request_id, api_key)
        except DomainError as exc:
            return _binding_page(request_id, exc.message)
        return RedirectResponse(redirect_uri, status_code=302, headers={"Cache-Control": "no-store"})

    return [
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=authorization_metadata,
            methods=["GET"],
        ),
        Route(
            "/.well-known/oauth-protected-resource",
            endpoint=protected_resource_metadata,
            methods=["GET"],
        ),
        Route(
            "/.well-known/oauth-protected-resource/mcp",
            endpoint=protected_resource_metadata,
            methods=["GET"],
        ),
        Route(
            "/oauth/register",
            endpoint=cors_middleware(
                RegistrationHandler(provider, client_options).handle,
                ["POST", "OPTIONS"],
            ),
            methods=["POST", "OPTIONS"],
        ),
        Route(
            "/oauth/authorize",
            endpoint=AuthorizationHandler(provider).handle,
            methods=["GET", "POST"],
        ),
        Route(
            "/oauth/token",
            endpoint=cors_middleware(
                TokenHandler(provider, authenticator).handle,
                ["POST", "OPTIONS"],
            ),
            methods=["POST", "OPTIONS"],
        ),
        Route(
            "/oauth/revoke",
            endpoint=cors_middleware(
                RevocationHandler(provider, authenticator).handle,
                ["POST", "OPTIONS"],
            ),
            methods=["POST", "OPTIONS"],
        ),
        Route("/oauth/bind", endpoint=bind_page, methods=["GET"]),
        Route("/oauth/bind", endpoint=bind_submit, methods=["POST"]),
    ]

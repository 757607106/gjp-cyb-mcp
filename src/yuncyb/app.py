"""动态接收 ERP Bearer Token 或 API Key 的直连 MCP 服务入口。

三方客户端在每个 MCP 请求中通过 ``Authorization: Bearer`` 或 ``X-API-Key``
传入 ERP 长期业务凭据。服务端只解析身份或生成摘要用于会话隔离，并把原凭据注入
固定 ERP API 做最终鉴权；WorkBuddy MCP OAuth 使用 ``workbuddy_app`` 独立入口。

运行方式：

    export YUNCYB_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
    uv run uvicorn yuncyb.app:app --host 0.0.0.0 --port 8102
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

import jwt
from cachetools import TTLCache
from starlette.types import ASGIApp, Receive, Scope, Send

from gjp_common.config import get_env_value
from gjp_common.connections import BusinessApiCredential, BusinessApiCredentialProvider
from gjp_common.context import InvocationContext, InvocationContextStore
from gjp_common.errors import DomainError
from gjp_common.logging_config import configure_logging
from gjp_common.mcp import McpToolSetResolver
from .adapters import (
    BusinessAuthenticatedJsonClient,
    ErpAuthenticatedHttpAdapter,
    UnavailableYunCybApi,
    create_match_logger_from_env,
)
from .catalog import ProductCatalog
from .catalog_state import TenantCatalogState
from .config import YunCybSettings
from .mcp_service import create_yuncyb_mcp_service, mcp_transport_allowlists, rate_limit_yuncyb_mcp
from .session import YunCybSession
from .toolset import YunCybToolSet

__all__ = ["app", "create_yuncyb_app"]


def _bearer_token_from_mcp_context(mcp_request_context: Any) -> str:
    """从 MCP HTTP 请求头读取 Bearer Token。

    标准格式为 ``Authorization: Bearer <token>``；部分客户端可能误传
    ``Bearer Bearer <token>``，此处剥离多余前缀以保证下游拿到纯 token。
    """
    request = getattr(mcp_request_context, "request", None)
    headers = getattr(request, "headers", None)
    authorization = headers.get("authorization", "") if headers is not None else ""
    if not authorization:
        raise DomainError("mcp_unauthorized", "缺少 Authorization: Bearer <token>")
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not token.strip():
        raise DomainError("mcp_unauthorized", "Authorization 必须使用 Bearer token")
    token = token.strip()
    # 防御性剥离客户端可能误传的多余 Bearer 前缀
    if token[:7].casefold() == "bearer ":
        token = token[7:].strip()
    if not token:
        raise DomainError("mcp_unauthorized", "Authorization Bearer 后缺少令牌")
    return token


def _api_key_from_mcp_context(mcp_request_context: Any) -> str:
    """从 MCP HTTP 请求头读取 X-API-Key。"""
    request = getattr(mcp_request_context, "request", None)
    headers = getattr(request, "headers", None)
    api_key = headers.get("x-api-key", "") if headers is not None else ""
    if not api_key.strip():
        raise DomainError("mcp_unauthorized", "缺少 X-API-Key")
    return api_key.strip()


_MAX_CONVERSATION_ID_LENGTH = 64


def _conversation_id_from_mcp_context(mcp_request_context: Any) -> str:
    """从 MCP HTTP 请求头读取对接方的会话标识，未传时返回空。

    标识参与 (tenant, account, session) 会话键，使同一用户的不同对话窗口
    各自隔离预览与幂等缓存；MCP 传输本身无状态，只能由客户端显式传入。
    仅作为内存字典键使用，截断长度防止异常长 header。
    """
    request = getattr(mcp_request_context, "request", None)
    headers = getattr(request, "headers", None)
    conversation_id = headers.get("x-conversation-id", "") if headers is not None else ""
    return conversation_id.strip()[:_MAX_CONVERSATION_ID_LENGTH]


def _decode_jwt_unverified(token: str) -> dict:
    """不验签地解析 JWT payload，用于受信入口的身份路由和提取 exp。"""
    try:
        payload = jwt.decode(
            token,
            options={"verify_signature": False},
        )
    except jwt.PyJWTError as exc:
        raise DomainError("mcp_unauthorized", "ERP JWT payload 解析失败") from exc
    if not isinstance(payload, dict):
        raise DomainError("mcp_unauthorized", "ERP JWT payload 不是对象")
    return payload


def _context_from_jwt(token: str, conversation_id: str) -> InvocationContext:
    """解析 ERP JWT payload 得到无凭据的调用上下文。

    legacy 入口只读取 payload 中的身份信息，不在本服务验签；调用方必须位于
    可信接入边界内，原 Token 由 ERP API 做最终鉴权。会话键拼接客户端传入的
    对话标识，未传时退化为按登录账号隔离，保持既有行为。
    """
    payload = _decode_jwt_unverified(token)
    identities: dict[str, str] = {}
    for claim in ("tenantId", "loginId"):
        value = payload.get(claim)
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
            raise DomainError("mcp_unauthorized", "ERP JWT 缺少有效身份字段")
        identities[claim] = str(value)
    tenant_id = identities["tenantId"]
    login_id = identities["loginId"]
    session_id = "yuncyb-" + login_id
    if conversation_id:
        session_id += "-" + conversation_id
    return InvocationContext(
        tenant_id=tenant_id,
        subject_id=login_id,
        account_id=tenant_id,
        session_id=session_id,
        scopes=frozenset({"yuncyb:read", "yuncyb:write"}),
    )


@dataclass
class _StoredCredential:
    """已存储的业务凭据及其过期时间。"""

    credential: BusinessApiCredential
    expires_at: float  # epoch seconds，0 表示未知/不过期


def _extract_exp(token: str) -> float:
    """从 JWT payload 解析 exp，失败返回 0。"""
    try:
        exp = _decode_jwt_unverified(token).get("exp")
    except DomainError:
        return 0.0
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        return float(exp)
    return 0.0


class SessionCredentialStore:
    """按会话保存当前 MCP 凭据，供 Adapter 注入 ERP API 调用。

    Bearer/API-Key 只存在于服务端内存，不进入 InvocationContext、Tool Schema
    或模型上下文；单进程装配使用，多副本部署应替换为共享会话存储。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._creds: TTLCache[tuple[str, str, str], _StoredCredential] = TTLCache(
            maxsize=int(get_env_value("MCP_SESSION_MAX_SIZE", "500") or 500),
            ttl=int(get_env_value("MCP_SESSION_TTL_SECONDS", "3600") or 3600),
        )

    @staticmethod
    def _key(context: InvocationContext) -> tuple[str, str, str]:
        return (context.tenant_id, context.account_id, context.session_id)

    def register(self, context: InvocationContext, credential: BusinessApiCredential) -> None:
        exp = _extract_exp(credential.value) if credential.kind == "bearer" else 0.0
        with self._lock:
            self._creds[self._key(context)] = _StoredCredential(credential, exp)

    def resolve(self, context: InvocationContext) -> BusinessApiCredential:
        with self._lock:
            stored = self._creds.get(self._key(context))
        if stored is None:
            raise DomainError("business_credential_required", "当前会话缺少 ERP 鉴权凭据")
        if stored.expires_at and stored.expires_at < time.time():
            with self._lock:
                self._creds.pop(self._key(context), None)
            raise DomainError("business_reauth_required", "当前业务系统授权已失效")
        return stored.credential


class DirectJwtIdentityResolver:
    """把可信接入方传入的 ERP JWT 映射为上下文，并将原 Token 交给 ERP。"""

    def __init__(self, store: SessionCredentialStore) -> None:
        self._store = store

    def resolve(self, mcp_request_context: Any) -> InvocationContext:
        token = _bearer_token_from_mcp_context(mcp_request_context)
        context = _context_from_jwt(
            token,
            _conversation_id_from_mcp_context(mcp_request_context),
        )
        context.require_scope("yuncyb:read")
        self._store.register(
            context,
            BusinessApiCredential(kind="bearer", value=token),
        )
        return context


class YunCybSessionToolSetResolver(McpToolSetResolver):
    """按 (tenant, account, session) 返回隔离的开单 ToolSet。

    商品目录是租户级数据：同一租户全部会话共享一个 TenantCatalogState，
    会话 ToolSet 被 TTL 淘汰或新对话建立时目录仍在，新会话零冷启动；
    目录过期由后台任务刷新，读请求继续用旧目录。
    """

    def __init__(
        self,
        store: BusinessApiCredentialProvider,
        settings: YunCybSettings,
        timeout_seconds: float,
    ) -> None:
        self._store = store
        self._settings = settings
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._toolsets: TTLCache[tuple[str, str, str], YunCybToolSet] = TTLCache(
            maxsize=int(get_env_value("MCP_SESSION_MAX_SIZE", "500") or 500),
            ttl=int(get_env_value("MCP_SESSION_TTL_SECONDS", "3600") or 3600),
        )
        # 别名与品类词从配置加载一次，供全部租户目录构建复用
        seed = ProductCatalog.from_settings(
            replace(settings, product_catalog_path=None),
        )
        self._catalog_aliases = seed.aliases
        self._catalog_categories = seed.categories
        self._catalog_ttl_seconds = float(settings.catalog_ttl_seconds)
        # 租户数远小于会话数，字典不设淘汰：目录常驻避免冷启动回退
        self._catalog_states: dict[str, TenantCatalogState] = {}
        # 全部 ToolSet 共用一个 HTTP 客户端：凭据按 InvocationContext 注入，
        # 传输层无会话差异；共享后 TTL 淘汰 ToolSet 不再泄漏连接池。
        # 惰性创建保持未配置 YUNCYB_BASE_URL 时构造不报错的既有行为。
        self._http: BusinessAuthenticatedJsonClient | None = None

    def _catalog_state_for(self, tenant_id: str) -> TenantCatalogState:
        with self._lock:
            state = self._catalog_states.get(tenant_id)
            if state is None:
                state = TenantCatalogState(
                    ttl_seconds=self._catalog_ttl_seconds,
                    aliases=self._catalog_aliases,
                    categories=self._catalog_categories,
                )
                self._catalog_states[tenant_id] = state
            return state

    def resolve(self, context: InvocationContext) -> YunCybToolSet:
        key = (context.tenant_id, context.account_id, context.session_id)
        with self._lock:
            toolset = self._toolsets.get(key)
        if toolset is not None:
            return toolset
        base_url = str(get_env_value("YUNCYB_BASE_URL")).strip()
        if not base_url:
            raise DomainError(
                "business_connection_invalid",
                "未配置 YUNCYB_BASE_URL",
            )
        if self._http is None:
            self._http = BusinessAuthenticatedJsonClient(
                base_url=base_url,
                credential_provider=self._store,
                timeout_seconds=self._timeout_seconds,
            )
        api = ErpAuthenticatedHttpAdapter(self._http)
        session = YunCybSession(
            replace(self._settings, product_catalog_path=None),
            ProductCatalog(
                products=[],
                aliases=self._catalog_aliases,
                categories=self._catalog_categories,
            ),
            match_logger=create_match_logger_from_env(),
            catalog_state=self._catalog_state_for(context.tenant_id),
        )
        toolset = YunCybToolSet(session, api, InvocationContextStore())
        with self._lock:
            self._toolsets[key] = toolset
        return toolset

    async def close(self) -> None:
        """服务停机时释放共享 HTTP 连接池、会话 ToolSet 和租户目录。"""
        with self._lock:
            http = self._http
            self._http = None
            self._toolsets.clear()
            states = list(self._catalog_states.values())
            self._catalog_states.clear()
        for state in states:
            await state.close()
        if http is not None:
            await http.close()


class _LazyYunCybApp:
    """让 uvicorn 导入模块时不立即读取部署配置。"""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._app: Any | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self._app is None:
            self._app = self._factory()
        await self._app(scope, receive, send)


class ApiKeyIdentityResolver:
    """X-API-Key 鉴权：用 key 直接构造调用上下文。

    key 等同于 token：每个租户在创业版生成 key，客户端传 X-API-Key 即
    可，服务端无需预配映射。MCP 用 key 的摘要作会话隔离标识，ERP 业务 API
    调用用 X-API-Key 头由 ERP 识别真实租户。key 只在服务端内存中流转，
    不进入 Tool Schema 或模型上下文。
    """

    def __init__(self, store: SessionCredentialStore) -> None:
        self._store = store

    def resolve(self, mcp_request_context: Any) -> InvocationContext:
        api_key = _api_key_from_mcp_context(mcp_request_context)
        conversation_id = _conversation_id_from_mcp_context(mcp_request_context)
        # 稳定摘要保持同 Key 隔离语义，避免凭据进入身份和常规日志。
        identity = "apikey-" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        context = InvocationContext(
            tenant_id=identity,
            subject_id=identity,
            account_id=identity,
            session_id=("yuncyb-apikey-" + conversation_id if conversation_id else "yuncyb-apikey"),
            scopes=frozenset({"yuncyb:read", "yuncyb:write"}),
        )
        context.require_scope("yuncyb:read")
        self._store.register(
            context,
            BusinessApiCredential(kind="api_key", value=api_key),
        )
        return context


class _CompositeIdentityResolver:
    """按请求头选择鉴权方式：有 Authorization 走 Bearer，否则走 X-API-Key。

    Bearer 解析器惰性构造；ERP Bearer 与 API Key 都由接入方逐请求传入，
    服务端不保存部署级业务凭据。
    """

    def __init__(
        self,
        bearer_factory: Callable[[], Any],
        api_key_resolver: ApiKeyIdentityResolver,
    ) -> None:
        self._bearer_factory = bearer_factory
        self._api_key_resolver = api_key_resolver
        self._bearer: Any | None = None

    def resolve(self, mcp_request_context: Any) -> InvocationContext:
        request = getattr(mcp_request_context, "request", None)
        headers = getattr(request, "headers", None)
        authorization = headers.get("authorization", "") if headers is not None else ""
        if authorization.strip():
            return self._ensure_bearer().resolve(mcp_request_context)
        return self._api_key_resolver.resolve(mcp_request_context)

    def _ensure_bearer(self) -> Any:
        if self._bearer is None:
            self._bearer = self._bearer_factory()
        return self._bearer


def _create_identity_resolver(bearer_store: SessionCredentialStore) -> Any:
    """组合根：按请求头选择 Bearer JWT 或 X-API-Key 鉴权。

    两类凭据都由可信接入方逐请求提供，并原样交给固定地址的 ERP API 验证；
    MCP 只解析 Bearer 身份字段用于会话隔离，不持有 ERP JWT 签名密钥。
    """
    api_key_resolver = ApiKeyIdentityResolver(bearer_store)

    def bearer_factory() -> Any:
        return DirectJwtIdentityResolver(bearer_store)

    return _CompositeIdentityResolver(bearer_factory, api_key_resolver)


class DirectCredentialProtectionMiddleware:
    """在进入 MCP 协议处理前校验直连接口的凭据类型与基本格式。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") == "OPTIONS"
            or str(scope.get("path") or "") != "/mcp"
        ):
            await self._app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").casefold(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "").strip()
        api_key = headers.get("x-api-key", "").strip()
        valid = bool(api_key)
        if authorization:
            scheme, _, token = authorization.partition(" ")
            token = token.strip()
            if token[:7].casefold() == "bearer ":
                token = token[7:].strip()
            try:
                payload = _decode_jwt_unverified(token) if scheme.casefold() == "bearer" and token else {}
                valid = bool(payload.get("tenantId") and payload.get("loginId"))
                exp = payload.get("exp")
                if isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp <= time.time():
                    valid = False
            except DomainError:
                valid = False
        if valid:
            await self._app(scope, receive, send)
            return
        body = json.dumps(
            {"error": "invalid_token", "error_description": "Authentication required"},
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"www-authenticate", b'Bearer realm="yuncyb"'),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def create_yuncyb_app() -> Any:
    """装配动态接收 ERP Bearer Token 或 API Key 的直连 MCP 应用。"""
    configure_logging()
    timeout_seconds = float(get_env_value("YUNCYB_TIMEOUT_SECONDS", "30") or 30)
    if timeout_seconds <= 0:
        raise DomainError("business_connection_invalid", "超时时间必须大于 0")
    settings = YunCybSettings.from_env()
    bearer_store = SessionCredentialStore()
    schema_toolset = YunCybToolSet(
        YunCybSession.from_settings(
            replace(settings, product_catalog_path=None),
            allow_missing_catalog=True,
            match_logger=create_match_logger_from_env(),
        ),
        UnavailableYunCybApi(),
        InvocationContextStore(),
    )
    toolset_resolver = YunCybSessionToolSetResolver(
        bearer_store,
        settings,
        timeout_seconds,
    )
    allowed_hosts, allowed_origins = mcp_transport_allowlists()
    service = create_yuncyb_mcp_service(
        schema_toolset=schema_toolset,
        identity_resolver=_create_identity_resolver(bearer_store),
        toolset_resolver=toolset_resolver,
        shutdown=toolset_resolver.close,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    return rate_limit_yuncyb_mcp(DirectCredentialProtectionMiddleware(service))


app = _LazyYunCybApp(create_yuncyb_app)

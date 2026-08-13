"""开单 MCP 服务启动入口。

MCP 客户端直接使用 ERP JWT 作为 Bearer Token，服务端从 JWT payload 解析
tenantId、loginId 构造 InvocationContext，并把同一个 JWT 注入当前会话的
ERP API 调用。业务 URL 始终来自部署级固定配置 ERP_BILLING_BASE_URL。

身份解析按环境区分，且只在本组合根分支：
local（测试环境）使用 DirectJwtIdentityResolver 直接读 payload；
production 使用 VerifiedJwtIdentityResolver，要求 HS256 验签通过且
未过期，密钥由部署环境变量 ERP_BILLING_JWT_SECRET 注入。

运行方式：

    export ERP_BILLING_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api
    uv run uvicorn erp_billing.app:app --host 0.0.0.0 --port 8102
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

import jwt
from cachetools import TTLCache

from gjp_common.config import get_env_value, is_production
from gjp_common.connections import BusinessApiCredential
from gjp_common.context import InvocationContext, InvocationContextStore
from gjp_common.errors import DomainError
from gjp_common.logging_config import configure_logging
from gjp_common.mcp import McpToolSetResolver
from .adapters import (
    BusinessAuthenticatedJsonClient,
    ErpAuthenticatedHttpAdapter,
    UnavailableBillingApi,
    create_match_logger_from_env,
)
from .config import ErpBillingSettings
from .mcp_service import create_billing_mcp_service
from .session import ErpBillingSession
from .toolset import BillingToolSet


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


def _decode_jwt_unverified(token: str) -> dict:
    """不验签地解析 JWT payload，用于本地环境和提取 exp。"""
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


def _decode_jwt_verified(token: str, secret: str) -> dict:
    """HS256 验签并校验过期后返回 payload。"""
    try:
        payload = jwt.decode(
            token,
            key=secret,
            algorithms=["HS256"],
        )
    except jwt.ExpiredSignatureError as exc:
        raise DomainError("mcp_unauthorized", "ERP JWT 已过期") from exc
    except jwt.InvalidAlgorithmError as exc:
        raise DomainError("mcp_unauthorized", "生产仅接受 HS256 签名的 JWT") from exc
    except jwt.PyJWTError as exc:
        raise DomainError("mcp_unauthorized", "ERP JWT 验签失败") from exc
    if not isinstance(payload, dict):
        raise DomainError("mcp_unauthorized", "ERP JWT payload 不是对象")
    return payload


def _context_from_jwt(token: str) -> InvocationContext:
    """解析 ERP JWT payload 得到无凭据的调用上下文。

    只读取 payload 中的身份信息，不验签；验签由生产专用解析器
    在调用本函数前完成。
    """
    payload = _decode_jwt_unverified(token)
    tenant_id = str(payload.get("tenantId") or "unknown")
    login_id = str(payload.get("loginId") or "unknown")
    return InvocationContext(
        tenant_id=tenant_id,
        subject_id=login_id,
        account_id=tenant_id,
        session_id="billing-" + login_id,
        scopes=frozenset({"billing:read", "billing:write"}),
    )


@dataclass
class _StoredBearer:
    """已存储的 Bearer Token 及其过期时间。"""

    token: str
    expires_at: float  # epoch seconds，0 表示未知


def _extract_exp(token: str) -> float:
    """从 JWT payload 解析 exp，失败返回 0。"""
    try:
        exp = _decode_jwt_unverified(token).get("exp")
    except DomainError:
        return 0.0
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        return float(exp)
    return 0.0


class SessionBearerStore:
    """按会话保存当前 MCP Bearer，供 Adapter 注入 ERP API 调用。

    Bearer 只存在于服务端内存，不进入 InvocationContext、Tool Schema 或
    模型上下文；单进程装配使用，多副本部署应替换为共享会话存储。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bearers: dict[tuple[str, str, str], _StoredBearer] = {}

    @staticmethod
    def _key(context: InvocationContext) -> tuple[str, str, str]:
        return (context.tenant_id, context.account_id, context.session_id)

    def register(self, context: InvocationContext, bearer: str) -> None:
        exp = _extract_exp(bearer)
        with self._lock:
            self._bearers[self._key(context)] = _StoredBearer(bearer, exp)

    def resolve(self, context: InvocationContext) -> BusinessApiCredential:
        with self._lock:
            stored = self._bearers.get(self._key(context))
        if stored is None:
            raise DomainError("business_credential_required", "当前会话缺少 ERP Bearer")
        if stored.expires_at and stored.expires_at < time.time():
            raise DomainError("business_reauth_required", "当前业务系统授权已失效")
        return BusinessApiCredential(kind="bearer", value=stored.token)


class DirectJwtIdentityResolver:
    """本地/测试环境：直接把 MCP Bearer 中的 ERP JWT 映射为 InvocationContext。"""

    def __init__(self, store: SessionBearerStore) -> None:
        self._store = store

    def resolve(self, mcp_request_context: Any) -> InvocationContext:
        token = _bearer_token_from_mcp_context(mcp_request_context)
        context = _context_from_jwt(token)
        context.require_scope("billing:read")
        self._store.register(context, token)
        return context


class VerifiedJwtIdentityResolver:
    """生产环境：HS256 验签并校验过期后才映射 InvocationContext。

    验签密钥由部署环境变量 ERP_BILLING_JWT_SECRET 注入，不进入
    配置文件模板；未配置密钥时拒绝构造，避免生产裸奔。
    """

    def __init__(self, store: SessionBearerStore, jwt_secret: str) -> None:
        if not jwt_secret.strip():
            raise DomainError(
                "mcp_unauthorized",
                "生产环境缺少 ERP_BILLING_JWT_SECRET，无法验签",
            )
        self._store = store
        self._jwt_secret = jwt_secret

    def resolve(self, mcp_request_context: Any) -> InvocationContext:
        token = _bearer_token_from_mcp_context(mcp_request_context)
        _decode_jwt_verified(token, self._jwt_secret)
        context = _context_from_jwt(token)
        context.require_scope("billing:read")
        self._store.register(context, token)
        return context


class BillingSessionToolSetResolver(McpToolSetResolver):
    """按 (tenant, account, session) 返回隔离的开单 ToolSet。"""

    def __init__(
        self,
        store: SessionBearerStore,
        settings: ErpBillingSettings,
        timeout_seconds: float,
    ) -> None:
        self._store = store
        self._settings = settings
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._toolsets: TTLCache[tuple[str, str, str], BillingToolSet] = TTLCache(
            maxsize=int(get_env_value("MCP_SESSION_MAX_SIZE", "500") or 500),
            ttl=int(get_env_value("MCP_SESSION_TTL_SECONDS", "3600") or 3600),
        )

    def resolve(self, context: InvocationContext) -> BillingToolSet:
        key = (context.tenant_id, context.account_id, context.session_id)
        with self._lock:
            toolset = self._toolsets.get(key)
        if toolset is not None:
            return toolset
        base_url = str(get_env_value("ERP_BILLING_BASE_URL")).strip()
        if not base_url:
            raise DomainError(
                "business_connection_invalid",
                "未配置 ERP_BILLING_BASE_URL",
            )
        http = BusinessAuthenticatedJsonClient(
            base_url=base_url,
            credential_provider=self._store,
            timeout_seconds=self._timeout_seconds,
        )
        api = ErpAuthenticatedHttpAdapter(http)
        session = ErpBillingSession.from_settings(
            replace(self._settings, product_catalog_path=None),
            allow_missing_catalog=True,
            match_logger=create_match_logger_from_env(),
        )
        toolset = BillingToolSet(session, api, InvocationContextStore())
        with self._lock:
            self._toolsets[key] = toolset
        return toolset


class _LazyBillingApp:
    """让 uvicorn 导入模块时不立即读取部署配置。"""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._app: Any | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self._app is None:
            self._app = self._factory()
        await self._app(scope, receive, send)


def _create_identity_resolver(bearer_store: SessionBearerStore) -> Any:
    """组合根唯一的环境分支：按 GJP_ENV 选择鉴权强度。"""
    if is_production():
        return VerifiedJwtIdentityResolver(
            bearer_store,
            get_env_value("ERP_BILLING_JWT_SECRET"),
        )
    return DirectJwtIdentityResolver(bearer_store)


def create_billing_app() -> Any:
    """装配固定 ERP URL、按环境选择身份解析的开单 MCP 应用。"""
    configure_logging()
    timeout_seconds = float(get_env_value("ERP_BILLING_TIMEOUT_SECONDS", "30") or 30)
    if timeout_seconds <= 0:
        raise DomainError("business_connection_invalid", "超时时间必须大于 0")
    settings = ErpBillingSettings.from_env()
    bearer_store = SessionBearerStore()
    schema_toolset = BillingToolSet(
        ErpBillingSession.from_settings(
            replace(settings, product_catalog_path=None),
            allow_missing_catalog=True,
            match_logger=create_match_logger_from_env(),
        ),
        UnavailableBillingApi(),
        InvocationContextStore(),
    )
    return create_billing_mcp_service(
        schema_toolset=schema_toolset,
        identity_resolver=_create_identity_resolver(bearer_store),
        toolset_resolver=BillingSessionToolSetResolver(
            bearer_store,
            settings,
            timeout_seconds,
        ),
    )


app = _LazyBillingApp(create_billing_app)

"""Token-only CLI 验证服务的凭据登记、Bearer 解析和 HTTP 工具。"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Generic, TypeVar

from gjp_common.context import InvocationContext
from gjp_common.errors import DomainError


ToolSetT = TypeVar("ToolSetT")


@dataclass(frozen=True, repr=False)
class ValidationCredential:
    """测试验证服务登记的临时凭据，不进入工具参数和模型上下文。"""

    context: InvocationContext
    upstream_bearer: str = ""
    expires_at: datetime | None = None

    def __repr__(self) -> str:
        return (
            "ValidationCredential(context=%r, upstream_bearer=<redacted>, expires_at=%r)"
            % (self.context, self.expires_at)
        )


class ValidationCredentialStore:
    """单进程测试凭据表；多进程部署时不要使用。"""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._by_bearer: dict[str, ValidationCredential] = {}
        self._by_context: dict[tuple[str, str, str], ValidationCredential] = {}

    def require_context(self, context: InvocationContext) -> ValidationCredential:
        with self._lock:
            credential = self._by_context.get(self._context_key(context))
        if credential is None:
            raise DomainError("MCP_UNAUTHORIZED", "当前 MCP 会话没有登记测试凭据")
        self._assert_alive(credential)
        return credential

    def _evict_expired(self) -> None:
        """淘汰过期凭据，避免长期运行的验证服务凭据表只增不减。"""
        now = datetime.now(timezone.utc)
        expired_bearers = [
            bearer
            for bearer, credential in self._by_bearer.items()
            if credential.expires_at and now >= credential.expires_at
        ]
        for bearer in expired_bearers:
            del self._by_bearer[bearer]
        expired_keys = [
            key
            for key, credential in self._by_context.items()
            if credential.expires_at and now >= credential.expires_at
        ]
        for key in expired_keys:
            del self._by_context[key]

    @staticmethod
    def _context_key(context: InvocationContext) -> tuple[str, str, str]:
        return context.tenant_id, context.account_id, context.session_id

    def register_upstream_token(self, token: str) -> InvocationContext:
        """直接用 ERP JWT 作为 Bearer key 登记凭据。

        从 JWT payload 解析 tenantId、loginId 构造 InvocationContext，
        并把 JWT 本身同时作为 Bearer key 和 upstream_bearer 存储，
        使 ErpBearerJsonClient 能用同一个 JWT 调用 ERP API。
        """
        context = _context_from_jwt(token)
        with self._lock:
            self._evict_expired()
            existing = self._by_bearer.get(token)
            if existing is not None:
                self._assert_alive(existing)
                return existing.context
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
            credential = ValidationCredential(
                context=context,
                upstream_bearer=token,
                expires_at=expires_at,
            )
            self._by_bearer[token] = credential
            self._by_context[self._context_key(context)] = credential
        return context

    @staticmethod
    def _assert_alive(credential: ValidationCredential) -> None:
        if credential.expires_at and datetime.now(timezone.utc) >= credential.expires_at:
            raise DomainError("MCP_UNAUTHORIZED", "Authorization Bearer 已过期")


def _context_from_jwt(token: str) -> InvocationContext:
    """从 ERP JWT payload 解析身份信息；不验签，ERP API 会拒绝过期 JWT。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise DomainError("MCP_UNAUTHORIZED", "ERP JWT 格式无效")
    padding = "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except (ValueError, json.JSONDecodeError) as exc:
        raise DomainError("MCP_UNAUTHORIZED", "ERP JWT payload 解析失败") from exc
    if not isinstance(payload, dict):
        raise DomainError("MCP_UNAUTHORIZED", "ERP JWT payload 不是对象")
    tenant_id = str(payload.get("tenantId") or "unknown")
    login_id = str(payload.get("loginId") or "unknown")
    return InvocationContext(
        tenant_id=tenant_id,
        subject_id=login_id,
        account_id=tenant_id,
        session_id="billing-" + login_id,
        scopes=frozenset({"billing:read", "billing:write"}),
    )


class ExpiringToolSetCache(Generic[ToolSetT]):
    """按 (tenant, account, session) 缓存 ToolSet，闲置超过 TTL 自动淘汰。

    与 Bearer TTL 对齐：Bearer 过期后 require_context 已拒绝访问，本缓存
    负责在每次访问时清理过期条目，避免长期运行的验证服务内存泄漏。
    活跃会话每次命中会刷新过期时间。
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str, str], tuple[float, ToolSetT]] = {}

    def get_or_create(
        self,
        context: InvocationContext,
        factory: Callable[[], ToolSetT],
    ) -> ToolSetT:
        key = (context.tenant_id, context.account_id, context.session_id)
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            entry = self._entries.get(key)
            if entry is not None:
                self._entries[key] = (now + self._ttl_seconds, entry[1])
                return entry[1]
        toolset = factory()
        with self._lock:
            self._entries[key] = (now + self._ttl_seconds, toolset)
        return toolset

    def _evict(self, now: float) -> None:
        expired = [key for key, (deadline, _) in self._entries.items() if now >= deadline]
        for key in expired:
            del self._entries[key]


class DirectJwtIdentityResolver:
    """直接从 MCP Bearer 解析 ERP JWT 并映射为 InvocationContext。"""

    def __init__(
        self,
        store: ValidationCredentialStore,
        *,
        required_scope: str,
    ) -> None:
        self._store = store
        self._required_scope = required_scope

    def resolve(self, mcp_request_context: Any) -> InvocationContext:
        token = bearer_token_from_mcp_context(mcp_request_context)
        context = self._store.register_upstream_token(token)
        context.require_scope(self._required_scope)
        return context


class LazyValidationApp:
    """让 uvicorn 导入模块时不立即读取测试登录配置。"""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._app: Any | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self._app is None:
            self._app = self._factory()
        await self._app(scope, receive, send)


def bearer_token_from_mcp_context(mcp_request_context: Any) -> str:
    request = getattr(mcp_request_context, "request", None)
    headers = getattr(request, "headers", None)
    authorization = headers.get("authorization", "") if headers is not None else ""
    if not authorization:
        raise DomainError("MCP_UNAUTHORIZED", "缺少 Authorization: Bearer <token>")
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not token.strip():
        raise DomainError("MCP_UNAUTHORIZED", "Authorization 必须使用 Bearer token")
    return token.strip()


def float_value(value: str, default: float) -> float:
    raw = value.strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise DomainError("VALIDATION_CONFIG_INVALID", "超时时间必须是数字") from exc
    if parsed <= 0:
        raise DomainError("VALIDATION_CONFIG_INVALID", "超时时间必须大于 0")
    return parsed


def int_value(value: str, default: int) -> int:
    raw = value.strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise DomainError("VALIDATION_CONFIG_INVALID", "TTL 必须是整数") from exc
    if parsed <= 0:
        raise DomainError("VALIDATION_CONFIG_INVALID", "TTL 必须大于 0")
    return parsed


def http_read_json(
    opener: urllib.request.OpenerDirector,
    request: urllib.request.Request,
    timeout_seconds: float,
) -> Any:
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8-sig")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise DomainError(
            "VALIDATION_HTTP_FAILED",
            "ERP 接口 HTTP %s：%s" % (exc.code, body[:300] or exc.reason),
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DomainError("VALIDATION_HTTP_FAILED", "ERP 接口不可用或超时") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        raise DomainError("VALIDATION_RESPONSE_INVALID", "ERP 接口返回的不是 JSON，耗时 %dms" % elapsed_ms) from exc

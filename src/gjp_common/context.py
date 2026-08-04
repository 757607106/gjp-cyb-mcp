"""业务能力调用上下文。

鉴权凭据不属于工具参数。对接方先完成认证，再把认证结果映射为本上下文；
业务 API Adapter 根据上下文选择当前租户、账号和访问凭据。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Iterator

from .errors import DomainError


@dataclass(frozen=True)
class InvocationContext:
    """一次业务能力调用的身份与链路信息，不保存密码或访问令牌。"""

    tenant_id: str
    subject_id: str
    account_id: str
    session_id: str = ""
    request_id: str = ""
    scopes: frozenset[str] = field(default_factory=frozenset)

    def require_scope(self, scope: str) -> None:
        """由能力层做最后一道权限检查。"""
        if scope not in self.scopes:
            raise DomainError("CAPABILITY_FORBIDDEN", "当前调用方缺少权限：%s" % scope)


class InvocationContextStore:
    """用 ContextVar 隔离并发请求，避免把用户身份放进模型可见参数。"""

    def __init__(self, default: InvocationContext | None = None) -> None:
        self._current: ContextVar[InvocationContext | None] = ContextVar(
            "gjp_invocation_context",
            default=default,
        )

    def get(self) -> InvocationContext:
        context = self._current.get()
        if context is None:
            raise DomainError("INVOCATION_CONTEXT_REQUIRED", "当前请求缺少已认证的业务上下文")
        return context

    @contextmanager
    def bind(self, context: InvocationContext) -> Iterator[None]:
        """只在当前异步任务中绑定上下文，请求结束后立即恢复。"""
        token: Token[InvocationContext | None] = self._current.set(context)
        try:
            yield
        finally:
            self._current.reset(token)

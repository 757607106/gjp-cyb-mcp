"""WorkBuddy 专用开单 MCP 组合入口。

运行方式：

    uv run uvicorn erp_billing.workbuddy_app:app --host 0.0.0.0 --port 8102

原有 ``erp_billing.app:app`` 保持不变；本入口只增加 WorkBuddy OAuth、ERP
AI Token 绑定和 HTTP 保护层。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from gjp_common.config import get_env_value
from gjp_common.context import InvocationContextStore
from gjp_common.errors import DomainError
from gjp_common.logging_config import configure_logging

from .adapters import UnavailableBillingApi, create_match_logger_from_env
from .app import BillingSessionToolSetResolver
from .config import ErpBillingSettings
from .mcp_service import create_billing_mcp_service
from .session import ErpBillingSession
from .toolset import BillingToolSet
from .workbuddy_oauth import (
    EncryptedSqliteOAuthStore,
    ErpApiKeyBindingValidator,
    ErpBindingValidator,
    WorkBuddyCredentialProvider,
    WorkBuddyIdentityResolver,
    WorkBuddyOAuthProtectionMiddleware,
    WorkBuddyOAuthProvider,
    WorkBuddyOAuthSettings,
    create_workbuddy_oauth_routes,
)

__all__ = ["app", "create_workbuddy_billing_app"]


def create_workbuddy_billing_app(
    *,
    oauth_settings: WorkBuddyOAuthSettings | None = None,
    oauth_store: EncryptedSqliteOAuthStore | None = None,
    binding_validator: ErpBindingValidator | None = None,
) -> Any:
    """装配独立的 WorkBuddy OAuth MCP 服务。"""
    configure_logging()
    settings = oauth_settings or WorkBuddyOAuthSettings.from_env()
    store = oauth_store or EncryptedSqliteOAuthStore(
        settings.database_path,
        settings.encryption_key,
    )
    timeout_seconds = float(get_env_value("ERP_BILLING_TIMEOUT_SECONDS", "30") or 30)
    if timeout_seconds <= 0:
        raise DomainError("business_connection_invalid", "超时时间必须大于 0")
    erp_settings = ErpBillingSettings.from_env()
    validator = binding_validator
    if validator is None:
        base_url = get_env_value("ERP_BILLING_BASE_URL").strip()
        if not base_url:
            raise DomainError("business_connection_invalid", "未配置 ERP_BILLING_BASE_URL")
        validator = ErpApiKeyBindingValidator(base_url, timeout_seconds)

    oauth_provider = WorkBuddyOAuthProvider(settings, store, validator)
    credential_provider = WorkBuddyCredentialProvider(oauth_provider)
    schema_toolset = BillingToolSet(
        ErpBillingSession.from_settings(
            replace(erp_settings, product_catalog_path=None),
            allow_missing_catalog=True,
            match_logger=create_match_logger_from_env(),
        ),
        UnavailableBillingApi(),
        InvocationContextStore(),
    )
    toolset_resolver = BillingSessionToolSetResolver(
        credential_provider,
        erp_settings,
        timeout_seconds,
    )

    async def shutdown() -> None:
        await toolset_resolver.close()
        store.close()

    service = create_billing_mcp_service(
        schema_toolset=schema_toolset,
        identity_resolver=WorkBuddyIdentityResolver(oauth_provider),
        toolset_resolver=toolset_resolver,
        extra_routes=create_workbuddy_oauth_routes(oauth_provider),
        shutdown=shutdown,
    )
    return WorkBuddyOAuthProtectionMiddleware(service, oauth_provider)


class _LazyWorkBuddyApp:
    """让 uvicorn 导入模块时不立即创建数据库或读取秘密配置。"""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._app: Any | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self._app is None:
            self._app = self._factory()
        await self._app(scope, receive, send)


app = _LazyWorkBuddyApp(create_workbuddy_billing_app)

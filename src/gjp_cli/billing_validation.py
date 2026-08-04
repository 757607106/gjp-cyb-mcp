"""固定 ERP URL + 已有 Token 的开单 MCP CLI 验证服务。

运行方式：
uv run uvicorn gjp_cli.billing_validation:app --host 0.0.0.0 --port 8102
"""

from __future__ import annotations

import json
import logging
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from erp_billing.adapters import ErpAuthenticatedHttpAdapter, UnavailableBillingApi
from erp_billing.config import ErpBillingSettings
from erp_billing.mcp_service import create_billing_mcp_service
from erp_billing.ports import AuthenticatedJsonClient
from erp_billing.session import ErpBillingSession
from erp_billing.toolset import BillingToolSet
from gjp_common.config import get_env_value
from gjp_common.connections import business_api_url, normalize_business_api_base_url
from gjp_common.context import InvocationContext, InvocationContextStore
from gjp_common.errors import DomainError
from gjp_common.logging_config import (
    clip_log_text,
    configure_logging,
    credential_dump_enabled,
    elapsed_ms,
)
from gjp_common.mcp import McpToolSetResolver
from .validation import (
    BearerTokenIdentityResolver,
    ExpiringToolSetCache,
    LazyValidationApp,
    ValidationCredentialStore,
    error_response,
    float_value,
    http_read_json,
    int_value,
    issue_bearer_response,
    read_json_object,
)

logger = logging.getLogger(__name__)


class ErpBearerJsonClient:
    """使用固定 ERP URL 和当前 CLI 会话中的上游 Bearer。"""

    def __init__(
        self,
        store: ValidationCredentialStore,
        base_url: str,
        timeout_seconds: float = 30,
    ) -> None:
        self._store = store
        self._base_url = normalize_business_api_base_url(base_url)
        self._timeout_seconds = timeout_seconds

    def get_json(
        self,
        context: InvocationContext,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request_json(context, "GET", path, params=params)

    def post_json(
        self,
        context: InvocationContext,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request_json(context, "POST", path, payload=payload)

    def _request_json(
        self,
        context: InvocationContext,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        credential = self._store.require_context(context)
        if not credential.upstream_bearer:
            raise DomainError("ERP_LIVE_AUTH_REQUIRED", "当前 MCP 会话缺少 ERP Bearer")
        url = business_api_url(self._base_url, path)
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + credential.upstream_bearer,
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "ERP 请求开始 method=%s url=%s headers=%s",
                method,
                url,
                json.dumps(headers, ensure_ascii=False)
                if credential_dump_enabled()
                else "<已脱敏>",
            )
        started = time.perf_counter()
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )
        data = http_read_json(urllib.request.build_opener(), request, self._timeout_seconds)
        if not isinstance(data, dict):
            raise DomainError("ERP_LIVE_RESPONSE_INVALID", "ERP 接口响应顶层不是对象")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "ERP 请求完成 url=%s elapsed=%dms body=%s",
                url,
                elapsed_ms(started),
                clip_log_text(json.dumps(data, ensure_ascii=False)),
            )
        return data


class BillingValidationToolSetResolver(McpToolSetResolver):
    """按测试 Bearer 对应的会话返回开单 ToolSet，闲置会话按 TTL 淘汰。"""

    def __init__(
        self,
        store: ValidationCredentialStore,
        settings: ErpBillingSettings,
        http_client: AuthenticatedJsonClient,
    ) -> None:
        self._store = store
        self._settings = settings
        self._http_client = http_client
        self._toolsets: ExpiringToolSetCache[BillingToolSet] = ExpiringToolSetCache(
            store.ttl_seconds,
        )

    def resolve(self, context: InvocationContext) -> BillingToolSet:
        self._store.require_context(context)
        return self._toolsets.get_or_create(context, self._build_toolset)

    def _build_toolset(self) -> BillingToolSet:
        api = ErpAuthenticatedHttpAdapter(self._http_client)
        session = ErpBillingSession.from_settings(
            replace(self._settings, product_catalog_path=None),
            allow_missing_catalog=True,
        )
        return BillingToolSet(session, api, InvocationContextStore())


def create_billing_validation_app(
    *,
    base_url: str | None = None,
    http_client: AuthenticatedJsonClient | None = None,
    store: ValidationCredentialStore | None = None,
) -> Any:
    """创建固定 ERP URL、Token-only 的 CLI 验证 ASGI 应用。"""
    configure_logging()
    timeout_seconds = float_value(get_env_value("ERP_BILLING_TIMEOUT_SECONDS", "30"), 30)
    ttl_hours = int_value(get_env_value("MCP_VALIDATION_TOKEN_TTL_HOURS", "1"), 1)
    fixed_base_url = normalize_business_api_base_url(
        str(base_url or get_env_value("ERP_BILLING_BASE_URL")).strip(),
    )
    credential_store = store or ValidationCredentialStore(ttl_seconds=ttl_hours * 3600)
    http = http_client or ErpBearerJsonClient(
        credential_store,
        fixed_base_url,
        timeout_seconds,
    )
    settings = ErpBillingSettings.from_env()
    schema_toolset = BillingToolSet(
        ErpBillingSession.from_settings(
            replace(settings, product_catalog_path=None),
            allow_missing_catalog=True,
        ),
        UnavailableBillingApi(),
        InvocationContextStore(),
    )

    async def test_token(request: Request) -> JSONResponse:
        """仅供 CLI 验证：登记已有上游 Token 并签发独立 MCP Bearer。"""
        try:
            payload = await read_json_object(request)
            if "appBaseUrl" in payload or "baseUrl" in payload:
                raise DomainError(
                    "ERP_LIVE_CONFIG_INVALID",
                    "ERP URL 是固定部署配置，Token 请求不接受动态 URL",
                )
            upstream_token = str(
                payload.get("upstreamToken")
                or get_env_value("ERP_BILLING_UPSTREAM_TOKEN")
            ).strip()
            scheme, separator, credential = upstream_token.partition(" ")
            if separator and scheme.casefold() == "bearer":
                upstream_token = credential.strip()
            if not upstream_token:
                raise DomainError(
                    "ERP_LIVE_AUTH_REQUIRED",
                    "缺少 upstreamToken 或 ERP_BILLING_UPSTREAM_TOKEN",
                )
            session_id = str(payload.get("session_id") or "billing-" + secrets.token_urlsafe(12))
            subject_id = str(payload.get("subject_id") or "cli-token-user").strip() or "cli-token-user"
            tenant_id = str(payload.get("tenant_id") or "cli-token-tenant").strip() or "cli-token-tenant"
            context = InvocationContext(
                tenant_id=tenant_id,
                subject_id=subject_id,
                account_id=str(payload.get("account_id") or tenant_id).strip() or tenant_id,
                session_id=session_id,
                scopes=frozenset({"billing:read", "billing:write"}),
            )
            return issue_bearer_response(
                credential_store,
                context,
                upstream_bearer=upstream_token,
            )
        except DomainError as exc:
            return error_response(exc)

    return create_billing_mcp_service(
        schema_toolset=schema_toolset,
        identity_resolver=BearerTokenIdentityResolver(
            credential_store,
            required_scope="billing:read",
        ),
        toolset_resolver=BillingValidationToolSetResolver(
            credential_store,
            settings,
            http,
        ),
        extra_routes=[
            Route("/test-auth/token", test_token, methods=["POST"]),
        ],
    )


app = LazyValidationApp(create_billing_validation_app)

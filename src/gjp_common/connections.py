"""服务端业务凭据解析与固定 API 地址校验。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlparse

from .context import InvocationContext
from .errors import DomainError


CredentialKind = Literal["bearer"]


def normalize_business_api_base_url(value: str) -> str:
    """校验并规范化部署级固定业务 API 地址。"""
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or not parsed.hostname:
        raise DomainError("business_connection_invalid", "业务 API 地址必须是有效的 HTTPS 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DomainError(
            "business_connection_invalid",
            "业务 API 地址不能包含用户信息、查询参数或片段",
        )
    return normalized


def business_api_url(base_url: str, path: str) -> str:
    """把源码中的固定相对路径拼到部署级 API 地址。"""
    normalized = path.strip()
    if (
        not normalized.startswith("/")
        or normalized.startswith("//")
        or "?" in normalized
        or "#" in normalized
        or "://" in normalized
    ):
        raise DomainError("business_api_path_invalid", "业务 API 路径无效")
    return normalize_business_api_base_url(base_url) + normalized


@dataclass(frozen=True, repr=False)
class BusinessApiCredential:
    """一次业务 API 会话使用的服务端凭据；repr 永远不输出秘密值。"""

    kind: CredentialKind
    value: str

    def __post_init__(self) -> None:
        if self.kind != "bearer":
            raise DomainError("business_credential_invalid", "不支持的业务 API 鉴权类型")
        if not self.value.strip():
            raise DomainError("business_credential_required", "业务端未提供当前会话的鉴权信息")

    def __repr__(self) -> str:
        return "BusinessApiCredential(kind=%r, value=<redacted>)" % self.kind


class BusinessApiCredentialProvider(Protocol):
    """由 SaaS 会话存储实现的 Bearer 解析入口；不解析业务 URL。"""

    def resolve(self, context: InvocationContext) -> BusinessApiCredential:
        ...

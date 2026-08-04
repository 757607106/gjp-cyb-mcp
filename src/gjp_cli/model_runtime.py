"""本地 CLI 的模型配置与 AgentScope 模型运行时。

模型厂商注册表、凭证、参数 schema 和流式行为都来自 AgentScope。生产 MCP
服务不构建模型；本模块只服务于 gjp_cli 的本地验证链路，不进入生产部署。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from agentscope.credential import CredentialFactory
from agentscope.model import ChatModelBase
from pydantic import ValidationError

from gjp_common.config import get_env_value
from gjp_common.errors import DomainError


def _env_bool(name: str, default: bool) -> bool:
    value = get_env_value(name, "true" if default else "false").strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise DomainError("MODEL_CONFIG_INVALID", "%s 必须是 true 或 false" % name)


def _env_int(name: str, default: Optional[int], minimum: int) -> Optional[int]:
    value = get_env_value(name, "" if default is None else str(default)).strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise DomainError("MODEL_CONFIG_INVALID", "%s 必须是整数" % name) from exc
    if parsed < minimum:
        raise DomainError("MODEL_CONFIG_INVALID", "%s 不能小于 %d" % (name, minimum))
    return parsed


def _env_json_object(name: str) -> dict[str, Any]:
    value = get_env_value(name, "{}").strip() or "{}"
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise DomainError("MODEL_CONFIG_INVALID", "%s 必须是 JSON 对象" % name) from exc
    if not isinstance(parsed, dict):
        raise DomainError("MODEL_CONFIG_INVALID", "%s 必须是 JSON 对象" % name)
    return parsed


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model_name: str
    api_key: str
    base_url: Optional[str]
    stream: bool
    parameters: dict[str, Any]
    timeout_seconds: float
    max_retries: int
    context_size: Optional[int]
    env_prefix: str = "LLM_TEXT"

    @classmethod
    def from_env(cls) -> "LLMSettings":
        """读取 LLM_TEXT_* 配置，构建文本对话模型。"""
        return cls._from_env_prefix(
            "LLM_TEXT",
            default_provider="deepseek",
            default_model="deepseek-chat",
            default_stream=True,
        )

    @classmethod
    def vision_from_env(cls) -> "LLMSettings":
        """读取 LLM_VISION_* 配置，构建图片识别的多模态模型；单次调用默认不流式。"""
        return cls._from_env_prefix(
            "LLM_VISION",
            default_provider="openai",
            default_model="",
            default_stream=False,
        )

    @classmethod
    def _from_env_prefix(
        cls,
        prefix: str,
        *,
        default_provider: str,
        default_model: str,
        default_stream: bool,
    ) -> "LLMSettings":
        provider = get_env_value(prefix + "_PROVIDER", default_provider).strip().casefold()
        model_name = get_env_value(prefix + "_MODEL_NAME", default_model).strip()
        api_key = get_env_value(prefix + "_API_KEY").strip()
        base_url = get_env_value(prefix + "_BASE_URL").strip().rstrip("/") or None
        try:
            timeout_seconds = float(get_env_value(prefix + "_TIMEOUT_SECONDS", "60"))
        except ValueError as exc:
            raise DomainError("MODEL_CONFIG_INVALID", "%s_TIMEOUT_SECONDS 必须是数字" % prefix) from exc
        if not provider:
            raise DomainError("MODEL_CONFIG_INVALID", "缺少 %s_PROVIDER" % prefix)
        if not model_name:
            raise DomainError("MODEL_CONFIG_INVALID", "缺少 %s_MODEL_NAME" % prefix)
        if timeout_seconds <= 0:
            raise DomainError("MODEL_CONFIG_INVALID", "%s_TIMEOUT_SECONDS 必须大于 0" % prefix)
        if base_url and not base_url.startswith(("https://", "http://")):
            raise DomainError(
                "MODEL_CONFIG_INVALID",
                "%s_BASE_URL 必须是 http:// 或 https:// 地址" % prefix,
            )
        settings = cls(
            provider=provider,
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            stream=_env_bool(prefix + "_STREAM", default_stream),
            parameters=_env_json_object(prefix + "_PARAMETERS"),
            timeout_seconds=timeout_seconds,
            max_retries=_env_int(prefix + "_MAX_RETRIES", 3, 0) or 0,
            context_size=_env_int(prefix + "_CONTEXT_SIZE", None, 1),
            env_prefix=prefix,
        )
        return settings

    @property
    def credential_type(self) -> str:
        normalized = self.provider.replace("-", "_")
        return normalized if normalized.endswith("_credential") else normalized + "_credential"


def supported_model_providers() -> list[str]:
    """返回 AgentScope 凭证注册表中可用的模型厂商名称。"""
    providers: list[str] = []
    for schema in CredentialFactory.list_schemas():
        value = schema.get("properties", {}).get("type", {}).get("const")
        if isinstance(value, str):
            providers.append(value.removesuffix("_credential"))
    return sorted(set(providers))


def build_chat_model(settings: LLMSettings) -> ChatModelBase:
    """根据项目配置创建 AgentScope ChatModel，并保留原生流式能力。"""
    credential_cls = CredentialFactory.get_credential_class(settings.credential_type)
    if credential_cls is None:
        raise DomainError(
            "MODEL_PROVIDER_UNSUPPORTED",
            "不支持模型厂商 %s；可选：%s"
            % (settings.provider, "、".join(supported_model_providers())),
        )

    credential_fields = credential_cls.model_fields
    credential_data: dict[str, object] = {"type": settings.credential_type}
    if "api_key" in credential_fields:
        if not settings.api_key:
            raise DomainError("MODEL_CONFIG_INVALID", "缺少 %s_API_KEY" % settings.env_prefix)
        credential_data["api_key"] = settings.api_key
    if settings.base_url:
        if "base_url" in credential_fields:
            credential_data["base_url"] = settings.base_url
        elif "host" in credential_fields:
            credential_data["host"] = settings.base_url
        elif "api_host" in credential_fields:
            parsed = urlparse(settings.base_url)
            credential_data["api_host"] = parsed.netloc or parsed.path
        else:
            raise DomainError(
                "MODEL_CONFIG_INVALID",
                "%s 不支持自定义 %s_BASE_URL" % (settings.provider, settings.env_prefix),
            )

    try:
        credential = CredentialFactory.from_dict(credential_data)
        model_cls = credential_cls.get_chat_model_class()
        unknown_parameters = set(settings.parameters) - set(model_cls.Parameters.model_fields)
        if unknown_parameters:
            raise DomainError(
                "MODEL_CONFIG_INVALID",
                "%s 不支持模型参数：%s"
                % (settings.provider, "、".join(sorted(unknown_parameters))),
            )
        parameters = model_cls.Parameters.model_validate(settings.parameters)
    except ValidationError as exc:
        raise DomainError(
            "MODEL_CONFIG_INVALID",
            "模型凭证或参数无效：%s" % exc.errors(include_url=False)[0]["msg"],
        ) from exc

    model_kwargs: dict[str, object] = {
        "credential": credential,
        "model": settings.model_name,
        "parameters": parameters,
        "stream": settings.stream,
        "max_retries": settings.max_retries,
        "client_kwargs": {"timeout": settings.timeout_seconds},
    }
    if settings.context_size is not None:
        model_kwargs["context_size"] = settings.context_size
    return model_cls(**model_kwargs)

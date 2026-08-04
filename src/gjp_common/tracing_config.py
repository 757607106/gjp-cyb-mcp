"""链路追踪配置模块：基于 OpenTelemetry 注册 TracerProvider，支持 OTLP 导出。

与 logging_config.py 平行，由 CLI 入口在启动时调用一次 configure_tracing()。
AgentScope 内置的 TracingMiddleware 会检测 TracerProvider 是否已注册：
  - 已注册 → 在 on_reply / on_model_call / on_acting 三个位置生成嵌套 span
  - 未注册 → 所有 hook 短路到 next_handler()，几乎零开销
"""

from __future__ import annotations

import logging
import os
from typing import Dict

from .config import _read_local_env


logger = logging.getLogger(__name__)

TRUE_VALUES = {"1", "true", "yes", "on", "enable", "enabled"}
FALSE_VALUES = {"0", "false", "no", "off", "disable", "disabled"}

_TRACING_CONFIGURED = False


def _setting(local_env: Dict[str, str], name: str, default: str) -> str:
    return os.getenv(name, local_env.get(name, default)).strip()


def _enabled(value: str) -> bool:
    normalized = value.lower()
    if normalized in FALSE_VALUES:
        return False
    if normalized in TRUE_VALUES:
        return True
    return True


def tracing_runtime_settings() -> Dict[str, object]:
    """返回当前追踪运行时配置快照，供 doctor 命令和交互模式查询。"""
    local_env = _read_local_env()
    return {
        "enabled": _enabled(_setting(local_env, "GJP_TRACING_ENABLED", "false")),
        "otlpEndpoint": _setting(
            local_env, "GJP_OTLP_ENDPOINT", "http://localhost:4318/v1/traces"
        ),
        "serviceName": _setting(
            local_env, "GJP_TRACING_SERVICE_NAME", "gjp-agent"
        ),
    }


def configure_tracing() -> bool:
    """注册 OpenTelemetry TracerProvider，返回是否成功启用。

    幂等调用：首次成功后再次调用直接返回 True。
    缺少 opentelemetry 依赖或 OTLP 连接异常时降级为禁用，不阻断主流程。
    """
    global _TRACING_CONFIGURED
    if _TRACING_CONFIGURED:
        return True

    local_env = _read_local_env()
    enabled = _enabled(_setting(local_env, "GJP_TRACING_ENABLED", "false"))
    otlp_endpoint = _setting(
        local_env, "GJP_OTLP_ENDPOINT", "http://localhost:4318/v1/traces"
    )

    if not enabled:
        logger.info("链路追踪未启用（GJP_TRACING_ENABLED=false）")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError:
        logger.warning(
            "链路追踪已启用但缺少 opentelemetry 依赖，请运行 uv sync 安装"
        )
        return False

    try:
        service_name = _setting(local_env, "GJP_TRACING_SERVICE_NAME", "gjp-agent")
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint))
        )
        trace.set_tracer_provider(provider)
        _TRACING_CONFIGURED = True
        logger.info(
            "链路追踪已启用 service=%s otlp_endpoint=%s",
            service_name, otlp_endpoint,
        )
        return True
    except Exception as exc:
        logger.warning("链路追踪初始化失败，已降级为禁用：%s", exc)
        return False

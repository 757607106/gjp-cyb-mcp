"""日志配置模块：根据.env控制终端阶段日志和脱敏后的模型上下文日志。"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Dict

from .config import _read_local_env


LOGGER_NAME = "gjp_common"
# 统一挂载 handler 的包命名空间；只含本项目业务包，不接管第三方 SDK 的协议层日志
PACKAGE_LOGGERS = ("gjp_common", "erp_billing")
# 凭据原文转储开关：独立于日志级别，仅本地调试开启
CREDENTIAL_DUMP_ENV = "GJP_DEBUG_DUMP_CREDENTIALS"
# 单条日志中业务参数与响应体的最大字符数，避免长文本淹没终端
LOG_TEXT_LIMIT = 2000
TRUE_VALUES = {"1", "true", "yes", "on", "enable", "enabled"}
FALSE_VALUES = {"0", "false", "no", "off", "disable", "disabled"}
LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class _DynamicStderrHandler(logging.StreamHandler):
    """每次输出重新绑定当前stderr，避免测试捕获结束后引用已关闭流。"""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def _setting(local_env: Dict[str, str], name: str, default: str) -> str:
    return os.getenv(name, local_env.get(name, default)).strip()


def _enabled(value: str) -> bool:
    normalized = value.lower()
    if normalized in FALSE_VALUES:
        return False
    if normalized in TRUE_VALUES:
        return True
    return True


def credential_dump_enabled() -> bool:
    """是否允许在 DEBUG 日志中输出凭据原文（Authorization、Cookie）。

    与日志级别完全独立且默认关闭：生产即使整体跑 DEBUG，只要不显式开启本开关
    就不会把访问令牌写进日志。仅接受明确的真值，未识别的取值一律视为关闭。
    """
    local_env = _read_local_env()
    return _setting(local_env, CREDENTIAL_DUMP_ENV, "false").lower() in TRUE_VALUES


def clip_log_text(text: str) -> str:
    """截断超长日志文本并标注原始长度，避免刷屏又不丢失规模信息。"""
    if len(text) <= LOG_TEXT_LIMIT:
        return text
    return "%s…(len=%d)" % (text[:LOG_TEXT_LIMIT], len(text))


def elapsed_ms(started: float) -> int:
    """返回自 started（time.perf_counter 取值）起经过的毫秒数。"""
    return int((time.perf_counter() - started) * 1000)


def error_text(exc: BaseException) -> str:
    """统一异常日志文本为 类型: 描述，便于按异常类型聚合排查。"""
    return clip_log_text("%s: %s" % (type(exc).__name__, exc))


def configure_logging() -> bool:
    """Configure package logs on stderr and return whether logging is enabled."""
    local_env = _read_local_env()
    enabled = _enabled(_setting(local_env, "GJP_LOG_ENABLED", "true"))
    level_name = _setting(local_env, "GJP_LOG_LEVEL", "INFO").upper()
    level = LEVELS.get(level_name, logging.INFO)
    context_enabled = _enabled(_setting(local_env, "GJP_LOG_CONTEXT", "false"))

    package_loggers = [logging.getLogger(name) for name in PACKAGE_LOGGERS]
    for package_logger in package_loggers:
        package_logger.handlers.clear()
        package_logger.propagate = False
    if not enabled:
        for package_logger in package_loggers:
            package_logger.setLevel(logging.CRITICAL + 1)
            package_logger.addHandler(logging.NullHandler())
        return False

    handler = _DynamicStderrHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    for package_logger in package_loggers:
        package_logger.setLevel(level)
        package_logger.addHandler(handler)
    logging.getLogger(LOGGER_NAME).info(
        "终端执行日志已开启 level=%s model_context=%s",
        logging.getLevelName(level),
        context_enabled,
    )
    if level_name not in LEVELS:
        logging.getLogger(LOGGER_NAME).warning("未知日志级别 %s，已使用 INFO", level_name)
    return True

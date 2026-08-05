"""配置模块：按运行环境加载 config 目录下的环境文件。

环境区分：``GJP_ENV`` 取值 local（默认，即测试环境）或 production，
分别加载 ``config/local.env`` 与 ``config/production.env``。
显式指定 ``GJP_ENV_FILE`` 或对应系统环境变量时优先于环境文件的值。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional

from .errors import DomainError
from .paths import discover_project_root


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 环境名与 config 目录下对应文件名的映射：本地即测试环境，生产使用独立文件
_ENV_DIR = "config"
_ENV_FILE_NAMES: Dict[str, str] = {
    "local": "local.env",
    "production": "production.env",
}


def current_env_name() -> str:
    """返回当前运行环境名，非法取值回退到 local。"""
    value = os.getenv("GJP_ENV", "local").strip().casefold()
    return value if value in _ENV_FILE_NAMES else "local"


def is_production() -> bool:
    """当前是否生产环境；供组合根选择环境专属装配，业务代码不应调用。"""
    return current_env_name() == "production"


def local_env_path() -> Optional[Path]:
    explicit_path = os.getenv("GJP_ENV_FILE")
    if explicit_path:
        path = Path(explicit_path).expanduser()
        return path.resolve() if path.is_file() else None
    file_name = _ENV_FILE_NAMES[current_env_name()]
    for root in (Path.cwd(), discover_project_root()):
        candidate = root / _ENV_DIR / file_name
        if candidate.is_file():
            return candidate.resolve()
    return None


_local_env_cache: Optional[Dict[str, str]] = None
_local_env_cached_path: Optional[str] = None


def _read_local_env() -> Dict[str, str]:
    """Read a project-local .env without overriding process environment values.

    The file is parsed once per unique path and cached for the lifetime of
    the process.  Environment variables set via ``os.environ`` always take
    precedence.
    """
    global _local_env_cache, _local_env_cached_path
    path = local_env_path()
    path_key = str(path) if path is not None else ""
    if path_key == _local_env_cached_path:
        return _local_env_cache or {}
    _local_env_cached_path = path_key
    if path is not None:
        result: Dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise DomainError("model_config_invalid", "无法读取本地环境文件：%s" % path) from exc
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not _ENV_KEY.fullmatch(key):
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            result[key] = value
        _local_env_cache = result
        return result
    _local_env_cache = {}
    return {}


def get_env_value(name: str, default: str = "") -> str:
    return os.getenv(name, _read_local_env().get(name, default))

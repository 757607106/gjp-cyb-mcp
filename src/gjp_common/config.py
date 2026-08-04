"""配置模块：自动加载项目.env，为公共层与各产品提供环境变量读取。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional

from .errors import DomainError
from .paths import discover_project_root


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def local_env_path() -> Optional[Path]:
    explicit_path = os.getenv("GJP_ENV_FILE")
    candidates = (
        [Path(explicit_path).expanduser()]
        if explicit_path
        else [Path.cwd() / ".env", discover_project_root() / ".env"]
    )
    for path in candidates:
        if path.is_file():
            return path.resolve()
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
            raise DomainError("MODEL_CONFIG_INVALID", "无法读取本地环境文件：%s" % path) from exc
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

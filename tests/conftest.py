"""测试全局配置：隔离项目环境配置文件，确保 monkeypatch 环境变量测试可重现。

项目 config 目录下的环境文件包含真实环境配置，不应泄漏到单元测试。
get_env_value 的 fallback 机制会按 GJP_ENV 读取对应环境文件缓存，
导致 monkeypatch.delenv 无法完全隔离环境变量。本 fixture 通过将
GJP_ENV_FILE 指向不存在的路径，使 _read_local_env 返回空字典，
从而保证测试完全由 monkeypatch / os.environ 控制。

需要读取自定义环境文件的测试（如 test_config.py）可通过
monkeypatch.setenv 覆盖 GJP_ENV_FILE 指向自己的临时文件。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_project_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """阻止测试读取项目 .env 文件，保证环境变量测试隔离。"""
    monkeypatch.setenv(
        "GJP_ENV_FILE",
        "/dev/null/__nonexistent_test_env__",
    )

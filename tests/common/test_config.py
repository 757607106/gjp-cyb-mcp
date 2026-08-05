"""验证 gjp_common.config 按 GJP_ENV 选择 local / production 环境文件。"""

from __future__ import annotations

from pathlib import Path

import pytest

from gjp_common.config import current_env_name, get_env_value, local_env_path


@pytest.fixture
def env_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """构造带 config/local.env 与 config/production.env 的临时项目根。"""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "src" / "gjp_common").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "local.env").write_text(
        "ENV_MARKER=local\nSHARED_KEY=from-file\n", encoding="utf-8",
    )
    (tmp_path / "config" / "production.env").write_text(
        "ENV_MARKER=production\nSHARED_KEY=from-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GJP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("GJP_ENV_FILE", raising=False)
    monkeypatch.delenv("ENV_MARKER", raising=False)
    monkeypatch.delenv("SHARED_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_default_env_is_local(env_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GJP_ENV", raising=False)

    assert current_env_name() == "local"
    assert local_env_path() == (env_project / "config" / "local.env").resolve()
    assert get_env_value("ENV_MARKER") == "local"


def test_production_env_loads_production_file(env_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GJP_ENV", "production")

    assert current_env_name() == "production"
    assert local_env_path() == (env_project / "config" / "production.env").resolve()
    assert get_env_value("ENV_MARKER") == "production"


def test_unknown_env_falls_back_to_local(env_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GJP_ENV", "staging")

    assert current_env_name() == "local"
    assert get_env_value("ENV_MARKER") == "local"


def test_system_environ_beats_env_file(env_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHARED_KEY", "from-environ")

    assert get_env_value("SHARED_KEY") == "from-environ"


def test_explicit_env_file_beats_env_selection(env_project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom.env"
    custom.write_text("ENV_MARKER=custom\n", encoding="utf-8")
    monkeypatch.setenv("GJP_ENV", "production")
    monkeypatch.setenv("GJP_ENV_FILE", str(custom))

    assert local_env_path() == custom.resolve()
    assert get_env_value("ENV_MARKER") == "custom"

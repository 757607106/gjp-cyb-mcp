"""服务器一键部署脚本契约测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy.sh"


def test_deploy_script_has_valid_shell_syntax() -> None:
    subprocess.run(["bash", "-n", str(DEPLOY_SCRIPT)], check=True)


def test_deploy_script_exposes_explicit_profiles() -> None:
    completed = subprocess.run(
        [str(DEPLOY_SCRIPT), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "deploy.sh <test|production>" in completed.stdout
    assert "origin/test" in completed.stdout
    assert "origin/main" in completed.stdout


def test_deploy_script_rejects_unknown_profile() -> None:
    completed = subprocess.run(
        [str(DEPLOY_SCRIPT), "staging"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2


def test_deploy_script_preserves_deployment_tree_contract() -> None:
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert "git reset --hard" not in source
    assert "uv sync --frozen --no-dev" in source
    assert 'test)\n        TARGET_BRANCH="test"' in source
    assert 'production)\n        TARGET_BRANCH="main"' in source

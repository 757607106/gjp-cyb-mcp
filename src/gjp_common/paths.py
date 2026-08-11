"""路径模块：发现项目根目录，并让运行时相对路径不受当前工作目录影响。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

from .errors import DomainError


PathValue = Union[str, Path]


def _is_project_root(path: Path) -> bool:
    return (path / "pyproject.toml").is_file() and (path / "src" / "gjp_common").is_dir()


def discover_project_root(start: Optional[PathValue] = None) -> Path:
    explicit = os.getenv("GJP_PROJECT_ROOT")
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not _is_project_root(root):
            raise DomainError("project_root_invalid", "GJP_PROJECT_ROOT 不是有效项目根目录：%s" % root)
        return root

    current = Path(start).expanduser().resolve() if start is not None else Path.cwd().resolve()
    search = [current] + list(current.parents)
    package_root = Path(__file__).resolve().parents[2]
    if package_root not in search:
        search.append(package_root)
    for candidate in search:
        if _is_project_root(candidate):
            return candidate
    raise DomainError(
        "project_root_not_found",
        "无法定位项目根目录；请设置 GJP_PROJECT_ROOT",
    )


def resolve_input_path(value: PathValue, project_root: Optional[Path] = None) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    root = project_root or discover_project_root()
    root_candidate = (root / path).resolve()
    if root_candidate.exists():
        return root_candidate
    cwd_candidate = (Path.cwd() / path).resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    # Return the stable project-root path so the error tells users exactly
    # where relative paths are expected, regardless of the current directory.
    return root_candidate


def resolve_output_path(value: PathValue, project_root: Optional[Path] = None) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return ((project_root or discover_project_root()) / path).resolve()

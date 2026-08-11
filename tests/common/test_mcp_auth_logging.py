"""验证 MCP 请求 Authorization 头的脱敏日志正确剥离多余 Bearer 前缀。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gjp_common.mcp import _masked_authorization


def _ctx(authorization: str) -> SimpleNamespace:
    return SimpleNamespace(
        request=SimpleNamespace(headers={"authorization": authorization}),
    )


def test_masked_auth_outputs_masked_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GJP_DEBUG_DUMP_CREDENTIALS", raising=False)
    token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.payload.sig"

    result = _masked_authorization(_ctx("Bearer " + token))

    assert result == "Bearer …(len=%d)" % len(token)


def test_masked_auth_outputs_full_token_when_dump_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GJP_DEBUG_DUMP_CREDENTIALS", "true")
    token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.payload.sig"

    result = _masked_authorization(_ctx("Bearer " + token))

    assert result == "Bearer " + token


def test_masked_auth_strips_duplicate_bearer_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    """脱敏模式下双重 Bearer 应剥离，len 是纯 token 长度。"""
    monkeypatch.delenv("GJP_DEBUG_DUMP_CREDENTIALS", raising=False)
    token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.payload.sig"

    result = _masked_authorization(_ctx("Bearer Bearer " + token))

    assert result == "Bearer …(len=%d)" % len(token)


def test_masked_auth_strips_duplicate_bearer_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """开启转储时双重 Bearer 应剥离后输出完整纯 token。"""
    monkeypatch.setenv("GJP_DEBUG_DUMP_CREDENTIALS", "true")
    token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.payload.sig"

    result = _masked_authorization(_ctx("Bearer Bearer " + token))

    assert result == "Bearer " + token


def test_masked_auth_missing_header() -> None:
    ctx = SimpleNamespace(request=SimpleNamespace(headers={}))
    assert _masked_authorization(ctx) == "<缺失>"


def test_masked_auth_empty_token() -> None:
    result = _masked_authorization(_ctx("Bearer   "))
    assert result == "Bearer <空>"


def test_masked_auth_bearer_no_token() -> None:
    """scheme 后无 token（仅有 scheme 本身）返回 <空>。"""
    result = _masked_authorization(_ctx("Bearer   "))
    assert result == "Bearer <空>"

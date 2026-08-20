"""验证 MCP 请求 Authorization / X-API-Key 头的脱敏日志。"""

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


def _api_key_ctx(api_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        request=SimpleNamespace(headers={"x-api-key": api_key}),
    )


def test_masked_api_key_outputs_masked_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GJP_DEBUG_DUMP_CREDENTIALS", raising=False)
    key = "ak_zJiEvhWvOvgYSSnkX5Md6qbE9j2vXXUjYZUKLPcsram"

    result = _masked_authorization(_api_key_ctx(key))

    assert result == "X-API-Key …(len=%d)" % len(key)


def test_masked_api_key_outputs_full_when_dump_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GJP_DEBUG_DUMP_CREDENTIALS", "true")
    key = "ak_zJiEvhWvOvgYSSnkX5Md6qbE9j2vXXUjYZUKLPcsram"

    result = _masked_authorization(_api_key_ctx(key))

    assert result == "X-API-Key %s" % key


def test_masked_auth_prefers_authorization_over_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同时存在两个头时优先输出 Authorization。"""
    monkeypatch.delenv("GJP_DEBUG_DUMP_CREDENTIALS", raising=False)
    ctx = SimpleNamespace(
        request=SimpleNamespace(
            headers={"authorization": "Bearer t", "x-api-key": "ak_x"},
        ),
    )

    assert _masked_authorization(ctx).startswith("Bearer")

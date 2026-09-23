"""ERP 上游不可达与超时场景 e2e：验证受控失败与等待预算。

复用主 e2e 的环境变量开关（YUNCYB_E2E_API_KEY），独立启动
第二个服务实例，把 YUNCYB_BASE_URL 指向不可达地址并收紧
超时预算，覆盖"等待"场景：

- 连接被拒（本地无监听端口）快速失败；
- 黑洞地址（RFC 5737 保留网段）等待至 YUNCYB_TIMEOUT_SECONDS
  配置的超时后受控失败，不会无限等待；
- 两种失败都返回结构化错误 business_upstream_unavailable，服务不
  崩溃，/healthz 探活保持正常。
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_API_KEY = os.environ.get("YUNCYB_E2E_API_KEY", "").strip()

pytestmark = pytest.mark.skipif(
    not _API_KEY,
    reason="需要 YUNCYB_E2E_API_KEY 环境变量",
)

_HTTP_TIMEOUT = httpx.Timeout(30, read=300)

# 地址必须是 HTTPS scheme（connections.py 会拒绝 http://）；黑洞/拒绝
# 行为发生在 TCP 层，与 scheme 无关。
# 192.0.2.0/24 为 RFC 5737 保留网段，连接请求会一直挂起到超时；
# 127.0.0.1:1 本地无监听端口，立即 ConnectionRefused。
_BLACKHOLE_BASE_URL = "https://192.0.2.1:9/aicyberp-api"
_REFUSED_BASE_URL = "https://127.0.0.1:1/aicyberp-api"
_BLACKHOLE_TIMEOUT_SECONDS = "2"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _server_fixture(base_url: str, timeout_seconds: str | None = None):
    """构建启动指向指定上游地址服务实例的 module 级 fixture。"""

    @pytest.fixture(scope="module")
    def server_url():
        port = _free_port()
        env = os.environ.copy()
        env["YUNCYB_BASE_URL"] = base_url
        env["GJP_ENV"] = "local"
        if timeout_seconds is not None:
            env["YUNCYB_TIMEOUT_SECONDS"] = timeout_seconds
        log_file = tempfile.NamedTemporaryFile(
            prefix="e2e-timeout-uvicorn-", suffix=".log", delete=False,
        )
        process = subprocess.Popen(
            [
                "uv", "run", "uvicorn", "yuncyb.app:app",
                "--host", "127.0.0.1", "--port", str(port),
            ],
            cwd=_PROJECT_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        url = "http://127.0.0.1:%d" % port
        deadline = time.monotonic() + 30
        ready = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                if httpx.get(url + "/healthz", timeout=2).status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        if not ready:
            process.terminate()
            log_file.close()
            log_path = Path(log_file.name)
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            log_path.unlink(missing_ok=True)
            raise AssertionError("MCP 服务未在 30 秒内就绪，服务日志尾部：\n" + tail)
        yield url
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        log_file.close()
        Path(log_file.name).unlink(missing_ok=True)

    return server_url


refused_url = _server_fixture(_REFUSED_BASE_URL)
blackhole_url = _server_fixture(_BLACKHOLE_BASE_URL, _BLACKHOLE_TIMEOUT_SECONDS)


def _call(
    server_url: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """建立一次 MCP 连接执行单个工具调用，返回 structuredContent。"""
    headers = {
        "X-API-Key": _API_KEY,
        "X-Conversation-Id": "e2e-timeout-" + time.strftime("%Y%m%d%H%M%S"),
    }

    async def _run() -> dict[str, Any]:
        async with httpx.AsyncClient(
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        ) as client:
            async with streamable_http_client(
                server_url + "/mcp",
                http_client=client,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments or {})
                    if getattr(result, "isError", False):
                        texts = [
                            getattr(block, "text", "")
                            for block in (result.content or [])
                        ]
                        raise AssertionError("MCP 协议级错误：%s" % " ".join(texts))
                    payload = getattr(result, "structuredContent", None)
                    assert isinstance(payload, dict), "工具结果缺少 structuredContent"
                    return payload

    return asyncio.run(_run())


def test_connection_refused_fails_fast_with_structured_error(refused_url):
    """上游连接被拒时立即返回结构化错误，不挂起也不协议崩溃。"""
    started = time.perf_counter()
    result = _call(refused_url, "syncProducts")
    elapsed = time.perf_counter() - started
    assert result["ok"] is False
    assert result["error"]["code"] == "business_upstream_unavailable"
    assert elapsed < 10, "连接被拒应快速失败，实际耗时 %.1fs" % elapsed


def test_blackhole_upstream_times_out_within_budget(blackhole_url):
    """黑洞地址按 YUNCYB_TIMEOUT_SECONDS 受控超时，不会无限等待。"""
    started = time.perf_counter()
    result = _call(blackhole_url, "syncProducts")
    elapsed = time.perf_counter() - started
    assert result["ok"] is False
    assert result["error"]["code"] == "business_upstream_unavailable"
    assert elapsed <= 15, (
        "应在超时预算内受控失败（配置 %ss），实际耗时 %.1fs"
        % (_BLACKHOLE_TIMEOUT_SECONDS, elapsed)
    )


def test_upstream_outage_keeps_service_alive(blackhole_url):
    """上游故障期间服务持续存活：直查工具同样受控失败且探活正常。"""
    result = _call(blackhole_url, "listSalesOrders", {"page": 1, "page_size": 1})
    assert result["ok"] is False
    assert result["error"]["code"] == "business_upstream_unavailable"

    response = httpx.get(blackhole_url + "/healthz", timeout=5)
    assert response.status_code == 200
    assert response.json() == {"ok": True}

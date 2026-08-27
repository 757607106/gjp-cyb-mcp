"""真实 MCP 服务全流程 e2e 测试：十个工具、会话隔离、幂等、稳定性与性能。

默认整体跳过，同时设置以下环境变量后启用（上游为真实 ERP 测试环境）：

    ERP_BILLING_E2E_API_KEY=<X-API-Key>
    ERP_BILLING_E2E_BASE_URL=https://test-ai.yuncyb.com/aicyberp-api

测试启动真实 uvicorn 子进程，通过 MCP Streamable HTTP 客户端访问；
X-Conversation-Id 参与会话隔离，验证无状态传输下状态按会话键保留。
写操作创建的销售单带"E2E自动化测试"备注，并在流程末尾作废。
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
from mcp.client.streamable_http import streamablehttp_client

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_API_KEY = os.environ.get("ERP_BILLING_E2E_API_KEY", "").strip()
_ERP_BASE_URL = os.environ.get("ERP_BILLING_E2E_BASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not _API_KEY or not _ERP_BASE_URL,
    reason="需要 ERP_BILLING_E2E_API_KEY 与 ERP_BILLING_E2E_BASE_URL 环境变量",
)

_MAIN_CONVERSATION = "e2e-main"
_ISOLATED_CONVERSATION = "e2e-isolated"
_E2E_REMARK = "E2E自动化测试单据，可作废"

# 每个工具调用的耗时预算（毫秒），防性能回归；实际耗时最后统一打印
_BUDGET_MS: dict[str, int] = {
    "syncProducts": 30_000,
    "previewSalesOrder": 20_000,
    "submitSalesOrder": 20_000,
    "updateSalesOrder": 20_000,
    "voidSalesOrder": 20_000,
}
_DEFAULT_BUDGET_MS = 15_000

# module 级共享状态：服务地址、耗时记录与流程数据
_SERVER_URL = ""
_TIMINGS: list[tuple[str, float]] = []
_STATE: dict[str, Any] = {}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    """启动真实 uvicorn 子进程并等待 /healthz 就绪。"""
    global _SERVER_URL
    port = _free_port()
    env = os.environ.copy()
    env["ERP_BILLING_BASE_URL"] = _ERP_BASE_URL
    env["GJP_ENV"] = "local"
    env.pop("ERP_BILLING_JWT_SECRET", None)
    log_file = tempfile.NamedTemporaryFile(
        prefix="e2e-uvicorn-", suffix=".log", delete=False,
    )
    process = subprocess.Popen(
        [
            "uv", "run", "uvicorn", "erp_billing.app:app",
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
        tail = Path(log_file.name).read_text(encoding="utf-8", errors="replace")[-2000:]
        raise AssertionError("MCP 服务未在 30 秒内就绪，服务日志尾部：\n" + tail)
    _SERVER_URL = url
    yield url
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    log_file.close()


def _headers(conversation: str) -> dict[str, str]:
    return {
        "X-API-Key": _API_KEY,
        "X-Conversation-Id": conversation,
    }


def _unwrap(result: Any) -> dict[str, Any]:
    """把 MCP CallToolResult 解包为 dict，优先 structuredContent。"""
    if getattr(result, "isError", False):
        texts = [
            getattr(block, "text", "")
            for block in (result.content or [])
        ]
        raise AssertionError("MCP 协议级错误：%s" % " ".join(texts))
    payload = getattr(result, "structuredContent", None)
    if isinstance(payload, dict):
        return payload
    import json

    for block in result.content or []:
        text = getattr(block, "text", "")
        try:
            parsed = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise AssertionError("工具结果没有可解析的 structuredContent 或 JSON 文本")


def _call(
    tool: str,
    arguments: dict[str, Any] | None = None,
    conversation: str = _MAIN_CONVERSATION,
    *,
    record_timing: bool = True,
) -> dict[str, Any]:
    """建立一次 MCP 连接执行单个工具调用；计时只覆盖 call_tool 本身。"""

    async def _run() -> dict[str, Any]:
        async with streamablehttp_client(
            _SERVER_URL + "/mcp",
            headers=_headers(conversation),
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                started = time.perf_counter()
                result = await session.call_tool(tool, arguments or {})
                if record_timing:
                    _TIMINGS.append((tool, (time.perf_counter() - started) * 1000))
                return _unwrap(result)

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# 基础链路：探活、工具清单、商品目录
# ---------------------------------------------------------------------------


def test_healthz(server_url):
    """/healthz 无鉴权探活返回 200。"""
    response = httpx.get(server_url + "/healthz", timeout=5)
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_initialize_lists_ten_tools(server_url):
    """MCP initialize 后应列出 10 个 camelCase 工具。"""

    async def _run() -> set[str]:
        async with streamablehttp_client(
            _SERVER_URL + "/mcp", headers=_headers(_MAIN_CONVERSATION),
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return {tool.name for tool in tools.tools}

    names = asyncio.run(_run())
    assert names == {
        "syncProducts", "listProducts", "searchProducts",
        "searchBillingReferences", "previewSalesOrder", "submitSalesOrder",
        "getSalesOrder", "listSalesOrders", "voidSalesOrder",
        "updateSalesOrder",
    }


def test_sync_products(server_url):
    """syncProducts 同步真实商品目录并返回样例。"""
    result = _call("syncProducts")
    assert result["ok"] is True
    assert result["product_count"] >= 1
    assert len(result["sample_products"]) >= 1
    assert "id" not in result["sample_products"][0]
    _STATE["product_count"] = result["product_count"]


def test_list_products_pagination(server_url):
    """listProducts 分页浏览目录，业务字段不含系统 ID。"""
    first = _call("listProducts", {"page": 1, "page_size": 5})
    assert first["ok"] is True
    assert first["total"] == _STATE["product_count"]
    assert 1 <= len(first["products"]) <= 5
    assert "product_name" in first["products"][0]

    second = _call("listProducts", {"page": 2, "page_size": 5})
    assert second["ok"] is True
    # 记录真实商品供后续开单使用：优先取有单位的商品
    products = first["products"] + second["products"]
    _STATE["product"] = next(
        (item for item in products if item.get("unit")),
        products[0],
    )


def test_search_products(server_url):
    """searchProducts 按真实商品名查询应命中，并回传 product_id 供确认链路使用。"""
    name = _STATE["product"]["product_name"]
    result = _call("searchProducts", {"keywords": [name]})
    assert result["ok"] is True
    entry = result["results"][0]
    assert entry["query"] == name
    assert entry["status"] == "matched"
    assert entry["product"]["product_name"] == name
    assert entry["product"]["product_id"]


# ---------------------------------------------------------------------------
# 基础资料与预览
# ---------------------------------------------------------------------------


def test_search_billing_references_with_page(server_url):
    """searchBillingReferences 支持翻页并隐藏内部 ID。"""
    page1 = _call("searchBillingReferences", {
        "reference_type": "customer", "keyword": "", "limit": 5, "page": 1,
    })
    assert page1["ok"] is True
    assert 1 <= len(page1["options"]) <= 5
    assert set(page1["options"][0]) <= {"name", "is_default"}
    _STATE["customer"] = page1["options"][0]["name"]

    page2 = _call("searchBillingReferences", {
        "reference_type": "customer", "keyword": "", "limit": 5, "page": 2,
    })
    assert page2["ok"] is True
    names1 = {item["name"] for item in page1["options"]}
    names2 = {item["name"] for item in page2["options"]}
    assert not (names1 & names2), "翻页后不应返回重复候选"

    for reference_type in ("warehouse", "handler"):
        result = _call("searchBillingReferences", {
            "reference_type": reference_type, "limit": 5,
        })
        assert result["ok"] is True
        assert result["options"], "%s 候选不应为空" % reference_type
        _STATE[reference_type] = result["options"][0]["name"]


def test_preview_reports_missing_fields(server_url):
    """只传商品文本时一次性返回全部缺失必填项。"""
    product = _STATE["product"]
    result = _call("previewSalesOrder", {
        "order_text": "%s1%s" % (product["product_name"], product.get("unit") or ""),
    })
    assert result["ok"] is True
    missing = {item["field"] for item in result["missing_required_fields"]}
    assert missing == {"customer", "warehouse", "handler", "order_date"}
    assert result["ready_to_submit"] is False
    assert result["preview_id"] is None


def test_preview_ready_after_confirmation(server_url):
    """补全必填项后生成预览；候选歧义时走 confirmed_products 回传循环。"""
    product = _STATE["product"]
    order_text = "%s2%s" % (product["product_name"], product.get("unit") or "")
    base_arguments = {
        "order_text": order_text,
        "customer": _STATE["customer"],
        "warehouse": _STATE["warehouse"],
        "handler": _STATE["handler"],
        "order_date": time.strftime("%Y-%m-%d"),
        "remark": _E2E_REMARK,
        "save_type": "final",
    }
    confirmed: list[dict[str, str]] = []
    for _ in range(4):
        result = _call("previewSalesOrder", {**base_arguments, "confirmed_products": confirmed})
        assert result["ok"] is True, result.get("error")
        if result["ready_to_submit"]:
            assert result["preview_id"]
            assert result["preview"]["customer"] == _STATE["customer"]
            assert result["preview"]["items"][0]["quantity"] == 2
            _STATE["preview_id"] = result["preview_id"]
            return
        pending: list[dict[str, str]] = []
        for item in result["recommended_products"]:
            pending.append({
                "line_id": item["line_id"],
                "product_id": item["product_id"],
            })
        for item in result["unmatched_products"]:
            searched = _call("searchProducts", {"keywords": [item["product_name"]]})
            entry = searched["results"][0]
            candidates = list(entry.get("recommendations") or [])
            if entry["product"] is not None:
                candidates.insert(0, entry["product"])
            assert candidates, "unmatched 行没有任何候选可确认"
            pending.append({
                "line_id": item["line_id"],
                "product_id": candidates[0]["product_id"],
            })
        assert pending, "未就绪但没有可确认的候选"
        confirmed = pending
    raise AssertionError("多轮确认后仍未 ready_to_submit")


def test_conversation_isolation(server_url):
    """另一个对话窗口拿不到当前对话的 preview_id，会话状态按 X-Conversation-Id 隔离。"""
    result = _call(
        "submitSalesOrder",
        {
            "preview_id": _STATE["preview_id"],
            "idempotency_key": "e2e-isolated-" + time.strftime("%H%M%S"),
            "confirmed_by_user": True,
        },
        conversation=_ISOLATED_CONVERSATION,
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "erp_sales_order_preview_not_found"


# ---------------------------------------------------------------------------
# 写操作：提交、幂等、查询、修改、作废
# ---------------------------------------------------------------------------


def test_submit_requires_confirmation(server_url):
    """未确认时提交应被拒绝且不产生单据。"""
    result = _call("submitSalesOrder", {
        "preview_id": _STATE["preview_id"],
        "idempotency_key": "e2e-no-confirm",
        "confirmed_by_user": False,
    })
    assert result["ok"] is False
    assert result["error"]["code"] == "erp_sales_order_confirmation_required"


def test_submit_returns_order_no_and_replays_idempotently(server_url):
    """提交成功返回业务单号 order_no；同幂等键重放返回同一单号。"""
    key = "e2e-submit-" + time.strftime("%Y%m%d%H%M%S")
    submitted = _call("submitSalesOrder", {
        "preview_id": _STATE["preview_id"],
        "idempotency_key": key,
        "confirmed_by_user": True,
    })
    assert submitted["ok"] is True, submitted.get("error")
    assert submitted["submitted"] is True
    order_no = submitted["order_no"]
    assert order_no and "preview_id" not in submitted
    _STATE["order_no"] = order_no

    replayed = _call("submitSalesOrder", {
        "preview_id": _STATE["preview_id"],
        "idempotency_key": key,
        "confirmed_by_user": True,
    })
    assert replayed["ok"] is True
    assert replayed["idempotent_replay"] is True
    assert replayed["order_no"] == order_no


def test_get_sales_order_by_order_no(server_url):
    """用业务单号查详情，返回 orderNo 一致并记录修改所需的内部字段。"""
    result = _call("getSalesOrder", {"order_id": _STATE["order_no"]})
    assert result["ok"] is True
    order = result["order"]
    assert order["orderNo"] == _STATE["order_no"]
    assert order["items"], "详情应包含商品明细"
    _STATE["detail"] = order


def test_list_sales_orders_filters_by_order_no(server_url):
    """列表按单号过滤能找到刚创建的单据。"""
    result = _call("listSalesOrders", {
        "page": 1, "page_size": 10, "order_no": _STATE["order_no"],
    })
    assert result["ok"] is True
    numbers = [order.get("orderNo") for order in result["orders"]]
    assert _STATE["order_no"] in numbers


def test_update_sales_order(server_url):
    """修改商品数量与备注后单据保持同一业务单号。"""
    order = _STATE["detail"]
    item = order["items"][0]
    original_qty = item["quantity"]
    result = _call("updateSalesOrder", {
        "order_id": _STATE["order_no"],
        "order_date": order["orderDate"],
        "handler_id": str(order["handlerId"]),
        "items": [
            {
                "product_id": str(item["productId"]),
                "quantity": original_qty + 1,
                "unit": item.get("unit") or "",
                "order_item_id": str(item.get("id") or item.get("orderItemId") or ""),
                "remark": "E2E修改",
            },
        ],
        "save_type": "final",
        "remark": _E2E_REMARK,
        "confirmed_by_user": True,
    })
    assert result["ok"] is True, result.get("error")
    assert result["modified"] is True
    assert result["order_no"] == _STATE["order_no"]


def test_get_sales_order_reflects_update(server_url):
    """修改后详情应反映新的数量与备注。"""
    result = _call("getSalesOrder", {"order_id": _STATE["order_no"]})
    assert result["ok"] is True
    order = result["order"]
    assert any(
        item["quantity"] == _STATE["detail"]["items"][0]["quantity"] + 1
        for item in order["items"]
    )


def test_void_sales_order(server_url):
    """作废前需确认；确认后单据状态变为已作废。"""
    rejected = _call("voidSalesOrder", {
        "order_id": _STATE["order_no"], "confirmed_by_user": False,
    })
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "erp_sales_order_confirmation_required"

    voided = _call("voidSalesOrder", {
        "order_id": _STATE["order_no"], "confirmed_by_user": True,
    })
    assert voided["ok"] is True, voided.get("error")
    assert voided["voided"] is True
    assert voided["order_no"] == _STATE["order_no"]

    detail = _call("getSalesOrder", {"order_id": _STATE["order_no"]})
    assert detail["ok"] is True
    assert detail["order"]["status"] == 3, "作废后状态应为 3（已作废）"


# ---------------------------------------------------------------------------
# 稳定性、错误处理与性能
# ---------------------------------------------------------------------------


def test_unknown_order_returns_friendly_error(server_url):
    """查询不存在的单号返回结构化业务错误而非协议错误。"""
    result = _call("getSalesOrder", {"order_id": "XS000000000000"})
    assert result["ok"] is False
    assert result["error"]["code"] == "erp_sales_order_not_found"


def test_invalid_arguments_return_structured_error(server_url):
    """参数名错传时返回 tool_arguments_invalid，模型可自助纠正。"""
    result = _call("listProducts", {"pageValue": 1})
    assert result["ok"] is False
    assert result["error"]["code"] == "tool_arguments_invalid"
    assert "pageValue" in result["error"]["message"]


def test_stability_repeated_calls(server_url):
    """连续多次调用结果一致，无状态漂移。"""
    for _ in range(5):
        result = _call("listProducts", {"page": 1, "page_size": 5}, record_timing=False)
        assert result["ok"] is True
        assert result["total"] == _STATE["product_count"]


def test_stability_concurrent_calls(server_url):
    """同一 MCP 连接内并发调用互不串扰。"""

    async def _run() -> list[dict[str, Any]]:
        async with streamablehttp_client(
            _SERVER_URL + "/mcp", headers=_headers(_MAIN_CONVERSATION),
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                started = time.perf_counter()
                results = await asyncio.gather(*[
                    session.call_tool("listProducts", {"page": 1, "page_size": 5})
                    for _ in range(5)
                ])
                _TIMINGS.append(("listProducts(并发x5)", (time.perf_counter() - started) * 1000))
                return [_unwrap(result) for result in results]

    results = asyncio.run(_run())
    assert len(results) == 5
    for result in results:
        assert result["ok"] is True
        assert result["total"] == _STATE["product_count"]


def test_performance_summary(server_url):
    """汇总各工具耗时并断言在预算内；失败时打印完整耗时表。"""
    lines = ["", "=== E2E 性能汇总（毫秒）==="]
    for tool, elapsed in _TIMINGS:
        lines.append("%-28s %8.0f ms" % (tool, elapsed))
    print("\n".join(lines))
    overflows = [
        (tool, elapsed, _BUDGET_MS.get(tool, _DEFAULT_BUDGET_MS))
        for tool, elapsed in _TIMINGS
        if elapsed > _BUDGET_MS.get(tool, _DEFAULT_BUDGET_MS)
    ]
    assert not overflows, "以下调用超出性能预算：%s" % overflows

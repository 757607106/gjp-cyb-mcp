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
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any
from uuid import uuid4

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

# mcp 1.29 起 streamable_http_client 不再接受 headers 参数，
# 需通过自定义 httpx.AsyncClient 传入鉴权头；read 超时对齐 MCP 默认 300 秒。
_HTTP_TIMEOUT = httpx.Timeout(30, read=300)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_API_KEY = os.environ.get("ERP_BILLING_E2E_API_KEY", "").strip()
_ERP_BASE_URL = os.environ.get("ERP_BILLING_E2E_BASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not _API_KEY or not _ERP_BASE_URL,
    reason="需要 ERP_BILLING_E2E_API_KEY 与 ERP_BILLING_E2E_BASE_URL 环境变量",
)

_RUN_ID = "%s-%s" % (time.strftime("%Y%m%d%H%M%S"), uuid4().hex[:8])
_MAIN_CONVERSATION = "e2e-main-" + _RUN_ID
_ISOLATED_CONVERSATION = "e2e-isolated-" + _RUN_ID
_E2E_REMARK = "E2E自动化测试单据%s，可作废" % _RUN_ID

# 每个工具调用的耗时预算（毫秒），防性能回归；实际耗时最后统一打印
_BUDGET_MS: dict[str, int] = {
    "mcpInitialize": 10_000,
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
        log_path = Path(log_file.name)
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        log_path.unlink(missing_ok=True)
        raise AssertionError("MCP 服务未在 30 秒内就绪，服务日志尾部：\n" + tail)
    _SERVER_URL = url
    yield url
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
    log_file.close()
    Path(log_file.name).unlink(missing_ok=True)


@pytest.fixture(scope="module", autouse=True)
def ensure_created_order_is_voided(server_url):
    """任一后续断言失败时，也要兜底作废本轮已经创建的真实测试单。"""
    yield
    order_no = str(_STATE.get("order_no") or "")
    if not order_no or _STATE.get("voided"):
        return
    result = _call(
        "voidSalesOrder",
        {"order_id": order_no, "confirmed_by_user": True},
        record_timing=False,
    )
    assert result["ok"] is True, "E2E 兜底作废失败：%s" % result.get("error")
    _STATE["voided"] = True


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
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """建立一次 MCP 连接执行单个工具调用；计时只覆盖 call_tool 本身。"""
    effective_headers = _headers(conversation) if headers is None else headers

    async def _run() -> dict[str, Any]:
        async with httpx.AsyncClient(
            headers=effective_headers,
            timeout=_HTTP_TIMEOUT,
        ) as client:
            async with streamable_http_client(
                _SERVER_URL + "/mcp",
                http_client=client,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    started = time.perf_counter()
                    result = await session.call_tool(tool, arguments or {})
                    if record_timing:
                        _TIMINGS.append((tool, (time.perf_counter() - started) * 1000))
                    return _unwrap(result)

    return asyncio.run(_run())


def _call_protocol_error(
    tool: str,
    arguments: dict[str, Any] | None = None,
    conversation: str = _MAIN_CONVERSATION,
    *,
    headers: dict[str, str] | None = None,
) -> str:
    """调用预期产生 MCP 协议级错误的工具，返回错误文本。"""
    effective_headers = _headers(conversation) if headers is None else headers

    async def _run() -> str:
        async with httpx.AsyncClient(
            headers=effective_headers,
            timeout=_HTTP_TIMEOUT,
        ) as client:
            async with streamable_http_client(
                _SERVER_URL + "/mcp",
                http_client=client,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments or {})
                    assert result.isError, "预期 MCP 协议级错误，实际成功返回"
                    return " ".join(
                        getattr(block, "text", "")
                        for block in (result.content or [])
                    )

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
    """MCP initialize 应返回服务说明及 10 个带输入输出 Schema 的工具。"""

    async def _run() -> list[Any]:
        async with httpx.AsyncClient(
            headers=_headers(_MAIN_CONVERSATION),
            timeout=_HTTP_TIMEOUT,
        ) as client:
            async with streamable_http_client(
                _SERVER_URL + "/mcp", http_client=client,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    started = time.perf_counter()
                    initialized = await session.initialize()
                    _TIMINGS.append(("mcpInitialize", (time.perf_counter() - started) * 1000))
                    assert "ERP 销售开单服务" in (initialized.instructions or "")
                    tools = await session.list_tools()
                    return tools.tools

    tools = asyncio.run(_run())
    names = {tool.name for tool in tools}
    assert names == {
        "syncProducts", "listProducts", "searchProducts",
        "searchBillingReferences", "previewSalesOrder", "submitSalesOrder",
        "getSalesOrder", "listSalesOrders", "voidSalesOrder",
        "updateSalesOrder",
    }
    for tool in tools:
        assert tool.inputSchema["type"] == "object"
        assert tool.outputSchema is not None
        assert tool.outputSchema["type"] == "object"


def test_missing_api_key_rejected(server_url):
    """缺少 X-API-Key 时工具调用被 MCP 协议层拒绝，不泄露任何业务数据。"""
    error_text = _call_protocol_error(
        "syncProducts",
        headers={"X-Conversation-Id": "e2e-nokey-" + _RUN_ID},
    )
    assert "X-API-Key" in error_text


def test_invalid_api_key_rejected(server_url):
    """无效 X-API-Key 返回结构化业务错误而非协议崩溃，模型可提示重新鉴权。"""
    result = _call(
        "syncProducts",
        headers={
            "X-API-Key": "ak_invalid_e2e_" + _RUN_ID,
            "X-Conversation-Id": "e2e-badkey-" + _RUN_ID,
        },
    )
    assert result["ok"] is False
    assert result["error"]["code"] in {
        "business_reauth_required", "erp_live_request_failed",
    }


def test_unknown_tool_rejected(server_url):
    """未知工具名在协议层报错，后续正常调用不受影响。"""
    error_text = _call_protocol_error("nonexistentTool")
    assert "nonexistentTool" in error_text
    result = _call("listProducts", {"page": 1, "page_size": 1}, record_timing=False)
    assert result["ok"] is True


def test_sync_products(server_url):
    """syncProducts 同步真实商品目录并返回样例。"""
    result = _call("syncProducts")
    assert result["ok"] is True
    assert result["product_count"] >= 1
    assert len(result["sample_products"]) >= 1
    assert "id" not in result["sample_products"][0]
    _STATE["product_count"] = result["product_count"]


def test_sync_products_with_limit(server_url):
    """syncProducts 支持 limit 截断；独立会话不污染主会话商品目录。"""
    conversation = "e2e-limit-" + _RUN_ID
    result = _call("syncProducts", {"limit": 3}, conversation=conversation)
    assert result["ok"] is True
    assert result["product_count"] == 3
    assert len(result["sample_products"]) == 3
    # 主会话目录不受 limit 同步影响
    main = _call("listProducts", {"page": 1, "page_size": 1}, record_timing=False)
    assert main["total"] == _STATE["product_count"]


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


def test_fresh_conversation_reuses_shared_catalog(server_url):
    """新对话无需先同步即可使用租户共享目录（目录不随会话淘汰丢失）。"""
    conversation = "e2e-shared-" + _RUN_ID
    result = _call(
        "listProducts",
        {"page": 1, "page_size": 1},
        conversation=conversation,
        record_timing=False,
    )
    assert result["ok"] is True
    assert result["total"] == _STATE["product_count"]
    # 共享目录对预览同样立即可用：商品已能匹配，只缺单头必填项
    preview = _call(
        "previewSalesOrder",
        {
            "order_text": "%s1%s"
            % (
                _STATE["product"]["product_name"],
                _STATE["product"].get("unit") or "",
            ),
        },
        conversation=conversation,
        record_timing=False,
    )
    assert preview["ok"] is True
    assert preview["unmatched_products"] == []
    missing = {item["field"] for item in preview["missing_required_fields"]}
    assert missing == {"customer", "warehouse", "handler", "order_date"}


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


def test_search_products_multiple_keywords(server_url):
    """searchProducts 多关键词批量查询，结果顺序与入参一致。"""
    products = _call("listProducts", {"page": 1, "page_size": 2}, record_timing=False)["products"]
    names = [item["product_name"] for item in products]
    if len(set(names)) < 2:
        # 目录前两行同名时向后补一个不同名的商品
        extra = _call("listProducts", {"page": 2, "page_size": 2}, record_timing=False)["products"]
        for item in extra:
            if item["product_name"] not in names:
                names.append(item["product_name"])
                break
    result = _call("searchProducts", {"keywords": names[:2]})
    assert result["ok"] is True
    assert [entry["query"] for entry in result["results"]] == names[:2]
    assert all(entry["status"] == "matched" for entry in result["results"])


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
    assert page1["page"] == 1
    assert page1["page_size"] == 5
    assert page1["total"] >= len(page1["options"])
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


def test_search_billing_references_keyword_filter(server_url):
    """searchBillingReferences 关键词过滤应命中包含该词的候选。"""
    # 取客户全名的前两个字符作为关键词，确保有命中
    customer = _STATE["customer"]
    keyword = customer[:2] if len(customer) >= 2 else customer
    result = _call("searchBillingReferences", {
        "reference_type": "customer", "keyword": keyword, "limit": 20,
    })
    assert result["ok"] is True
    assert result["total"] >= 1
    assert any(
        keyword in option["name"] for option in result["options"]
    ), "关键词过滤未命中任何候选"


def test_search_billing_references_invalid_type(server_url):
    """reference_type 非法时被输入 Schema 枚举在协议层拦截，不产生 ERP 调用。"""
    error_text = _call_protocol_error("searchBillingReferences", {
        "reference_type": "department", "limit": 5,
    })
    assert "department" in error_text
    assert "customer" in error_text, "错误应提示合法枚举值供模型自助纠正"


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


def test_preview_parameter_validation(server_url):
    """枚举参数被 Schema 在协议层拦截；日期与备注长度在运行时返回结构化错误。"""
    base = {
        "order_text": "任意商品1",
        "customer": _STATE["customer"],
        "warehouse": _STATE["warehouse"],
        "handler": _STATE["handler"],
        "order_date": time.strftime("%Y-%m-%d"),
    }
    # 枚举类非法值：inputSchema 的 enum 在协议层拒绝，模型可直接纠正
    for arguments in (
        {**base, "save_type": "urgent"},
        {**base, "source": "video"},
    ):
        error_text = _call_protocol_error("previewSalesOrder", arguments)
        assert "Input validation error" in error_text, arguments
    # 运行时校验：日期格式与备注长度返回结构化业务错误
    invalid_cases = [
        ({**base, "order_date": "2026/08/27"}, "erp_sales_order_date_invalid"),
        ({**base, "order_date": "not-a-date"}, "erp_sales_order_date_invalid"),
        ({**base, "remark": "长" * 201}, "erp_sales_order_remark_too_long"),
    ]
    for arguments, expected_code in invalid_cases:
        result = _call("previewSalesOrder", arguments, record_timing=False)
        assert result["ok"] is False, arguments
        assert result["error"]["code"] == expected_code, arguments


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


def test_draft_order_lifecycle_with_voice_source(server_url):
    """独立会话走草稿全流程：voice 来源预览 → 提交草稿 → 查详情 → 作废。"""
    conversation = "e2e-draft-" + _RUN_ID
    product = _STATE["product"]
    order_text = "%s2%s" % (product["product_name"], product.get("unit") or "")
    base_arguments = {
        "order_text": order_text,
        "customer": _STATE["customer"],
        "warehouse": _STATE["warehouse"],
        "handler": _STATE["handler"],
        "order_date": time.strftime("%Y-%m-%d"),
        "remark": _E2E_REMARK,
        "save_type": "draft",
        "source": "voice",
    }
    confirmed: list[dict[str, str]] = []
    preview_id = ""
    for _ in range(4):
        result = _call(
            "previewSalesOrder",
            {**base_arguments, "confirmed_products": confirmed},
            conversation=conversation,
        )
        assert result["ok"] is True, result.get("error")
        if result["ready_to_submit"]:
            assert result["preview"]["save_type"] == "draft"
            assert result["preview"]["save_type_label"] == "草稿"
            preview_id = result["preview_id"]
            break
        pending = [
            {"line_id": item["line_id"], "product_id": item["product_id"]}
            for item in result["recommended_products"]
        ]
        for item in result["unmatched_products"]:
            searched = _call(
                "searchProducts",
                {"keywords": [item["product_name"]]},
                conversation=conversation,
            )
            entry = searched["results"][0]
            candidates = list(entry.get("recommendations") or [])
            if entry["product"] is not None:
                candidates.insert(0, entry["product"])
            assert candidates, "草稿流程 unmatched 行没有任何候选可确认"
            pending.append({
                "line_id": item["line_id"],
                "product_id": candidates[0]["product_id"],
            })
        assert pending, "草稿流程未就绪但没有可确认的候选"
        confirmed = pending
    else:
        raise AssertionError("草稿流程多轮确认后仍未 ready_to_submit")

    submitted = _call(
        "submitSalesOrder",
        {
            "preview_id": preview_id,
            "idempotency_key": "e2e-draft-" + _RUN_ID,
            "confirmed_by_user": True,
        },
        conversation=conversation,
    )
    assert submitted["ok"] is True, submitted.get("error")
    assert submitted["save_type"] == "draft"
    draft_order_no = submitted["order_no"]
    assert draft_order_no
    detail = _call(
        "getSalesOrder", {"order_id": draft_order_no},
        conversation=conversation,
    )
    assert detail["ok"] is True
    assert detail["order"]["orderNo"] == draft_order_no
    assert detail["order"]["status"] in (0, 1), "草稿单状态应为 0 或 1"

    # 真实 ERP 不允许直接作废草稿单（不满足作废条件），
    # 工具应透传结构化错误而非协议崩溃
    draft = detail["order"]
    rejected = _call(
        "voidSalesOrder",
        {"order_id": draft_order_no, "confirmed_by_user": True},
        conversation=conversation,
    )
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "erp_live_request_failed"

    # 草稿转正式过账后再作废，完成清理闭环
    draft_item = draft["items"][0]
    converted = _call(
        "updateSalesOrder",
        {
            "order_id": draft_order_no,
            "order_date": draft["orderDate"],
            "handler_id": str(draft["handlerId"]),
            "items": [
                {
                    "product_id": str(draft_item["productId"]),
                    "quantity": draft_item["quantity"],
                    "unit": draft_item.get("unit") or "",
                    "order_item_id": str(
                        draft_item.get("id") or draft_item.get("orderItemId") or "",
                    ),
                },
            ],
            "save_type": "final",
            "remark": _E2E_REMARK,
            "confirmed_by_user": True,
        },
        conversation=conversation,
    )
    assert converted["ok"] is True, converted.get("error")
    assert converted["order_no"] == draft_order_no

    voided = _call(
        "voidSalesOrder",
        {"order_id": draft_order_no, "confirmed_by_user": True},
        conversation=conversation,
    )
    assert voided["ok"] is True, "草稿转正式后作废失败：%s" % voided.get("error")

    final_detail = _call(
        "getSalesOrder", {"order_id": draft_order_no},
        conversation=conversation,
    )
    assert final_detail["order"]["status"] == 3


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
    key = "e2e-submit-" + _RUN_ID
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


def test_submit_idempotency_key_conflict(server_url):
    """同一幂等键用于另一份预览时拒绝重放，防止串单。"""
    # 同参数再生成一份新预览，与已提交单据共用同一幂等键
    product = _STATE["product"]
    order_text = "%s1%s" % (product["product_name"], product.get("unit") or "")
    confirmed: list[dict[str, str]] = []
    preview_id = ""
    for _ in range(4):
        result = _call("previewSalesOrder", {
            "order_text": order_text,
            "customer": _STATE["customer"],
            "warehouse": _STATE["warehouse"],
            "handler": _STATE["handler"],
            "order_date": time.strftime("%Y-%m-%d"),
            "remark": _E2E_REMARK,
            "save_type": "final",
            "confirmed_products": confirmed,
        })
        assert result["ok"] is True, result.get("error")
        if result["ready_to_submit"]:
            preview_id = result["preview_id"]
            break
        confirmed = [
            {"line_id": item["line_id"], "product_id": item["product_id"]}
            for item in result["recommended_products"]
        ]
        for item in result["unmatched_products"]:
            searched = _call("searchProducts", {"keywords": [item["product_name"]]})
            entry = searched["results"][0]
            candidates = list(entry.get("recommendations") or [])
            if entry["product"] is not None:
                candidates.insert(0, entry["product"])
            assert candidates, "幂等冲突用例 unmatched 行没有候选"
            confirmed.append({
                "line_id": item["line_id"],
                "product_id": candidates[0]["product_id"],
            })
    else:
        raise AssertionError("幂等冲突用例未能生成新预览")

    conflict = _call("submitSalesOrder", {
        "preview_id": preview_id,
        "idempotency_key": "e2e-submit-" + _RUN_ID,
        "confirmed_by_user": True,
    })
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "erp_sales_order_idempotency_key_conflict"


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


def test_list_sales_orders_date_range(server_url):
    """按录单日期范围过滤能命中今天创建的单据。"""
    today = time.strftime("%Y-%m-%d")
    result = _call("listSalesOrders", {
        "page": 1, "page_size": 100,
        "start_date": today, "end_date": today,
    })
    assert result["ok"] is True
    numbers = [order.get("orderNo") for order in result["orders"]]
    assert _STATE["order_no"] in numbers

    # 日期区间倒置与非法格式均被拦截为结构化错误
    invalid_cases = [
        ({"start_date": today, "end_date": "2000-01-01"}, "erp_sales_order_date_invalid"),
        ({"start_date": "2026/08/27"}, "erp_sales_order_date_invalid"),
        ({"end_date": "not-a-date"}, "erp_sales_order_date_invalid"),
    ]
    for arguments, expected_code in invalid_cases:
        rejected = _call("listSalesOrders", arguments, record_timing=False)
        assert rejected["ok"] is False, arguments
        assert rejected["error"]["code"] == expected_code, arguments


def test_update_requires_confirmation(server_url):
    """修改未确认时应被拒绝且不改变单据。"""
    order = _STATE["detail"]
    item = order["items"][0]
    result = _call("updateSalesOrder", {
        "order_id": _STATE["order_no"],
        "order_date": order["orderDate"],
        "handler_id": str(order["handlerId"]),
        "items": [
            {
                "product_id": str(item["productId"]),
                "quantity": item["quantity"] + 5,
                "unit": item.get("unit") or "",
                "order_item_id": str(item.get("id") or item.get("orderItemId") or ""),
            },
        ],
        "save_type": "final",
        "confirmed_by_user": False,
    })
    assert result["ok"] is False
    assert result["error"]["code"] == "erp_sales_order_confirmation_required"


def test_update_rejects_invalid_items(server_url):
    """商品明细为空或数量非法时返回结构化错误。"""
    order = _STATE["detail"]
    invalid_cases = [
        ({"items": []}, "erp_sales_order_items_empty"),
        (
            {"items": [{"product_id": "", "quantity": 1}]},
            "erp_sales_order_item_invalid",
        ),
        (
            {"items": [{"product_id": "1", "quantity": 0}]},
            "erp_sales_order_item_invalid",
        ),
    ]
    for arguments, expected_code in invalid_cases:
        result = _call("updateSalesOrder", {
            "order_id": _STATE["order_no"],
            "order_date": order["orderDate"],
            "handler_id": str(order["handlerId"]),
            "save_type": "draft",
            "confirmed_by_user": True,
            **arguments,
        }, record_timing=False)
        assert result["ok"] is False, arguments
        assert result["error"]["code"] == expected_code, arguments


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
    _STATE["voided"] = True

    detail = _call("getSalesOrder", {"order_id": _STATE["order_no"]})
    assert detail["ok"] is True
    assert detail["order"]["status"] == 3, "作废后状态应为 3（已作废）"


def test_void_rejects_invalid_order_id(server_url):
    """空单号与不存在单号均返回结构化错误，不会误作废其他单据。"""
    empty = _call("voidSalesOrder", {
        "order_id": "   ", "confirmed_by_user": True,
    })
    assert empty["ok"] is False
    assert empty["error"]["code"] == "erp_sales_order_id_invalid"

    missing = _call("voidSalesOrder", {
        "order_id": "XS000000000000", "confirmed_by_user": True,
    })
    assert missing["ok"] is False
    assert missing["error"]["code"] == "erp_sales_order_not_found"


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
        result = _call("listProducts", {"page": 1, "page_size": 5})
        assert result["ok"] is True
        assert result["total"] == _STATE["product_count"]


def test_stability_concurrent_calls(server_url):
    """同一 MCP 连接内并发调用互不串扰。"""

    async def _run() -> list[dict[str, Any]]:
        async with httpx.AsyncClient(
            headers=_headers(_MAIN_CONVERSATION),
            timeout=_HTTP_TIMEOUT,
        ) as client:
            async with streamable_http_client(
                _SERVER_URL + "/mcp", http_client=client,
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


def test_stability_concurrent_sessions(server_url):
    """多个会话并发调用同一服务，各自目录独立且结果一致。"""

    async def _run() -> list[dict[str, Any]]:
        async def _session_call(conversation: str) -> dict[str, Any]:
            async with httpx.AsyncClient(
                headers=_headers(conversation),
                timeout=_HTTP_TIMEOUT,
            ) as client:
                async with streamable_http_client(
                    _SERVER_URL + "/mcp", http_client=client,
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            "listProducts", {"page": 1, "page_size": 5},
                        )
                        return _unwrap(result)

        return await asyncio.gather(*[
            _session_call("e2e-parallel-%d-%s" % (index, _RUN_ID))
            for index in range(3)
        ])

    results = asyncio.run(_run())
    assert len(results) == 3
    for result in results:
        assert result["ok"] is True
        assert result["total"] == _STATE["product_count"]


def test_performance_summary(server_url):
    """汇总各工具耗时并断言在预算内；失败时打印完整耗时表。"""
    lines = ["", "=== E2E 性能汇总（毫秒）==="]
    for tool, elapsed in _TIMINGS:
        lines.append("%-28s %8.0f ms" % (tool, elapsed))
    grouped: dict[str, list[float]] = defaultdict(list)
    for tool, elapsed in _TIMINGS:
        grouped[tool].append(elapsed)
    lines.append("--- 聚合：次数 / 最小 / 中位 / 最大 ---")
    for tool, values in grouped.items():
        lines.append(
            "%-28s %3d / %6.0f / %6.0f / %6.0f ms"
            % (tool, len(values), min(values), median(values), max(values)),
        )
    print("\n".join(lines))
    overflows = [
        (tool, elapsed, _BUDGET_MS.get(tool, _DEFAULT_BUDGET_MS))
        for tool, elapsed in _TIMINGS
        if elapsed > _BUDGET_MS.get(tool, _DEFAULT_BUDGET_MS)
    ]
    assert not overflows, "以下调用超出性能预算：%s" % overflows
